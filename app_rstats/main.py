"""ESSOSC Raster Stats API.

This FastAPI application exposes endpoints for retrieving pixel-level,
window-level, and geometry-based statistics from registered GeoTIFF rasters.
It supports automatic coordinate reprojection, masking, and summary operations
(mean, sum, min, max, std, count, median, histogram). The service loads its
raster registry from a YAML file defined by the environment variable
`RASTERS_YAML_PATH`.

Endpoints:
    GET  /health              - Returns service status and available rasters
    GET  /rasters             - Lists all registered rasters
    POST /stats/pixel         - Returns a single pixel or small window’s value(s)
    POST /stats/geometry      - Computes zonal statistics for a polygon geometry

Dependencies:
    - FastAPI for the API framework
    - Rasterio for raster data access
    - Shapely and PyProj for geometry and CRS operations
    - NumPy for numerical statistics
    - YAML and dotenv for configuration management

Intended usage:
    Run with `uvicorn app_rstats.main:app --host 0.0.0.0 --port 8000`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Union
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import traceback
import zipfile

from affine import Affine
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from osgeo import gdal, ogr, osr
from pydantic import BaseModel
from pyproj import Geod, Transformer
from rasterio.errors import WindowError
from rasterio.features import geometry_mask
from rasterio.mask import mask as rio_mask
from rasterio.warp import reproject, Resampling
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds, Window
from shapely.geometry import shape, mapping
from shapely.ops import transform as shp_transform
from starlette.background import BackgroundTask
from starlette.responses import FileResponse
import numpy as np
import rasterio
import yaml

load_dotenv()

# this will be mounted in the docker container when it is launched to always
# point at this yaml
RASTERS_YAML_PATH = Path("/app/layers.yml")


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
logging.getLogger("rasterio").setLevel(logging.WARN)

_GEOD = Geod(ellps="WGS84")
_SQUARE_METERS_PER_HECTARE = 10_000.0
_LEGEND_KEY_SEPARATOR = "\x00"
_GEOMETRY_SCATTER_CHUNK_SIZE = 1000
_STATS_JOB_TTL_SECONDS = 15 * 60


class StatsJobCancelled(Exception):
    """Raised when a running stats job is cooperatively cancelled."""


@dataclass
class _StatsJob:
    job_id: str
    session_id: Optional[str]
    cancel_event: threading.Event = field(default_factory=threading.Event)
    status: str = "running"
    progress: float = 0.0
    message: str = "Preparing stats"
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    done_at: Optional[float] = None
    error: Optional[str] = None


_STATS_JOBS: dict[str, _StatsJob] = {}
_STATS_SESSION_ACTIVE: dict[str, str] = {}
_STATS_JOBS_LOCK = threading.Lock()


def _stats_job_snapshot(job: _StatsJob) -> dict:
    return {
        "job_id": job.job_id,
        "session_id": job.session_id,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "error": job.error,
        "started_at": job.started_at,
        "updated_at": job.updated_at,
        "done_at": job.done_at,
    }


def _cleanup_stats_jobs(now: Optional[float] = None):
    now = now or time.time()
    stale_ids = [
        job_id
        for job_id, job in _STATS_JOBS.items()
        if job.done_at is not None and now - job.done_at > _STATS_JOB_TTL_SECONDS
    ]
    for job_id in stale_ids:
        job = _STATS_JOBS.pop(job_id, None)
        if (
            job
            and job.session_id
            and _STATS_SESSION_ACTIVE.get(job.session_id) == job_id
        ):
            _STATS_SESSION_ACTIVE.pop(job.session_id, None)


def _register_stats_job(
    job_id: Optional[str],
    session_id: Optional[str],
) -> Optional[_StatsJob]:
    if not job_id:
        return None

    with _STATS_JOBS_LOCK:
        _cleanup_stats_jobs()
        if session_id:
            previous_id = _STATS_SESSION_ACTIVE.get(session_id)
            if previous_id and previous_id != job_id:
                previous = _STATS_JOBS.get(previous_id)
                if previous and previous.status == "running":
                    previous.cancel_event.set()
                    previous.status = "cancelled"
                    previous.message = "Cancelled by a newer stats request"
                    previous.progress = min(previous.progress, 0.99)
                    previous.updated_at = time.time()
                    previous.done_at = previous.updated_at

        job = _StatsJob(job_id=job_id, session_id=session_id)
        _STATS_JOBS[job_id] = job
        if session_id:
            _STATS_SESSION_ACTIVE[session_id] = job_id
        return job


def _update_stats_job(
    job: Optional[_StatsJob],
    *,
    progress: Optional[float] = None,
    message: Optional[str] = None,
    status: Optional[str] = None,
    error: Optional[str] = None,
):
    if job is None:
        return
    with _STATS_JOBS_LOCK:
        current = _STATS_JOBS.get(job.job_id)
        if current is None:
            return
        if progress is not None:
            current.progress = max(0.0, min(1.0, float(progress)))
        if message is not None:
            current.message = message
        if status is not None:
            current.status = status
        if error is not None:
            current.error = error
        current.updated_at = time.time()
        if (
            current.status in {"completed", "cancelled", "failed"}
            and current.done_at is None
        ):
            current.done_at = current.updated_at


def _raise_if_stats_job_cancelled(job: Optional[_StatsJob]):
    if job is not None and job.cancel_event.is_set():
        _update_stats_job(
            job,
            status="cancelled",
            message="Stats request cancelled",
            progress=min(job.progress, 0.99),
        )
        raise StatsJobCancelled()


def _geometry_log_summary(geometry: dict) -> str:
    """Return a compact geometry summary that omits coordinate values."""
    if not isinstance(geometry, dict):
        return "type=unknown"

    geom_type = geometry.get("type") or "unknown"
    coords = geometry.get("coordinates")

    if geom_type == "Point":
        return "type=Point vertices=1"
    if geom_type in {"LineString", "MultiPoint"} and isinstance(coords, list):
        return f"type={geom_type} vertices={len(coords)}"
    if geom_type == "Polygon" and isinstance(coords, list):
        return (
            f"type=Polygon rings={len(coords)} "
            f"vertices={sum(len(ring) for ring in coords if isinstance(ring, list))}"
        )
    if geom_type == "MultiLineString" and isinstance(coords, list):
        return (
            f"type=MultiLineString lines={len(coords)} "
            f"vertices={sum(len(line) for line in coords if isinstance(line, list))}"
        )
    if geom_type == "MultiPolygon" and isinstance(coords, list):
        rings = sum(len(poly) for poly in coords if isinstance(poly, list))
        vertices = sum(
            len(ring)
            for poly in coords
            if isinstance(poly, list)
            for ring in poly
            if isinstance(ring, list)
        )
        return f"type=MultiPolygon polygons={len(coords)} rings={rings} vertices={vertices}"
    if geom_type == "GeometryCollection":
        geometries = geometry.get("geometries")
        count = len(geometries) if isinstance(geometries, list) else 0
        return f"type=GeometryCollection geometries={count}"

    return f"type={geom_type}"


class RasterMinMaxIn(BaseModel):
    """Input model for min max value query.

    Attributes:
        raster_id (str): Identifier of the target raster in the registry.
    """

    raster_id: str


class RasterMinMaxOut(BaseModel):
    """Output model for just min max of the raster.

    Attributes:
        raster_id (str): Identifier of the raster used for analysis.
        min_ (float): minimum value in the raster
        max_ (float): maximum value in the raster
    """

    raster_id: str
    min_: float
    max_: float


class GeometryScatterIn(BaseModel):
    raster_id_x: Optional[str]
    raster_id_y: Optional[str]
    geometry: dict
    from_crs: str
    histogram_bins: int
    max_points: int
    all_touched: bool
    job_id: Optional[str] = None
    session_id: Optional[str] = None


class StatsJobCancelIn(BaseModel):
    session_id: Optional[str] = None


class StatsJobStatusOut(BaseModel):
    job_id: str
    session_id: Optional[str] = None
    status: str
    progress: float
    message: str
    error: Optional[str] = None
    started_at: float
    updated_at: float
    done_at: Optional[float] = None


class RasterSummary(BaseModel):
    """Summary statistics for valid raster samples."""

    count: int
    area_hectares: float
    area_percent: float
    sum: float
    mean: float


class CategoryAreaSummary(BaseModel):
    """Area summary for a configured categorical legend item."""

    label: str
    area_hectares: float
    area_percent: float
    color: Optional[str] = None
    opacity: Optional[float] = None


class ScatterOut(BaseModel):
    """Output model for scatterplot and histogram statistics between two raster layers.

    This model represents the result of a bivariate comparison between two raster datasets
    (raster_id_x and raster_id_y) within a specified window or geometry. Many of the
    statistical fields are optional and may be `None` if the window did not cover any
    valid part of the raster.

    Attributes:
        raster_id_x (str): Identifier for the X-axis raster layer.
        raster_id_y (str): Identifier for the Y-axis raster layer.
        x (Optional[List[float]]): List of X-axis pixel values, or None if unavailable.
        y (Optional[List[float]]): List of Y-axis pixel values, or None if unavailable.
        hist2d (Optional[List[List[int]]]): 2D histogram counts, or None if unavailable.
        x_edges (Optional[List[float]]): Bin edges for the X-axis histogram, or None if unavailable.
        y_edges (Optional[List[float]]): Bin edges for the Y-axis histogram, or None if unavailable.
        x_summary (Optional[RasterSummary]): Valid-value summary for the X-axis raster.
        y_summary (Optional[RasterSummary]): Valid-value summary for the Y-axis raster.
        x_categories (Optional[List[CategoryAreaSummary]]): Category area summaries for the X-axis raster.
        y_categories (Optional[List[CategoryAreaSummary]]): Category area summaries for the Y-axis raster.
        pearson_r (Optional[float]): Pearson correlation coefficient, or None if not computed.
        slope (Optional[float]): Linear regression slope (Y on X), or None if not computed.
        intercept (Optional[float]): Linear regression intercept, or None if not computed.
        valid_pixels (Optional[int]): Number of valid (non-null) paired pixels, or None if not available.
        geometry (dict): GeoJSON-like geometry defining the analysis window.
    """

    raster_id_x: Optional[str]
    raster_id_y: Optional[str]
    x: Optional[List[float]] = None
    y: Optional[List[float]] = None
    hist2d: Optional[List[List[int]]] = None
    hist1d_x: Optional[List[int]] = None
    hist1d_y: Optional[List[int]] = None
    x_edges: Optional[List[float]] = None
    y_edges: Optional[List[float]] = None
    x_summary: Optional[RasterSummary] = None
    y_summary: Optional[RasterSummary] = None
    x_categories: Optional[List[CategoryAreaSummary]] = None
    y_categories: Optional[List[CategoryAreaSummary]] = None
    pearson_r: Optional[float] = None
    slope: Optional[float] = None
    intercept: Optional[float] = None
    valid_pixels: Optional[int] = None
    geometry: dict


def valid_area_hectares(
    valid_mask: np.ndarray,
    affine: Affine,
    raster_crs: rasterio.crs.CRS,
) -> float:
    """Estimate sampled valid raster area in hectares.

    Args:
        valid_mask: Boolean array where True marks finite, non-nodata sample
            pixels inside the requested geometry.
        affine: Affine transform for the sampled array.
        raster_crs: Coordinate reference system for the sampled raster.

    Returns:
        Valid sampled area in hectares.
    """

    if raster_crs.is_projected:
        unit_factor = raster_crs.linear_units_factor[1]
        pixel_area = abs(affine.a * affine.e - affine.b * affine.d)
        area_m2 = np.count_nonzero(valid_mask) * pixel_area * unit_factor**2
        return float(area_m2 / _SQUARE_METERS_PER_HECTARE)

    row_counts = np.count_nonzero(valid_mask, axis=1)
    rows = np.flatnonzero(row_counts)
    transformer_obj = None
    if raster_crs.to_epsg() != 4326:
        transformer_obj = Transformer.from_crs(
            raster_crs, "EPSG:4326", always_xy=True
        )

    area_m2 = 0.0
    for row_index in rows:
        corners = [
            affine * (0, row_index),
            affine * (1, row_index),
            affine * (1, row_index + 1),
            affine * (0, row_index + 1),
        ]
        xs, ys = zip(*corners)
        if transformer_obj is not None:
            xs, ys = transformer_obj.transform(xs, ys)
        cell_area_m2, _ = _GEOD.polygon_area_perimeter(xs, ys)
        area_m2 += abs(cell_area_m2) * int(row_counts[row_index])

    return float(area_m2 / _SQUARE_METERS_PER_HECTARE)


def legend_groups_for_rendering(rendering: dict) -> tuple[dict[int, str], dict[str, dict]]:
    """Build code-to-legend grouping metadata from categorical rendering config.

    Args:
        rendering: Layer rendering metadata from the YAML registry.

    Returns:
        A tuple containing a raster-code to legend-group-key mapping and an
        ordered dictionary of legend-group metadata.
    """

    categories = rendering.get("categories") or {}
    configured_order = [
        str(value) for value in (rendering.get("legend") or {}).get("order", [])
    ]
    category_keys = [str(value) for value in categories.keys()]
    ordered_keys = [
        *[
            key
            for key in configured_order
            if key in category_keys or int(key) in categories
        ],
        *[
            key
            for key in sorted(category_keys, key=lambda value: float(value))
            if key not in configured_order
        ],
    ]

    code_to_group = {}
    group_meta = {}
    for key in ordered_keys:
        category = categories.get(key)
        if category is None:
            category = categories.get(int(float(key)), {})
        category = category or {}
        label = str(category.get("label", key))
        color = category.get("color")
        opacity = category.get("opacity", rendering.get("opacity"))
        color_key = "" if color is None else str(color)
        opacity_key = "" if opacity is None else str(opacity)
        group_key = _LEGEND_KEY_SEPARATOR.join(
            [label, color_key, opacity_key]
        )
        code_to_group[int(float(key))] = group_key
        if group_key not in group_meta:
            group_meta[group_key] = {
                "label": label,
                "color": str(color) if color else None,
                "opacity": float(opacity) if opacity is not None else None,
            }

    return code_to_group, group_meta


def categorical_area_summaries(
    arr: np.ndarray,
    valid_mask: np.ndarray,
    affine: Affine,
    raster_crs: rasterio.crs.CRS,
    rendering: dict,
    sample_area_hectares: float,
) -> list[CategoryAreaSummary]:
    """Aggregate sampled categorical raster area by configured legend item.

    Args:
        arr: Sampled raster values with nodata and out-of-geometry cells masked
            to NaN.
        valid_mask: Boolean array where True marks finite, non-nodata sample
            pixels inside the requested geometry.
        affine: Affine transform for the sampled array.
        raster_crs: Coordinate reference system for the sampled raster.
        rendering: Layer rendering metadata from the YAML registry.
        sample_area_hectares: Area represented by the sampled geometry.

    Returns:
        Category summaries in legend order, with duplicate raster codes
        collapsed into the same displayed legend item.
    """

    group_areas, group_meta = categorical_area_totals(
        arr,
        valid_mask,
        affine,
        raster_crs,
        rendering,
    )

    return category_summaries_from_totals(
        group_areas,
        group_meta,
        sample_area_hectares,
    )


def categorical_area_totals(
    arr: np.ndarray,
    valid_mask: np.ndarray,
    affine: Affine,
    raster_crs: rasterio.crs.CRS,
    rendering: dict,
) -> tuple[dict[str, float], dict[str, dict]]:
    """Aggregate sampled categorical raster area totals by legend item."""

    code_to_group, group_meta = legend_groups_for_rendering(rendering)
    group_areas = {group_key: 0.0 for group_key in group_meta}

    if raster_crs.is_projected:
        unit_factor = raster_crs.linear_units_factor[1]
        pixel_area = abs(affine.a * affine.e - affine.b * affine.d)
        default_area = pixel_area * unit_factor**2 / _SQUARE_METERS_PER_HECTARE
        row_area_hectares = {}
        transformer_obj = None
    else:
        default_area = None
        row_area_hectares = {}
        transformer_obj = None
        if raster_crs.to_epsg() != 4326:
            transformer_obj = Transformer.from_crs(
                raster_crs, "EPSG:4326", always_xy=True
            )

    for row_index in np.flatnonzero(np.count_nonzero(valid_mask, axis=1)):
        if default_area is None:
            corners = [
                affine * (0, row_index),
                affine * (1, row_index),
                affine * (1, row_index + 1),
                affine * (0, row_index + 1),
            ]
            xs, ys = zip(*corners)
            if transformer_obj is not None:
                xs, ys = transformer_obj.transform(xs, ys)
            cell_area_m2, _ = _GEOD.polygon_area_perimeter(xs, ys)
            row_area_hectares[row_index] = (
                abs(cell_area_m2) / _SQUARE_METERS_PER_HECTARE
            )

        cell_area_hectares = default_area or row_area_hectares[row_index]
        row_values = arr[row_index, valid_mask[row_index]]
        values, counts = np.unique(row_values.astype("int64"), return_counts=True)

        for value, count in zip(values, counts):
            code = int(value)
            group_key = code_to_group.get(code)
            if group_key is None:
                label = str(code)
                group_key = _LEGEND_KEY_SEPARATOR.join([label, "", ""])
                group_meta[group_key] = {
                    "label": label,
                    "color": None,
                    "opacity": None,
                }
                group_areas[group_key] = 0.0
                code_to_group[code] = group_key

            group_areas[group_key] += int(count) * cell_area_hectares

    return group_areas, group_meta


def category_summaries_from_totals(
    group_areas: dict[str, float],
    group_meta: dict[str, dict],
    sample_area_hectares: float,
) -> list[CategoryAreaSummary]:
    """Build categorical summary response rows from accumulated area totals."""

    if sample_area_hectares <= 0:
        return []

    return [
        CategoryAreaSummary(
            label=meta["label"],
            color=meta["color"],
            opacity=meta["opacity"],
            area_hectares=group_areas[group_key],
            area_percent=group_areas[group_key] / sample_area_hectares * 100.0,
        )
        for group_key, meta in group_meta.items()
        if group_areas.get(group_key, 0.0) > 0
    ]


def summary_from_valid_values(
    vals: Optional[np.ndarray],
    area_hectares: Optional[float],
    sample_area_hectares: Optional[float],
) -> Optional[RasterSummary]:
    """Compute compact summary statistics from valid raster values.

    Args:
        vals: One-dimensional array of finite raster values from the sampled
            geometry.
        area_hectares: Area represented by finite, non-nodata sampled pixels.
        sample_area_hectares: Area represented by the sampled geometry.

    Returns:
        Area, pixel count, sum, and mean for the sampled values, or None when
        no valid values were read for the layer.
    """

    if vals is None:
        return None
    return RasterSummary(
        count=int(vals.size),
        area_hectares=float(area_hectares),
        area_percent=float(area_hectares / sample_area_hectares * 100.0),
        sum=float(np.sum(vals)),
        mean=float(np.mean(vals)),
    )


class PixelValIn(BaseModel):
    """Input model for single-pixel value query.

    Attributes:
        raster_id (str): Identifier of the target raster in the registry.
        lon (float): Longitude (or X) of the query coordinate in `from_crs`.
        lat (float): Latitude (or Y) of the query coordinate in `from_crs`.
        from_crs (str): CRS of the input coordinate, default 'EPSG:4326'.
    """

    raster_id: str
    lon: float
    lat: float
    from_crs: str = "EPSG:4326"


class PixelValOut(BaseModel):
    """Output model for single-pixel value query.

    Attributes:
        raster_id (str): Identifier of the raster queried.
        lon (float): Input longitude (X) in `from_crs`.
        lat (float): Input latitude (Y) in `from_crs`.
        row (Optional[int]): Raster row index (0-based) if within bounds, else None.
        col (Optional[int]): Raster column index (0-based) if within bounds, else None.
        in_bounds (bool): Whether the projected coordinate fell inside raster bounds.
        value (Optional[Union[float, str]]): Pixel value or None if nodata/out-of-bounds/non-finite.
    """

    raster_id: str
    lon: float
    lat: float
    row: Optional[int] = None
    col: Optional[int] = None
    in_bounds: bool = False
    value: Optional[Union[float, str]] = None


class ClipIn(BaseModel):
    """Input model for raster clipping requests.

    This schema defines the inputs required to clip one or two rasters
    using a provided GeoJSON geometry. The geometry can be a Feature or
    FeatureCollection, and is assumed to be in the coordinate reference
    system specified by `from_crs`.

    Attributes:
        raster_id_x (Optional[str]): Identifier for the first raster layer to clip.
            May be `None` if only one raster is needed.
        raster_id_y (Optional[str]): Identifier for the second raster layer to clip.
            May be `None` if only one raster is needed.
        geometry (dict): A GeoJSON dictionary defining the polygon(s) to clip.
            Supports both single `Feature` and `FeatureCollection` formats.
        from_crs (str): Coordinate reference system of the input geometry,
            e.g. 'EPSG:4326'.
        all_touched (bool): Whether to include all pixels touched by the
            geometry boundary (True) or only those whose centers fall within
            the geometry (False). Defaults to False.
    """

    raster_id_x: Optional[str]
    raster_id_y: Optional[str]
    geometry: dict
    from_crs: str
    all_touched: bool = False


def _reproject_geojson_geoms(gj: dict, from_crs: str, to_crs) -> list[dict]:
    """Reproject all geometries in a GeoJSON object to the target CRS.

    Accepts Feature, FeatureCollection, or bare geometry GeoJSON dictionaries
    and returns a list of reprojected geometry dictionaries suitable for use
    with rasterio masking operations. The function supports nested coordinate
    arrays for polygons, multipolygons, and geometry collections.

    Args:
        gj (dict): Input GeoJSON object containing geometries to reproject.
        from_crs (str): EPSG code or PROJ string defining the input CRS.
        to_crs: A `pyproj.CRS` object or similar defining the target CRS.

    Returns:
        list[dict]: A list of GeoJSON geometry dictionaries reprojected to the
        target CRS. If no reprojection is needed, the original geometries are
        returned unchanged.
    """
    if not to_crs or not from_crs or from_crs == to_crs.to_string():
        return _extract_geometries(gj)

    tf = Transformer.from_crs(from_crs, to_crs, always_xy=True)

    def _tx_point(pt):
        if len(pt) == 2:
            x, y = tf.transform(pt[0], pt[1])
            return [x, y]
        x, y, z = pt[0], pt[1], pt[2]
        x2, y2 = tf.transform(x, y)
        return [x2, y2, z]

    def _map_coords(coords):
        if (
            isinstance(coords, (list, tuple))
            and coords
            and isinstance(coords[0], (int, float))
        ):
            return _tx_point(coords)
        return [_map_coords(c) for c in coords]

    def _tx_geometry(geom):
        gtype = geom.get("type")
        if gtype in (
            "Point",
            "MultiPoint",
            "LineString",
            "MultiLineString",
            "Polygon",
            "MultiPolygon",
        ):
            return {
                "type": gtype,
                "coordinates": _map_coords(geom.get("coordinates", [])),
            }
        if gtype == "GeometryCollection":
            return {
                "type": "GeometryCollection",
                "geometries": [_tx_geometry(g) for g in geom.get("geometries", [])],
            }
        return geom

    geoms = _extract_geometries(gj)
    return [_tx_geometry(g) for g in geoms]


def _extract_geometries(geojson_obj: dict) -> list[dict]:
    """Extract all geometry dictionaries from a GeoJSON object.

    This function normalizes various GeoJSON structures—such as a bare geometry,
    a single Feature, or a FeatureCollection—into a flat list of geometry
    dictionaries. Each geometry dictionary in the returned list will contain
    at least a 'type' (e.g., 'Polygon', 'Point') and 'coordinates' key,
    making the result suitable for operations like masking or reprojection.

    Args:
        geojson_obj (dict): A GeoJSON dictionary. This may represent a
            single geometry (e.g., {"type": "Polygon", "coordinates": [...]})
            or a higher-level container such as a Feature or FeatureCollection.

    Returns:
        list[dict]: A list of GeoJSON geometry dictionaries extracted from the
        input object. The list will contain:
            - One geometry if the input is a single Feature or bare geometry.
            - Multiple geometries if the input is a FeatureCollection.
            - An empty list if the input contains no valid geometries.
    """
    geojson_type = geojson_obj.get("type")

    if geojson_type == "Feature":
        geometry = geojson_obj.get("geometry")
        return [geometry] if geometry else []

    if geojson_type == "FeatureCollection":
        geometries = []
        for feature in geojson_obj.get("features", []):
            geometry = feature.get("geometry")
            if geometry:
                geometries.append(geometry)
        return geometries

    # Assume the input is a bare geometry object
    return [geojson_obj]


def _clip_and_write_tif(
    ds: rasterio.io.DatasetReader,
    geoms_ds: list[dict],
    from_crs: str,
    nodata_val,
    all_touched: bool,
    out_path: str,
):
    """Clip a raster dataset to a GeoJSON geometry and write the result to disk.

    Reprojects the input GeoJSON geometry to match the raster's CRS,
    applies the geometry as a spatial mask, and writes the clipped subset
    to a new GeoTIFF file. Supports both single and multi-feature geometries
    and respects the `all_touched` flag for boundary inclusion.

    Args:
        ds (rasterio.io.DatasetReader): Opened raster dataset to clip.
        geoms_ds (list[dict]): List of GeoJSON geometry dictionaries (e.g.,
            Polygons or MultiPolygons) already reprojected to the raster's CRS.
        from_crs (str): CRS string of the input geometry (e.g., 'EPSG:4326').
        nodata_val: NoData value to use for masking. If None, uses the dataset's
            internal NoData value.
        all_touched (bool): Whether to include all pixels touched by the
            geometry boundary.
        out_path (str): Filesystem path where the clipped GeoTIFF will be saved.

    Raises:
        HTTPException: If no valid geometry is provided.
    """
    if not geoms_ds:
        raise HTTPException(status_code=400, detail="No valid geometry provided")

    out_image, out_transform = rio_mask(
        ds,
        geoms_ds,
        crop=True,
        all_touched=bool(all_touched),
        nodata=nodata_val if nodata_val is not None else ds.nodata,
        filled=True,
    )
    out_meta = ds.meta.copy()
    out_meta.update(
        {
            "height": out_image.shape[1],
            "width": out_image.shape[2],
            "transform": out_transform,
            "nodata": nodata_val if nodata_val is not None else ds.nodata,
            "compress": "deflate",
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
            "BIGTIFF": "IF_SAFER",
        }
    )
    with rasterio.open(out_path, "w", **out_meta) as dst:
        dst.write(out_image)


def _load_layers_yaml() -> dict:
    """Load expanded layers YAML config."""
    if not RASTERS_YAML_PATH.exists():
        raise RuntimeError(f"{RASTERS_YAML_PATH} not found")
    raw_yaml = RASTERS_YAML_PATH.read_text()
    return yaml.safe_load(os.path.expandvars(raw_yaml)) or {}


def _load_registry() -> dict:
    """Load the raster layer registry from a YAML configuration file.

    Attempts to read and parse the file defined by the global constant
    `RASTERS_YAML_PATH`. The YAML file may include environment variable
    references, which are expanded before parsing. The function returns the
    dictionary under the top-level key "layers" if present.

    Returns:
        dict: A dictionary of raster layer definitions loaded from the YAML file.

    Raises:
        RuntimeError: If the registry file is missing or cannot be found.
        Exception: For any unexpected error during file reading or YAML parsing.

    """
    y = _load_layers_yaml()

    layers_dict = {}
    for section_key in ("layers", "baseLayers"):
        for k, v in (y.get(section_key, {}) or {}).items():
            layers_dict[k.lower()] = v

    for layer_name, layer_dict in layers_dict.items():
        rendering = layer_dict.get("rendering") or {}
        categories = rendering.get("categories") or {}
        if categories:
            rendering["category_labels"] = {
                int(value): (meta or {}).get("label")
                for value, meta in categories.items()
            }
            layer_dict["rendering"] = rendering

    return layers_dict


REGISTRY = _load_registry()


def _load_sample_vector_config():
    """Load optional configured sample vector metadata from layers.yml."""
    y = _load_layers_yaml()
    config = y.get("sampleVector") or y.get("sample_vector")
    if not config:
        return None

    file_path = config.get("file_path")
    label_field = config.get("label_field")
    if not file_path or not label_field:
        raise RuntimeError("sampleVector requires file_path and label_field")

    return {
        "file_path": file_path,
        "layer": config.get("layer"),
        "label_field": label_field,
        "toggle_label": config.get("toggle_label") or "Select feature",
    }


SAMPLE_VECTOR = _load_sample_vector_config()


def _target_wgs84_srs():
    """Return EPSG:4326 SRS using traditional lon/lat axis order."""
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _open_sample_vector_layer():
    """Open the configured sample vector dataset and layer."""
    if not SAMPLE_VECTOR:
        raise HTTPException(status_code=404, detail="sampleVector is not configured")

    path = SAMPLE_VECTOR["file_path"]
    if not Path(path).exists():
        raise HTTPException(status_code=500, detail=f"sample vector missing: {path}")

    dataset = ogr.Open(path)
    if dataset is None:
        raise HTTPException(status_code=500, detail=f"sample vector unreadable: {path}")

    layer_name = SAMPLE_VECTOR.get("layer")
    layer = dataset.GetLayerByName(layer_name) if layer_name else dataset.GetLayer(0)
    if layer is None:
        detail = (
            f"sample vector layer not found: {layer_name}"
            if layer_name
            else "sample vector has no readable layer"
        )
        raise HTTPException(status_code=500, detail=detail)

    return dataset, layer


def _sample_vector_features():
    """Read configured vector features as EPSG:4326 GeoJSON features."""
    dataset, layer = _open_sample_vector_layer()
    try:
        label_field = SAMPLE_VECTOR["label_field"]
        layer_defn = layer.GetLayerDefn()
        if layer_defn.GetFieldIndex(label_field) < 0:
            raise HTTPException(
                status_code=500,
                detail=f"sample vector label field not found: {label_field}",
            )

        source_srs = layer.GetSpatialRef()
        target_srs = _target_wgs84_srs()
        coord_transform = None
        if source_srs:
            source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            coord_transform = osr.CoordinateTransformation(source_srs, target_srs)

        features = []
        layer.ResetReading()
        for feature in layer:
            label_value = feature.GetField(label_field)
            geometry = feature.GetGeometryRef()
            if label_value is None or geometry is None:
                continue

            geometry = geometry.Clone()
            if coord_transform is not None:
                geometry.Transform(coord_transform)

            envelope = geometry.GetEnvelope()
            label = str(label_value)
            features.append(
                {
                    "id": label,
                    "label": label,
                    "bounds": [
                        envelope[0],
                        envelope[2],
                        envelope[1],
                        envelope[3],
                    ],
                    "type": "Feature",
                    "properties": {label_field: label},
                    "geometry": json.loads(geometry.ExportToJson()),
                }
            )

        return sorted(features, key=lambda item: item["label"].casefold())
    finally:
        dataset = None


def _open_raster(raster_id: str):
    """Open a registered raster dataset and return its metadata.

    Looks up the raster entry from the global `REGISTRY` using the provided
    raster ID, verifies that the file exists, and opens it with Rasterio.
    Returns the dataset handle along with its nodata value and units.

    Args:
        raster_id (str): Identifier of the raster to open, matching an entry
            in `REGISTRY`.

    Returns:
        tuple:
            ds (rasterio.io.DatasetReader): Opened Rasterio dataset.
            nodata (float | None): Nodata value from metadata or dataset.

    Raises:
        HTTPException: If the raster ID is not found in the registry (404) or
            if the raster file does not exist on disk (500).
    """
    meta = REGISTRY.get(raster_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"raster_id not found: {raster_id}")
    path = meta["file_path"]
    if not Path(path).exists():
        raise HTTPException(status_code=500, detail=f"raster file missing: {path}")
    ds = rasterio.open(path)
    nodata = meta.get("nodata", ds.nodata)
    return ds, nodata


app = FastAPI(title="ESSOSC Raster Stats API", version="0.1.0")

# Enable Cross-Origin Resource Sharing (CORS) for the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/stats/jobs/{job_id}", response_model=StatsJobStatusOut)
def stats_job_status(job_id: str):
    """Return progress metadata for a polygon stats job."""
    with _STATS_JOBS_LOCK:
        _cleanup_stats_jobs()
        job = _STATS_JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="stats job not found")
        return _stats_job_snapshot(job)


@app.post("/stats/jobs/{job_id}/cancel", response_model=StatsJobStatusOut)
def cancel_stats_job(job_id: str, req: StatsJobCancelIn):
    """Request cooperative cancellation for a running polygon stats job."""
    with _STATS_JOBS_LOCK:
        _cleanup_stats_jobs()
        job = _STATS_JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="stats job not found")
        if req.session_id and job.session_id != req.session_id:
            raise HTTPException(status_code=403, detail="stats job session mismatch")

        if job.status == "running":
            job.cancel_event.set()
            job.status = "cancelled"
            job.message = "Cancellation requested"
            job.progress = min(job.progress, 0.99)
            job.updated_at = time.time()
            job.done_at = job.updated_at

        return _stats_job_snapshot(job)


@app.get("/sample_vector")
def sample_vector():
    """Return configured sample vector features as EPSG:4326 GeoJSON."""
    if not SAMPLE_VECTOR:
        return {"enabled": False, "features": []}

    return {
        "enabled": True,
        "label_field": SAMPLE_VECTOR["label_field"],
        "toggle_label": SAMPLE_VECTOR["toggle_label"],
        "features": _sample_vector_features(),
    }


def _compute_window(
    ds: rasterio.io.DatasetReader,
    x: float,
    y: float,
    r: int,
    c: int,
    radius_pixels: Optional[int],
    bbox_raster_crs: Optional[Tuple[float, float, float, float]],
) -> Window:
    """Compute a pixel window for reading a raster subset.

    Determines the raster window to read based on either a bounding box in the
    raster's coordinate reference system (CRS) or a radius in pixel units
    around a given pixel center. The resulting window is clamped to the raster
    boundaries and rounded to integer offsets and sizes.

    Args:
        ds (rasterio.io.DatasetReader): Open raster dataset.
        x (float): X coordinate of the center point in raster CRS.
        y (float): Y coordinate of the center point in raster CRS.
        r (int): Row index of the center pixel.
        c (int): Column index of the center pixel.
        radius_pixels (Optional[int]): Radius in pixels around the center pixel.
            If None, only the center pixel is used.
        bbox_raster_crs (Optional[Tuple[float, float, float, float]]): Bounding
            box in the raster CRS as (minx, miny, maxx, maxy). If provided, it
            overrides the pixel-radius method.

    Returns:
        rasterio.windows.Window: Window object representing the pixel region
        to read from the raster.
    """
    if bbox_raster_crs is not None:
        minx, miny, maxx, maxy = bbox_raster_crs
        return (
            from_bounds(minx, miny, maxx, maxy, transform=ds.transform)
            .round_offsets()
            .round_lengths()
        )
    # center + radius in pixel space (inclusive)
    rp = max(0, r - (radius_pixels or 0))
    cp = max(0, c - (radius_pixels or 0))
    rh = min(ds.height, r + (radius_pixels or 0) + 1)
    cw = min(ds.width, c + (radius_pixels or 0) + 1)
    height = rh - rp
    width = cw - cp
    return Window(cp, rp, width, height)


def _area_per_pixel_m2(ds: rasterio.io.DatasetReader) -> Optional[float]:
    """Compute the ground area represented by a single pixel in square meters.

    Calculates the per-pixel area based on the raster’s affine transform,
    assuming a projected coordinate reference system (CRS). The area is
    derived from the absolute value of the product of pixel width and height.
    For north-up rasters, the height term is typically negative, so the
    absolute value ensures a positive result.

    Args:
        ds (rasterio.io.DatasetReader): Open raster dataset.

    Returns:
        Optional[float]: Pixel area in square meters if the CRS is projected;
        otherwise, None.
    """
    try:
        crsproj = ds.crs and ds.crs.is_projected
    except Exception:
        crsproj = False
    if not crsproj:
        return None
    a = ds.transform.a
    e = ds.transform.e
    # e is often negative for north up, so we abs it
    return abs(a * e)


@app.get("/rasters")
def rasters():
    """List all registered raster IDs."""
    return {"rasters": list(REGISTRY.keys())}


def get_best_overview(band):
    """Returns the highest-resolution overview that fits within a memory budget.

    This scans overviews for band 1 (from highest to lowest resolution) and returns
    the first overview whose pixel count fits within 2*28 bytes assuming
    4 bytes per pixel.

    Args:
      raster: A GDAL rasterband.

    Returns:
      A GDAL RasterBand overview (RasterBand) for band 1 that fits the budget, or
      None if no available overview fits.
    """
    max_bytes = 2**28
    element_bytesize = 4
    max_elements = max_bytes / element_bytesize

    for i in range(band.GetOverviewCount()):
        print(f"trying {i}")
        overview = band.GetOverview(i)
        if overview.XSize * overview.YSize < max_elements:
            return overview


@app.post("/stats/minmax", response_model=RasterMinMaxOut)
def minmax_stats(r: RasterMinMaxIn):
    """Compute approximate 5th and 95th percentile values for a raster.

    This endpoint either uses pre-computed low/high values or estimates the
    value range for a given raster by sampling multiple random windows and
    aggregating valid pixel values. The computed percentiles are used as
    approximate minimum and maximum values for visualization or dynamic styling.

    Args:
        r (RasterMinMaxIn): Input model containing the raster identifier (`raster_id`)
            to locate and open the raster file.

    Returns:
        RasterMinMaxOut: Object containing the raster ID along with the estimated
        5th percentile (`min_`) and 95th percentile (`max_`) values.

    Raises:
        HTTPException: If any error occurs while reading the raster or computing
        percentiles. Returns HTTP 500 with detail "Failed to compute min/max".
    """
    try:
        raster_dict = REGISTRY[r.raster_id]
        # use min/max if it exists
        if all(key in raster_dict for key in ("min", "max")):
            return RasterMinMaxOut(
                raster_id=r.raster_id,
                min_=raster_dict["min"],
                max_=raster_dict["max"],
            )
        # fallback is to calculatemanually
        file_path = REGISTRY[r.raster_id]["file_path"]
        raster = gdal.Open(file_path, gdal.GA_ReadOnly)
        band = raster.GetRasterBand(1)
        overview = get_best_overview(band)
        array = overview.ReadAsArray()
        nodata = band.GetNoDataValue()
        if nodata is not None:
            array = array[(array != nodata) & (np.isfinite(array))]
        p5, p95 = np.percentile(array, [5, 95])
        return RasterMinMaxOut(raster_id=r.raster_id, min_=p5, max_=p95)
    except Exception as e:
        logger.exception("minmax_stats failed")
        tb = traceback.format_exc()
        raise HTTPException(
            status_code=500,
            detail={
                "error": str(e),
                "traceback": tb,
            },
        )


def _safe_window_for_geom(dataset, geometry):
    """Compute a rasterio window on a dataset safely enclosing a given geometry.

    Ensures that the resulting window:
    - Falls within the raster bounds
    - Has nonzero dimensions (expands slightly if needed)
    - Falls back to a single pixel window if geometry is too small

    Args:
        dataset (rasterio.io.DatasetReader): Open raster dataset.
        geometry (dict (geojson)): Geometry to bound,
            must be in the same CRS as dataset.

    Returns:
        rasterio.windows.Window: Window enclosing the geometry or a
        fallback pixel window.
    """
    geometry_bounds = geometry.bounds
    try:
        candidate_window = rasterio.windows.from_bounds(
            *geometry_bounds, transform=dataset.transform
        )
        candidate_window = candidate_window.round_offsets().round_lengths()
        candidate_window = candidate_window.intersection(
            Window(0, 0, dataset.width, dataset.height)
        )
    except WindowError:
        return Window(0, 0, 0, 0)

    if int(candidate_window.width) > 0 and int(candidate_window.height) > 0:
        return candidate_window
    # if we get here it's because our geometry is a point or a line
    # or a very thin polygon, so we just try to pad one pixel
    # around it.
    x_res, y_res = map(abs, dataset.res)
    padded_bounds = (
        geometry_bounds[0] - 0.5 * x_res,
        geometry_bounds[1] - 0.5 * y_res,
        geometry_bounds[2] + 0.5 * x_res,
        geometry_bounds[3] + 0.5 * y_res,
    )
    padded_window = rasterio.windows.from_bounds(
        *padded_bounds, transform=dataset.transform
    )
    padded_window = padded_window.round_offsets().round_lengths()
    padded_window = padded_window.intersection(
        Window(0, 0, dataset.width, dataset.height)
    )

    if int(padded_window.width) == 0 or int(padded_window.height) == 0:
        # if we get here it means for some reason our intersection
        # was still 0, so we'll just try to grab a pixel
        centroid_x, centroid_y = (
            geometry.centroid.x,
            geometry.centroid.y,
        )
        row_idx, col_idx = rasterio.transform.rowcol(
            dataset.transform, centroid_x, centroid_y
        )
        row_idx = min(max(row_idx, 0), dataset.height - 1)
        col_idx = min(max(col_idx, 0), dataset.width - 1)
        fallback_window = Window(col_idx, row_idx, 1, 1)
        return fallback_window
    return padded_window


def _shape_for_pixel_budget(win, max_pixels):
    """Helper for determining a max window size for scatter plots.

    Args:
        win (Window): base window size
        max_pixels (int): desired target window pixel size

    returns:
        out_h, out_w (both ints the window that would match max_pixels)
    """
    w = max(1, int(np.ceil(win.width)))
    h = max(1, int(np.ceil(win.height)))
    n = w * h
    if n <= max_pixels:
        return h, w

    scale = np.sqrt(max_pixels / n)
    out_w = max(1, int(np.floor(w * scale)))
    out_h = max(1, int(np.floor(h * scale)))

    if out_w * out_h > max_pixels:
        out_w = max(1, min(out_w, max_pixels // out_h))
        if out_w * out_h > max_pixels:
            out_h = max(1, min(out_h, max_pixels // out_w))

    return out_h, out_w


def _iter_window_chunks(win: Window, chunk_size: int = _GEOMETRY_SCATTER_CHUNK_SIZE):
    """Yield bounded integer windows covering a larger raster window."""
    col_start = int(win.col_off)
    row_start = int(win.row_off)
    col_stop = int(np.ceil(win.col_off + win.width))
    row_stop = int(np.ceil(win.row_off + win.height))

    for row_off in range(row_start, row_stop, chunk_size):
        height = min(chunk_size, row_stop - row_off)
        if height <= 0:
            continue
        for col_off in range(col_start, col_stop, chunk_size):
            width = min(chunk_size, col_stop - col_off)
            if width <= 0:
                continue
            yield Window(col_off, row_off, width, height)


def _window_chunk_count(win: Window, chunk_size: int = _GEOMETRY_SCATTER_CHUNK_SIZE):
    """Return the number of bounded chunks needed to cover a raster window."""
    width = max(0, int(np.ceil(win.col_off + win.width)) - int(win.col_off))
    height = max(0, int(np.ceil(win.row_off + win.height)) - int(win.row_off))
    if width <= 0 or height <= 0:
        return 0
    return int(np.ceil(width / chunk_size) * np.ceil(height / chunk_size))


def _histogram_edges(min_value: float, max_value: float, bins: int) -> np.ndarray:
    """Build stable histogram edges, padding flat-value ranges slightly."""
    if min_value == max_value:
        pad = max(abs(min_value) * 0.001, 0.5)
        min_value -= pad
        max_value += pad
    return np.linspace(min_value, max_value, bins + 1)


def _bounded_sample_append(
    sample: Optional[np.ndarray],
    values: np.ndarray,
    max_points: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Append values to a bounded approximate sample without retaining all pixels."""
    if values.size == 0:
        return sample if sample is not None else np.array([], dtype="float64")

    values_sample = values
    if values_sample.shape[0] > max_points:
        idx = rng.choice(values_sample.shape[0], size=max_points, replace=False)
        values_sample = values_sample[idx]

    if sample is None or sample.size == 0:
        combined = values_sample
    else:
        combined = np.concatenate([sample, values_sample])

    if combined.shape[0] > max_points:
        idx = rng.choice(combined.shape[0], size=max_points, replace=False)
        combined = combined[idx]
    return combined


