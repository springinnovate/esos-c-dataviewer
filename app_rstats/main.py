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

from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List
import logging
import os
import shutil
import tempfile
import zipfile

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from osgeo import gdal
from pydantic import BaseModel
from pyproj import Transformer
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
import traceback
import yaml

load_dotenv()

RASTERS_YAML_PATH = Path(os.getenv("RASTERS_YAML_PATH"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


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


class ScatterOut(BaseModel):
    """Output model for scatterplot and histogram statistics between two raster layers.

    This model represents the result of a bivariate comparison between two raster datasets
    (raster_id_x and raster_id_y) within a specified window or geometry. Many of the
    statistical fields are optional and may be `None` if the window did not cover any
    valid part of the raster.

    Attributes:
        raster_id_x (str): Identifier for the X-axis raster layer.
        raster_id_y (str): Identifier for the Y-axis raster layer.
        n_pairs (int): Total number of paired pixel values sampled.
        x (Optional[List[float]]): List of X-axis pixel values, or None if unavailable.
        y (Optional[List[float]]): List of Y-axis pixel values, or None if unavailable.
        hist2d (Optional[List[List[int]]]): 2D histogram counts, or None if unavailable.
        x_edges (Optional[List[float]]): Bin edges for the X-axis histogram, or None if unavailable.
        y_edges (Optional[List[float]]): Bin edges for the Y-axis histogram, or None if unavailable.
        pearson_r (Optional[float]): Pearson correlation coefficient, or None if not computed.
        slope (Optional[float]): Linear regression slope (Y on X), or None if not computed.
        intercept (Optional[float]): Linear regression intercept, or None if not computed.
        pixels_sampled (Optional[int]): Number of pixels included in the window mask, or None if not applicable.
        valid_pixels (Optional[int]): Number of valid (non-null) paired pixels, or None if not available.
        coverage_ratio (Optional[float]): Ratio of valid pixels to total mask pixels, or None if not available.
        geometry (dict): GeoJSON-like geometry defining the analysis window.
    """

    raster_id_x: Optional[str]
    raster_id_y: Optional[str]
    n_pairs: int
    x: Optional[List[float]] = None
    y: Optional[List[float]] = None
    hist2d: Optional[List[List[int]]] = None
    hist1d_x: Optional[List[int]] = None
    hist1d_y: Optional[List[int]] = None
    x_edges: Optional[List[float]] = None
    y_edges: Optional[List[float]] = None
    pearson_r: Optional[float] = None
    slope: Optional[float] = None
    intercept: Optional[float] = None
    pixels_sampled: Optional[int] = None
    valid_pixels: Optional[int] = None
    coverage_ratio: Optional[float] = None
    geometry: dict


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
        value (Optional[float]): Pixel value or None if nodata/out-of-bounds/non-finite.
    """

    raster_id: str
    lon: float
    lat: float
    row: Optional[int] = None
    col: Optional[int] = None
    in_bounds: bool = False
    value: Optional[float] = None


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
                "geometries": [
                    _tx_geometry(g) for g in geom.get("geometries", [])
                ],
            }
        return geom

    geoms = _extract_geometries(gj)
    return [_tx_geometry(g) for g in geoms]


def _extract_geometries(gj: dict) -> list[dict]:
    t = gj.get("type")
    if t == "Feature":
        g = gj.get("geometry")
        return [g] if g else []
    if t == "FeatureCollection":
        out = []
        for f in gj.get("features", []):
            g = f.get("geometry")
            if g:
                out.append(g)
        return out
    # assume a bare geometry
    return [gj]


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
        raise HTTPException(
            status_code=400, detail="No valid geometry provided"
        )

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
    if not RASTERS_YAML_PATH.exists():
        raise RuntimeError("rasters.yml not found")
    raw_yaml = RASTERS_YAML_PATH.read_text()
    expanded_yaml = os.path.expandvars(raw_yaml)
    y = yaml.safe_load(expanded_yaml)
    # geoserver expects all the raster ids to be lowercase
    layers_dict = {k.lower(): v for k, v in y.get("layers", {}).items()}
    logger.warning(
        "this is a hack to use the processed layers, fix in issue #57"
    )
    for layer_dict in layers_dict.values():
        layer_dict["file_path"] = layer_dict["file_path"].replace(
            "rasters", "processed_rasters"
        )

    return layers_dict


REGISTRY = _load_registry()


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
        raise HTTPException(
            status_code=404, detail=f"raster_id not found: {raster_id}"
        )
    path = meta["file_path"]
    if not Path(path).exists():
        raise HTTPException(
            status_code=500, detail=f"raster file missing: {path}"
        )
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


@app.post("/stats/minmax", response_model=RasterMinMaxOut)
def minmax_stats(r: RasterMinMaxIn):
    """Compute approximate 5th and 95th percentile values for a raster.

    This endpoint estimates the low and high value range for a given raster by
    sampling multiple random windows and aggregating valid pixel values.
    The computed percentiles are used as approximate minimum and maximum values
    for visualization or dynamic styling.

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
        file_path = REGISTRY[r.raster_id]["file_path"]
        logger.debug(f"stats on this file: {file_path}")
        raster = gdal.Open(file_path, gdal.GA_ReadOnly)
        band = raster.GetRasterBand(1)
        n_ovr = band.GetOverviewCount()
        overview = band.GetOverview(n_ovr - 1)
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
    candidate_window = rasterio.windows.from_bounds(
        *geometry_bounds, transform=dataset.transform
    )
    candidate_window = candidate_window.round_offsets().round_lengths()
    candidate_window = candidate_window.intersection(
        Window(0, 0, dataset.width, dataset.height)
    )

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


@app.post("/stats/scatter", response_model=ScatterOut)
def geometry_scatter(scatter_request: GeometryScatterIn):
    try:
        logger.debug("Opening rasters")
        x_ds, x_nodata_val = (
            _open_raster(scatter_request.raster_id_x)
            if scatter_request.raster_id_x
            else (None, None)
        )
        y_ds, y_nodata_val = (
            _open_raster(scatter_request.raster_id_y)
            if scatter_request.raster_id_y
            else (None, None)
        )

        logger.debug("Shaping geometry")
        geom_in_shape = shape(scatter_request.geometry)

        reference_ds = x_ds or y_ds
        if scatter_request.from_crs != reference_ds.crs.to_string():
            logger.debug("Reprojecting geometry to reference raster CRS")
            transformer_obj = Transformer.from_crs(
                scatter_request.from_crs, reference_ds.crs, always_xy=True
            )
            geom_ref_shape = shp_transform(
                lambda x, y, z=None: transformer_obj.transform(x, y),
                geom_in_shape,
            )
        else:
            geom_ref_shape = geom_in_shape

        def read_masked_array(ds, nodata_val, geom_shape):
            win = _safe_window_for_geom(ds, geom_shape)
            data = ds.read(1, window=win, boundless=True, masked=False).astype(
                "float64", copy=False
            )
            affine = ds.window_transform(win)
            mask = geometry_mask(
                [mapping(geom_shape)],
                transform=affine,
                invert=True,
                out_shape=data.shape,
                all_touched=bool(scatter_request.all_touched),
            )
            if nodata_val is not None:
                data = np.where(np.isclose(data, nodata_val), np.nan, data)
            return np.where(mask, data, np.nan), affine, mask

        x_arr, x_affine, mask = (None, None, None)
        if x_ds:
            x_arr, x_affine, mask = read_masked_array(
                x_ds, x_nodata_val, geom_ref_shape
            )

        y_arr, y_on_xgrid_masked = (None, None)
        if y_ds and x_ds:
            try:
                logger.debug("Transforming bounds and reprojecting Y to X grid")
                ul = x_affine * (0, 0)
                lr = x_affine * (x_arr.shape[1], x_arr.shape[0])
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
                y_src = y_ds.read(1, window=y_win, masked=False).astype(
                    "float64", copy=False
                )
                y_affine = y_ds.window_transform(y_win)
                y_on_xgrid = np.full(x_arr.shape, np.nan, dtype="float64")
                reproject(
                    source=y_src,
                    destination=y_on_xgrid,
                    src_transform=y_affine,
                    src_crs=y_ds.crs,
                    src_nodata=y_nodata_val,
                    dst_transform=x_affine,
                    dst_crs=x_ds.crs,
                    dst_nodata=np.nan,
                    resampling=Resampling.nearest,
                    num_threads=0,
                )
                y_on_xgrid_masked = np.where(mask, y_on_xgrid, np.nan)
            except WindowError:
                pass

        elif y_ds:
            y_arr, y_affine, mask = read_masked_array(
                y_ds, y_nodata_val, geom_ref_shape
            )

        x_vals = x_arr[np.isfinite(x_arr)] if x_arr is not None else None
        y_vals = y_arr[np.isfinite(y_arr)] if y_arr is not None else None

        if x_arr is not None and y_on_xgrid_masked is not None:
            finite_mask = np.isfinite(x_arr) & np.isfinite(y_on_xgrid_masked)
            x_vals = x_arr[finite_mask]
            y_vals = y_on_xgrid_masked[finite_mask]
        elif x_arr is not None:
            x_vals = x_arr[np.isfinite(x_arr)]
        elif y_arr is not None:
            y_vals = y_arr[np.isfinite(y_arr)]

        x_plot, y_plot = None, None
        n_pairs = len(x_vals) if x_vals is not None else len(y_vals)
        if n_pairs == 0:
            return ScatterOut(
                raster_id_x=scatter_request.raster_id_x,
                raster_id_y=scatter_request.raster_id_y,
                n_pairs=0,
                pixels_sampled=(
                    int(np.count_nonzero(mask)) if mask is not None else 0
                ),
                valid_pixels=0,
                coverage_ratio=0.0,
                geometry=scatter_request.geometry,
            )

        if n_pairs > scatter_request.max_points:
            idx = np.random.default_rng(0).choice(
                n_pairs, size=scatter_request.max_points, replace=False
            )
            if x_vals is not None:
                x_plot = x_vals[idx]
            if y_vals is not None:
                y_plot = y_vals[idx]
        else:
            x_plot = x_vals
            y_plot = y_vals

        hist2d, x_edges, y_edges = None, None, None
        hist1d_x, hist1d_y = None, None

        if x_vals is not None:
            hist1d_x, x_edges = np.histogram(
                x_vals, bins=scatter_request.histogram_bins
            )
            hist1d_x = hist1d_x.astype("int64")
            x_plot = x_plot.astype("float64")
        if y_vals is not None:
            hist1d_y, y_edges = np.histogram(
                y_vals, bins=scatter_request.histogram_bins
            )
            hist1d_y = hist1d_y.astype("int64")
            y_plot = y_plot.astype("float64")
        if x_vals is not None and y_vals is not None:
            hist2d, x_edges, y_edges = np.histogram2d(
                x_vals, y_vals, bins=scatter_request.histogram_bins
            )
            hist2d = hist2d.astype("int64")

        pearson_r = None
        slope = None
        intercept = None
        if x_vals is not None and y_vals is not None and n_pairs > 1:
            x_std, y_std = np.std(x_vals), np.std(y_vals)
            if x_std != 0 and y_std != 0:
                corr = np.corrcoef(x_vals, y_vals)[0, 1]
                pearson_r = float(corr) if np.isfinite(corr) else None
                design = np.vstack([x_vals, np.ones_like(x_vals)]).T
                slope, intercept = np.linalg.lstsq(design, y_vals, rcond=None)[
                    0
                ]
                slope = float(slope)
                intercept = float(intercept)

        total_mask_pixels = (
            int(np.count_nonzero(mask)) if mask is not None else 0
        )
        valid_pixels = n_pairs
        logger.debug(f"this is the scatter requets: {scatter_request}")
        return ScatterOut(
            raster_id_x=scatter_request.raster_id_x,
            raster_id_y=scatter_request.raster_id_y,
            n_pairs=n_pairs,
            x=x_plot.tolist() if x_plot is not None else None,
            y=y_plot.tolist() if y_plot is not None else None,
            hist2d=hist2d.tolist() if hist2d is not None else None,
            x_edges=x_edges.tolist() if x_edges is not None else None,
            y_edges=y_edges.tolist() if y_edges is not None else None,
            hist1d_x=hist1d_x.tolist() if hist1d_x is not None else None,
            hist1d_y=hist1d_y.tolist() if hist1d_y is not None else None,
            pearson_r=pearson_r,
            slope=slope,
            intercept=intercept,
            pixels_sampled=total_mask_pixels,
            valid_pixels=valid_pixels,
            coverage_ratio=(
                float(valid_pixels / total_mask_pixels)
                if total_mask_pixels
                else 0.0
            ),
            geometry=scatter_request.geometry,
        )

    except HTTPException:
        logger.exception("scatter stats failed")
        raise
    except Exception as e:
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

        # project input coordinate to raster CRS if needed
        if req.from_crs and ds.crs and req.from_crs != ds.crs.to_string():
            tf = Transformer.from_crs(req.from_crs, ds.crs, always_xy=True)
            x, y = tf.transform(req.lon, req.lat)
        else:
            x, y = req.lon, req.lat

        # compute row/col and check bounds
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

        # read single pixel
        win = Window(c, r, 1, 1)
        arr = ds.read(1, window=win, masked=False)
        v = float(arr[0, 0])

        # nodata / non-finite -> None
        if (nodata is not None and np.isclose(v, nodata)) or (
            not np.isfinite(v)
        ):
            val = None
        else:
            val = v

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
            raise HTTPException(
                status_code=400, detail="No valid raster provided"
            )

        geom_ref = _reproject_geojson_geoms(
            req.geometry, req.from_crs, ref_ds.crs
        )
        ts = datetime.utcnow().strftime("%Y_%m_%d_%S")
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
        with zipfile.ZipFile(
            zip_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as zf:
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