def _summary_from_accumulator(acc: dict) -> Optional[RasterSummary]:
    """Create a RasterSummary from chunk-accumulated counts and sums."""
    count = int(acc["count"])
    sample_area_hectares = float(acc["sample_area_hectares"])
    area_hectares = float(acc["area_hectares"])
    if count <= 0 or sample_area_hectares <= 0:
        return None
    return RasterSummary(
        count=count,
        area_hectares=area_hectares,
        area_percent=float(area_hectares / sample_area_hectares * 100.0),
        sum=float(acc["sum"]),
        mean=float(acc["sum"] / count),
    )


@app.post("/stats/scatter", response_model=ScatterOut)
def geometry_scatter(scatter_request: GeometryScatterIn):
    job = _register_stats_job(scatter_request.job_id, scatter_request.session_id)
    try:
        _update_stats_job(job, progress=0.01, message="Preparing stats")
        logger.debug(
            "Starting scatter computation: raster_id_x=%r raster_id_y=%r "
            "geometry=%s from_crs=%r histogram_bins=%s max_points=%s "
            "all_touched=%s job_id=%r session_id=%r",
            scatter_request.raster_id_x,
            scatter_request.raster_id_y,
            _geometry_log_summary(scatter_request.geometry),
            scatter_request.from_crs,
            scatter_request.histogram_bins,
            scatter_request.max_points,
            scatter_request.all_touched,
            scatter_request.job_id,
            scatter_request.session_id,
        )

        # init validity flags
        x_valid, y_valid = False, False

        # base geometry (input CRS)
        geom_in_shape = shape(scatter_request.geometry)

        # helper: reproject a geometry from scatter_request.from_crs to ds.crs
        def _geom_in_ds_crs(ds):
            if scatter_request.from_crs != ds.crs.to_string():
                transformer_obj = Transformer.from_crs(
                    scatter_request.from_crs, ds.crs, always_xy=True
                )
                return shp_transform(
                    lambda x, y, z=None: transformer_obj.transform(x, y),
                    geom_in_shape,
                )
            return geom_in_shape

        def _empty_result():
            return {
                "ds": None,
                "nodata": None,
                "hist": None,
                "edges": None,
                "summary": None,
                "categories": None,
                "is_categorical": False,
                "valid": False,
                "sample": None,
                "window": None,
                "geometry": None,
            }

        def _masked_chunk_values(
            ds,
            nodata_val,
            geom_ref_shape,
            win,
            all_touched,
            *,
            progress_start=None,
            progress_end=None,
            progress_message=None,
        ):
            total_chunks = _window_chunk_count(win)
            for chunk_index, chunk_win in enumerate(_iter_window_chunks(win), start=1):
                _raise_if_stats_job_cancelled(job)
                if (
                    total_chunks > 0
                    and progress_start is not None
                    and progress_end is not None
                ):
                    fraction = (chunk_index - 1) / total_chunks
                    _update_stats_job(
                        job,
                        progress=progress_start
                        + (progress_end - progress_start) * fraction,
                        message=progress_message,
                    )
                data = ds.read(1, window=chunk_win, masked=False).astype(
                    "float64", copy=False
                )
                affine = ds.window_transform(chunk_win)
                mask = geometry_mask(
                    [mapping(geom_ref_shape)],
                    transform=affine,
                    invert=True,
                    out_shape=data.shape,
                    all_touched=bool(all_touched),
                )
                if not np.any(mask):
                    continue

                if nodata_val is not None:
                    data = np.where(np.isclose(data, nodata_val), np.nan, data)

                valid_mask = mask & np.isfinite(data)
                yield data, mask, valid_mask, affine, chunk_win
                if (
                    total_chunks > 0
                    and progress_start is not None
                    and progress_end is not None
                ):
                    fraction = chunk_index / total_chunks
                    _update_stats_job(
                        job,
                        progress=progress_start
                        + (progress_end - progress_start) * fraction,
                        message=progress_message,
                    )
                _raise_if_stats_job_cancelled(job)

        def _merge_category_totals(group_areas, group_meta, chunk_areas, chunk_meta):
            for group_key, meta in chunk_meta.items():
                group_meta.setdefault(group_key, meta)
                group_areas[group_key] = group_areas.get(group_key, 0.0) + float(
                    chunk_areas.get(group_key, 0.0)
                )

        # helper: read, clip-to-geometry, and compute 1D histogram
        def _read_clip_hist(
            raster_id,
            bins,
            all_touched,
            progress_start,
            progress_end,
        ):
            if not raster_id:
                return _empty_result()

            _raise_if_stats_job_cancelled(job)
            result = _empty_result()
            ds = None
            nodata_val = None
            hist = None
            edges = None
            category_summaries = None
            is_categorical = False
            valid = False
            try:
                layer_cfg = REGISTRY.get(raster_id.lower(), {})
                rendering = layer_cfg.get("rendering") or {}
                is_categorical = (
                    str(rendering.get("type", "")).lower() == "categorical"
                )
                ds, nodata_val = _open_raster(raster_id)
                if nodata_val is None:
                    nodata_val = rendering.get("nodata")
                geom_ref_shape = _geom_in_ds_crs(ds)
                result.update(
                    {
                        "ds": ds,
                        "nodata": nodata_val,
                        "is_categorical": is_categorical,
                        "geometry": geom_ref_shape,
                    }
                )

                win = _safe_window_for_geom(ds, geom_ref_shape)
                result["window"] = win
                if int(win.width) <= 0 or int(win.height) <= 0:
                    return result

                progress_span = progress_end - progress_start
                first_pass_end = (
                    progress_start + progress_span * 0.7
                    if not is_categorical
                    else progress_end
                )
                acc = {
                    "count": 0,
                    "sum": 0.0,
                    "area_hectares": 0.0,
                    "sample_area_hectares": 0.0,
                }
                min_value = None
                max_value = None
                sample_values = None
                sample_rng = np.random.default_rng(0)
                group_areas = {}
                group_meta = {}

                for data, mask, valid_mask, affine, _chunk_win in _masked_chunk_values(
                    ds,
                    nodata_val,
                    geom_ref_shape,
                    win,
                    all_touched,
                    progress_start=progress_start,
                    progress_end=first_pass_end,
                    progress_message=f"Reading {raster_id}",
                ):
                    acc["sample_area_hectares"] += valid_area_hectares(
                        mask,
                        affine,
                        ds.crs,
                    )
                    if not np.any(valid_mask):
                        continue

                    vals = data[valid_mask]
                    acc["count"] += int(vals.size)
                    acc["sum"] += float(np.sum(vals))
                    acc["area_hectares"] += valid_area_hectares(
                        valid_mask,
                        affine,
                        ds.crs,
                    )
                    if is_categorical:
                        chunk_areas, chunk_meta = categorical_area_totals(
                            data,
                            valid_mask,
                            affine,
                            ds.crs,
                            rendering,
                        )
                        _merge_category_totals(
                            group_areas,
                            group_meta,
                            chunk_areas,
                            chunk_meta,
                        )
                    else:
                        chunk_min = float(np.min(vals))
                        chunk_max = float(np.max(vals))
                        min_value = (
                            chunk_min
                            if min_value is None
                            else min(min_value, chunk_min)
                        )
                        max_value = (
                            chunk_max
                            if max_value is None
                            else max(max_value, chunk_max)
                        )
                        sample_values = _bounded_sample_append(
                            sample_values,
                            vals,
                            scatter_request.max_points,
                            sample_rng,
                        )

                valid = acc["count"] > 0
                summary = _summary_from_accumulator(acc)

                if valid and is_categorical:
                    category_summaries = category_summaries_from_totals(
                        group_areas,
                        group_meta,
                        acc["sample_area_hectares"],
                    )
                elif valid:
                    edges = _histogram_edges(min_value, max_value, bins)
                    hist = np.zeros(bins, dtype="int64")
                    for (
                        data,
                        _mask,
                        valid_mask,
                        _affine,
                        _chunk_win,
                    ) in _masked_chunk_values(
                        ds,
                        nodata_val,
                        geom_ref_shape,
                        win,
                        all_touched,
                        progress_start=first_pass_end,
                        progress_end=progress_end,
                        progress_message=f"Building histogram for {raster_id}",
                    ):
                        if not np.any(valid_mask):
                            continue
                        vals = data[valid_mask]
                        hist += np.histogram(vals, bins=edges)[0].astype("int64")

                result.update(
                    {
                        "hist": hist,
                        "edges": edges,
                        "summary": summary,
                        "categories": category_summaries,
                        "valid": valid,
                        "sample": sample_values,
                    }
                )
            except ValueError as e:
                logger.warning(f"{e} error on _read_clip_hist {raster_id}")

            return result

        # loop over both rasters (read, clip, 1D hist)
        bins = scatter_request.histogram_bins
        results = {
            "x": _read_clip_hist(
                scatter_request.raster_id_x,
                bins,
                scatter_request.all_touched,
                0.05,
                0.4,
            ),
            "y": _read_clip_hist(
                scatter_request.raster_id_y,
                bins,
                scatter_request.all_touched,
                0.4,
                0.75,
            ),
        }

        x_valid = results["x"]["valid"]
        y_valid = results["y"]["valid"]
        x_hist_valid = x_valid and not results["x"]["is_categorical"]
        y_hist_valid = y_valid and not results["y"]["is_categorical"]

        # prepare outputs
        hist2d = None
        x_edges_out = (
            results["x"]["edges"] if results["x"]["edges"] is not None else None
        )
        y_edges_out = (
            results["y"]["edges"] if results["y"]["edges"] is not None else None
        )
        x_plot, y_plot = None, None
        valid_pixels = None

        # compute 2D histogram on overlapping pixels if both are valid
        if x_hist_valid and y_hist_valid:
            x_ds = results["x"]["ds"]
            y_ds = results["y"]["ds"]
            y_nodata = results["y"]["nodata"]
            x_geom = results["x"]["geometry"]
            x_win = results["x"]["window"]
            x_edges_2d = results["x"]["edges"]
            y_edges_2d = results["y"]["edges"]
            hist2d = np.zeros(
                (len(x_edges_2d) - 1, len(y_edges_2d) - 1),
                dtype="int64",
            )
            paired_chunk_total = _window_chunk_count(x_win)
            pair_sample = None
            pair_sample_rng = np.random.default_rng(0)
            n_pairs = 0

            def _read_y_on_x_grid(x_affine, x_shape):
                try:
                    ul = x_affine * (0, 0)
                    lr = x_affine * (x_shape[1], x_shape[0])
                    minx, maxx = sorted([ul[0], lr[0]])
                    miny, maxy = sorted([ul[1], lr[1]])

                    minx_y, miny_y, maxx_y, maxy_y = transform_bounds(
                        x_ds.crs, y_ds.crs, minx, miny, maxx, maxy, densify_pts=0
                    )

                    y_win = from_bounds(
                        minx_y, miny_y, maxx_y, maxy_y, transform=y_ds.transform
                    )
                    y_win = (
                        y_win.round_offsets()
                        .round_lengths()
                        .intersection(Window(0, 0, y_ds.width, y_ds.height))
                    )
                    if int(y_win.width) <= 0 or int(y_win.height) <= 0:
                        return np.full(x_shape, np.nan, dtype="float64")

                    y_src = y_ds.read(1, window=y_win, masked=False).astype(
                        "float64", copy=False
                    )
                    if y_nodata is not None:
                        y_src = np.where(np.isclose(y_src, y_nodata), np.nan, y_src)
                    y_affine = y_ds.window_transform(y_win)

                    y_on_xgrid = np.full(x_shape, np.nan, dtype="float64")
                    reproject(
                        source=y_src,
                        destination=y_on_xgrid,
                        src_transform=y_affine,
                        src_crs=y_ds.crs,
                        src_nodata=np.nan if y_nodata is not None else None,
                        dst_transform=x_affine,
                        dst_crs=x_ds.crs,
                        dst_nodata=np.nan,
                        resampling=Resampling.nearest,
                        num_threads=0,
                    )
                    return y_on_xgrid
                except WindowError:
                    return np.full(x_shape, np.nan, dtype="float64")

            for (
                pair_chunk_index,
                (data, _mask, valid_mask, affine, _chunk_win),
            ) in enumerate(_masked_chunk_values(
                x_ds,
                results["x"]["nodata"],
                x_geom,
                x_win,
                scatter_request.all_touched,
                progress_start=0.75,
                progress_end=0.98,
                progress_message="Building paired scatter",
            ), start=1):
                _raise_if_stats_job_cancelled(job)
                if not np.any(valid_mask):
                    continue
                y_on_xgrid = _read_y_on_x_grid(affine, data.shape)
                finite_mask = valid_mask & np.isfinite(y_on_xgrid)
                if not np.any(finite_mask):
                    continue

                x_pairs = data[finite_mask]
                y_pairs = y_on_xgrid[finite_mask]
                n_pairs += int(x_pairs.size)
                chunk_hist2d, _x_edges_unused, _y_edges_unused = np.histogram2d(
                    x_pairs,
                    y_pairs,
                    bins=[x_edges_2d, y_edges_2d],
                )
                hist2d += chunk_hist2d.astype("int64")

                chunk_pairs = np.column_stack([x_pairs, y_pairs])
                pair_sample = _bounded_sample_append(
                    pair_sample,
                    chunk_pairs,
                    scatter_request.max_points,
                    pair_sample_rng,
                )
                if paired_chunk_total > 0:
                    _update_stats_job(
                        job,
                        progress=0.75
                        + (0.98 - 0.75) * (pair_chunk_index / paired_chunk_total),
                        message="Building paired scatter",
                    )

            x_edges_out, y_edges_out = x_edges_2d, y_edges_2d
            valid_pixels = n_pairs
            if n_pairs > 0:
                x_plot = pair_sample[:, 0]
                y_plot = pair_sample[:, 1]

        # if only one is valid, prepare 1D scatter arrays directly from that raster
        if x_hist_valid ^ y_hist_valid:  # XOR
            side = "x" if x_hist_valid else "y"
            sampled = results[side]["sample"]
            x_plot = sampled if side == "x" else None
            y_plot = sampled if side == "y" else None

        response = ScatterOut(
            raster_id_x=scatter_request.raster_id_x,
            raster_id_y=scatter_request.raster_id_y,
            x=x_plot.tolist() if x_plot is not None else None,
            y=y_plot.tolist() if y_plot is not None else None,
            hist2d=hist2d.tolist() if hist2d is not None else None,
            x_edges=(
                x_edges_out.tolist()
                if isinstance(x_edges_out, np.ndarray)
                else (x_edges_out if x_edges_out is None else list(x_edges_out))
            ),
            y_edges=(
                y_edges_out.tolist()
                if isinstance(y_edges_out, np.ndarray)
                else (y_edges_out if y_edges_out is None else list(y_edges_out))
            ),
            hist1d_x=(
                results["x"]["hist"].tolist()
                if results["x"]["hist"] is not None
                else None
            ),
            hist1d_y=(
                results["y"]["hist"].tolist()
                if results["y"]["hist"] is not None
                else None
            ),
            x_summary=results["x"]["summary"] if x_hist_valid else None,
            y_summary=results["y"]["summary"] if y_hist_valid else None,
            x_categories=results["x"]["categories"],
            y_categories=results["y"]["categories"],
            valid_pixels=valid_pixels,
            geometry=scatter_request.geometry,
        )
        _update_stats_job(
            job,
            status="completed",
            progress=1.0,
            message="Stats complete",
        )
        return response

    except HTTPException:
        _update_stats_job(job, status="failed", message="Stats failed")
        logger.exception("scatter stats failed")
        raise
    except StatsJobCancelled:
        logger.info("scatter stats cancelled: job_id=%r", scatter_request.job_id)
        raise HTTPException(status_code=499, detail="Stats job cancelled")
    except Exception as e:
        _update_stats_job(
            job,
            status="failed",
            message="Stats failed",
            error=f"{type(e).__name__}: {e}",
        )
        logger.exception("scatter stats failed")
        raise HTTPException(
            status_code=500,
            detail=f"Scatter computation failed: {type(e).__name__}: {e}",
        )


@app.post("/stats/pixel_val", response_model=PixelValOut)
def pixel_val(req: PixelValIn):
    """Return the value of the pixel containing a given coordinate.

    Projects the (lon, lat) from `from_crs` to the raster CRS, checks bounds,
    and returns the nearest pixel value. Nodata or non-finite values are returned
    as `None`. If the point is out of bounds, `in_bounds=False` with `value=None`.
    """
    try:
        ds, nodata = _open_raster(req.raster_id)

        if req.from_crs and ds.crs and req.from_crs != ds.crs.to_string():
            tf = Transformer.from_crs(req.from_crs, ds.crs, always_xy=True)
            x, y = tf.transform(req.lon, req.lat)
        else:
            x, y = req.lon, req.lat

        r, c = rasterio.transform.rowcol(ds.transform, x, y)
        if r < 0 or c < 0 or r >= ds.height or c >= ds.width:
            return PixelValOut(
                raster_id=req.raster_id,
                lon=req.lon,
                lat=req.lat,
                row=None,
                col=None,
                in_bounds=False,
                value=None,
            )

        win = Window(c, r, 1, 1)
        arr = ds.read(1, window=win, masked=False)
        v = float(arr[0, 0])

        if (nodata is not None and np.isclose(v, nodata)) or (not np.isfinite(v)):
            val = None
        else:
            val = v

        layer_cfg = REGISTRY.get(req.raster_id.lower(), {})
        category_labels = (layer_cfg.get("rendering") or {}).get(
            "category_labels"
        ) or {}
        if val is not None and category_labels:
            label = category_labels.get(int(val))
            if label is not None:
                val = label

        return PixelValOut(
            raster_id=req.raster_id,
            lon=req.lon,
            lat=req.lat,
            row=int(r),
            col=int(c),
            in_bounds=True,
            value=val,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("pixel_val failed")
        raise HTTPException(
            status_code=500,
            detail=f"Pixel value query failed: {type(e).__name__}: {e}",
        )


@app.post("/download/clip")
def download_clip(req: ClipIn):
    try:
        if not req.raster_id_x and not req.raster_id_y:
            raise HTTPException(
                status_code=400,
                detail="At least one of raster_id_x or raster_id_y must be provided",
            )

        x_ds = x_nodata = y_ds = y_nodata = None
        if req.raster_id_x:
            x_ds, x_nodata = _open_raster(req.raster_id_x)
        if req.raster_id_y:
            y_ds, y_nodata = _open_raster(req.raster_id_y)

        ref_ds = x_ds or y_ds
        if ref_ds is None:
            raise HTTPException(status_code=400, detail="No valid raster provided")

        geom_ref = _reproject_geojson_geoms(req.geometry, req.from_crs, ref_ds.crs)
        ts = datetime.utcnow().strftime("%Y_%m_%d_%H_%M_%S")
        name_parts = []
        if req.raster_id_x:
            name_parts.append(req.raster_id_x)
        if req.raster_id_y:
            name_parts.append(req.raster_id_y)
        base_name = "_".join(name_parts) if name_parts else "clip"
        zip_name = f"{base_name}_{ts}.zip"

        temp_dir = tempfile.mkdtemp(prefix="essosc_clip_")
        out_paths = []

        if req.raster_id_x:
            out_x = Path(temp_dir) / f"{req.raster_id_x}_{ts}.tif"
            _clip_and_write_tif(
                x_ds,
                geom_ref,
                req.from_crs,
                x_nodata,
                req.all_touched,
                str(out_x),
            )
            out_paths.append(out_x)

        if req.raster_id_y:
            out_y = Path(temp_dir) / f"{req.raster_id_y}_{ts}.tif"
            _clip_and_write_tif(
                y_ds,
                geom_ref,
                req.from_crs,
                y_nodata,
                req.all_touched,
                str(out_y),
            )
            out_paths.append(out_y)

        zip_path = Path(temp_dir) / zip_name
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in out_paths:
                zf.write(p, arcname=p.name)

        # close datasets before returning
        for d in (x_ds, y_ds):
            try:
                if d:
                    d.close()
            except Exception:
                pass

        def _cleanup():
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=zip_name,
            background=BackgroundTask(_cleanup),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("download_clip failed")
        raise HTTPException(
            status_code=500,
            detail=f"Clip download failed: {type(e).__name__}: {e}",
        )
