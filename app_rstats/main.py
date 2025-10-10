"""
ESSOSC Raster Stats API
-----------------------
Main entrypoint for the raster statistics microservice.

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

from pathlib import Path
from typing import Literal, Optional, Dict, Tuple, Any
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pyproj import Transformer
from rasterio.features import geometry_mask
from rasterio.transform import rowcol
from rasterio.windows import from_bounds, Window
from shapely.geometry import shape, mapping, box
from shapely.ops import transform as shp_transform
import numpy as np
import rasterio
import yaml

load_dotenv()

RASTERS_YAML_PATH = Path(os.getenv("RASTERS_YAML_PATH"))


class PixelStatsIn(BaseModel):
    """Input model for single-pixel raster queries.

    Attributes:
        raster_id (str): Identifier of the target raster in the registry.
        lon (float): Longitude of the target point (in the input CRS).
        lat (float): Latitude of the target point (in the input CRS).
        crs (str): Coordinate reference system of the input point, default is 'EPSG:4326'.
    """

    raster_id: str
    lon: float
    lat: float
    crs: str = Field(default="EPSG:4326")  # incoming coordinates


class PixelWindowStatsIn(BaseModel):
    """Input model for window-based raster statistics.

    Attributes:
        raster_id (str): Identifier of the target raster in the registry.
        lon (float): Longitude of the window center point (in the input CRS).
        lat (float): Latitude of the window center point (in the input CRS).
        crs (str): Coordinate reference system of the input point, default is 'EPSG:4326'.
        radius_pixels (Optional[int]): Radius in pixels around the center for sampling.
        bbox (Optional[Tuple[float, float, float, float]]): Bounding box in the input CRS
            defined as (minx, miny, maxx, maxy).
        histogram_bins (int): Number of bins for histogram calculation, default is 16.
        histogram_range (Optional[Tuple[float, float]]): Range (min, max) for histogram,
            or None to compute automatically.
    """

    raster_id: str
    lon: float
    lat: float
    crs: str = Field(default="EPSG:4326")

    # one of: radius in pixels around the center, or a bbox in input CRS units
    radius_pixels: Optional[int] = Field(default=None, ge=0)
    bbox: Optional[Tuple[float, float, float, float]] = Field(
        default=None,
        description='minx, miny, maxx, maxy in the same CRS as "crs"',
    )

    # histogram controls
    histogram_bins: int = Field(default=16, ge=1)
    histogram_range: Optional[Tuple[float, float]] = None  # min,max or None to auto


class GeometryStatsIn(BaseModel):
    """Input model for geometry-based zonal statistics.

    Attributes:
        raster_id (str): Identifier of the target raster in the registry.
        geometry (dict): GeoJSON geometry defining the analysis area.
        from_crs (str): CRS of the input geometry, default is 'EPSG:4326'.
        reducer (Literal): Statistical operation to apply over the geometry,
            one of 'mean', 'sum', 'min', 'max', 'std', 'count', 'median', 'histogram'.
        histogram_bins (Optional[int]): Number of bins for histogram statistics.
        histogram_range (Optional[Tuple[float, float]]): Range (min, max) for histogram,
            or None to determine automatically.
    """

    raster_id: str
    geometry: dict  # GeoJSON geometry
    from_crs: str = Field(default="EPSG:4326")
    reducer: Literal[
        "mean", "sum", "min", "max", "std", "count", "median", "histogram"
    ] = "mean"
    histogram_bins: Optional[int] = 16
    histogram_range: Optional[tuple[float, float]] = None


class StatsOut(BaseModel):
    """Output model for pixel or geometry-based statistics.

    Attributes:
        raster_id (str): Identifier of the raster used for analysis.
        band (int): Band number used, default is 1.
        reducer (Optional[str]): Statistical reducer applied, if any.
        value (Optional[float]): Single pixel value when applicable.
        stats (Optional[dict]): Dictionary of computed statistics.
        units (Optional[str]): Units of measurement for the raster data.
        nodata (Optional[float]): Nodata value for the raster.
        pixel (Optional[dict]): Pixel metadata including row/col indices and coordinates.
        geometry (Optional[dict]): GeoJSON geometry associated with the result, if any.
    """

    raster_id: str
    band: int = 1
    reducer: Optional[str] = None
    value: Optional[float] = None
    stats: Optional[dict] = None
    units: Optional[str] = None
    nodata: Optional[float] = None
    pixel: Optional[dict] = None
    geometry: Optional[dict] = None


class WindowStatsOut(BaseModel):
    """Output model for window-based raster statistics.

    Attributes:
        raster_id (str): Identifier of the raster used for analysis.
        window (Dict[str, Any]): Window metadata (offsets, dimensions, center pixel).
        nodata (Optional[float]): Nodata value for the raster.
        units (Optional[str]): Units of measurement for the raster data.
        stats (Dict[str, Any]): Summary statistics for the sampled window.
        histogram (Dict[str, Any]): Histogram data for the sampled window.
    """

    raster_id: str
    window: Dict[str, Any]
    nodata: Optional[float] = None
    units: Optional[str] = None
    stats: Dict[str, Any]
    histogram: Dict[str, Any]


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
    return y.get("layers", {})


REGISTRY = _load_registry()


def _open_raster(raster_id: str):
    print(f"getting {raster_id} from {REGISTRY}", flush=True)
    meta = REGISTRY.get(raster_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"raster_id not found: {raster_id}")
    path = meta["file_path"]
    if not Path(path).exists():
        raise HTTPException(status_code=500, detail=f"raster file missing: {path}")
    ds = rasterio.open(path)
    nodata = meta.get("nodata", ds.nodata)
    units = meta.get("units")
    return ds, nodata, units


app = FastAPI(title="ESSOSC Raster Stats API", version="0.1.0")

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
    # projected CRS only: area = |a * e| where a=px width, e=px height (note: e is negative for north-up)
    try:
        crsproj = ds.crs and ds.crs.is_projected
    except Exception:
        crsproj = False
    if not crsproj:
        return None
    a = ds.transform.a
    e = ds.transform.e
    if a == 0 or e == 0:
        return None
    return abs(a * e)


@app.get("/health")
def health():
    return {"ok": True, "rasters": list(REGISTRY.keys())}


@app.get("/rasters")
def rasters():
    return {"rasters": list(REGISTRY.keys())}


@app.post("/stats/pixel", response_model=WindowStatsOut)
def pixel_stats(q: PixelWindowStatsIn):
    ds, nodata, units = _open_raster(q.raster_id)

    # reproject center point to raster CRS if needed
    if q.crs != ds.crs.to_string():
        transformer = Transformer.from_crs(q.crs, ds.crs, always_xy=True)
        x, y = transformer.transform(q.lon, q.lat)
    else:
        x, y = q.lon, q.lat

    r, c = rowcol(ds.transform, x, y, op=round)
    if not (0 <= r < ds.height and 0 <= c < ds.width):
        raise HTTPException(status_code=400, detail="point outside raster extent")

    # optional bbox: reproject bbox corners to raster CRS
    bbox_raster = None
    if q.bbox is not None:
        minx, miny, maxx, maxy = q.bbox
        if q.crs != ds.crs.to_string():
            transformer = Transformer.from_crs(q.crs, ds.crs, always_xy=True)
            bx = box(minx, miny, maxx, maxy)
            minx, miny = transformer.transform(bx.bounds[0], bx.bounds[1])
            maxx, maxy = transformer.transform(bx.bounds[2], bx.bounds[3])
        bbox_raster = (minx, miny, maxx, maxy)

    win = _compute_window(ds, x, y, r, c, q.radius_pixels, bbox_raster)
    win = (
        win.intersection(Window(0, 0, ds.width, ds.height))
        .round_offsets()
        .round_lengths()
    )
    if win.width <= 0 or win.height <= 0:
        raise HTTPException(status_code=400, detail="empty window")

    arr = ds.read(1, window=win, masked=False).astype("float64")
    mask = np.zeros_like(arr, dtype=bool)
    if nodata is not None:
        mask |= np.isclose(arr, nodata)
    mask |= ~np.isfinite(arr)

    vals = arr[~mask]
    count_valid = int(vals.size)
    count_all = int(arr.size)

    if count_valid == 0:
        stats = {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "std": None,
            "full_area_m2": None,
            "non_nodata_area_m2": None,
        }
        histogram = {"hist": [], "bin_edges": [], "count": 0}
    else:
        stats = {
            "count": count_valid,
            "min": float(np.min(vals)),
            "max": float(np.max(vals)),
            "mean": float(np.mean(vals)),
            "median": float(np.median(vals)),
            "std": float(np.std(vals, ddof=1) if count_valid > 1 else 0.0),
        }
        bins = q.histogram_bins or 16
        rng = q.histogram_range
        hist, bin_edges = np.histogram(vals, bins=bins, range=rng)
        histogram = {
            "hist": hist.tolist(),
            "bin_edges": bin_edges.tolist(),
            "count": count_valid,
        }

        px_area = _area_per_pixel_m2(ds)
        if px_area is not None:
            stats["full_area_m2"] = float(px_area * count_all)
            stats["non_nodata_area_m2"] = float(px_area * count_valid)
        else:
            stats["full_area_m2"] = None
            stats["non_nodata_area_m2"] = None

    out = WindowStatsOut(
        raster_id=q.raster_id,
        window={
            "col_off": int(win.col_off),
            "row_off": int(win.row_off),
            "width": int(win.width),
            "height": int(win.height),
            "center_pixel": {"row": int(r), "col": int(c)},
        },
        nodata=nodata if nodata is not None else None,
        units=units,
        stats=stats,
        histogram=histogram,
    )
    return out


@app.post("/stats/geometry", response_model=StatsOut)
def geometry_stats(q: GeometryStatsIn):
    ds, nodata, units = _open_raster(q.raster_id)

    geom = shape(q.geometry)
    print(f"this is the geom: {geom}", flush=True)

    # Reproject geometry to raster CRS if needed
    if q.from_crs != ds.crs.to_string():
        transformer = Transformer.from_crs(q.from_crs, ds.crs, always_xy=True)
        geom = shp_transform(lambda x, y, z=None: transformer.transform(x, y), geom)

    # --- safe window builder (avoids zero-sized windows) ---
    def _safe_window_for_geom(dataset, g):
        b = g.bounds
        w = rasterio.windows.from_bounds(*b, transform=dataset.transform)
        w = w.round_offsets().round_lengths()
        w = w.intersection(Window(0, 0, dataset.width, dataset.height))

        if int(w.width) > 0 and int(w.height) > 0:
            return w

        # pad by half a pixel and retry
        xres, yres = map(abs, dataset.res)
        bpad = (
            b[0] - 0.5 * xres,
            b[1] - 0.5 * yres,
            b[2] + 0.5 * xres,
            b[3] + 0.5 * yres,
        )
        w = rasterio.windows.from_bounds(*bpad, transform=dataset.transform)
        w = w.round_offsets().round_lengths()
        w = w.intersection(Window(0, 0, dataset.width, dataset.height))

        # fallback: centroid pixel
        if int(w.width) == 0 or int(w.height) == 0:
            cx, cy = g.centroid.x, g.centroid.y
            rr, cc = rasterio.transform.rowcol(dataset.transform, cx, cy)
            rr = min(max(rr, 0), dataset.height - 1)
            cc = min(max(cc, 0), dataset.width - 1)
            w = Window(cc, rr, 1, 1)

        return w

    # Window the raster to geometry bounds
    print(f"here are the raster bounds: {geom.bounds}")
    window = _safe_window_for_geom(ds, geom)
    data = ds.read(1, window=window, boundless=True, masked=False)
    if data.size == 0:
        raise HTTPException(
            status_code=400,
            detail="Empty read window (geometry outside raster).",
        )
    print(
        f"window (col_off,row_off,width,height): {window.col_off:.2f},{window.row_off:.2f},{window.width:.2f},{window.height:.2f}",
        flush=True,
    )

    # Build geometry mask in the window’s transform
    window_transform = ds.window_transform(window)
    print(f"here's the window transform: {window_transform}")
    mask = geometry_mask(
        [mapping(geom)],
        transform=window_transform,
        invert=True,
        out_shape=data.shape,
        all_touched=False,
    )

    # Apply mask + nodata
    arr = data.astype("float64", copy=False)
    if nodata is not None:
        arr = np.where(np.isclose(arr, nodata), np.nan, arr)
    # keep only pixels inside polygon
    arr = np.where(mask, arr, np.nan)

    # Stats over valid (finite) values
    vals = arr[np.isfinite(arr)]

    stats = {
        "count": int(vals.size),
        "min": None,
        "max": None,
        "mean": None,
        "median": None,
        "sum": None,
        "std": None,
        "hist": None,
        "bin_edges": None,
    }
    if vals.size > 0:
        stats.update(
            {
                "min": float(np.min(vals)),
                "max": float(np.max(vals)),
                "mean": float(np.mean(vals)),
                "median": float(np.median(vals)),
                "sum": float(np.sum(vals)),
                "std": float(np.std(vals, ddof=1) if vals.size > 1 else 0.0),
            }
        )
        qs = np.linspace(0, 1, 17)
        bin_edges = np.quantile(vals, qs)
        q25, q75 = np.percentile(vals, [25, 75])
        iqr = q75 - q25
        bin_width = 2 * iqr * len(vals) ** (-1 / 3)
        if bin_width == 0:
            bin_width = np.ptp(vals) / 10 or 1  # fallback

        num_bins_fd = int(np.ceil(np.ptp(vals) / bin_width))
        num_bins = min(num_bins_fd, 64)  # cap at 32 bins (or whatever limit you want)

        hist, bin_edges = np.histogram(vals, bins=num_bins)
        # hist, bin_edges = np.histogram(vals, bins="doane")
        stats["hist"] = hist.tolist()
        stats["bin_edges"] = bin_edges.tolist()

    # Area metrics (assumes projected CRS in meters; for geographic CRS these are in "degree²")
    # pixel area from affine determinant (handles rotations too)
    det = ds.transform.a * ds.transform.e - ds.transform.b * ds.transform.d
    pixel_area = abs(det)
    total_mask_pixels = int(np.count_nonzero(mask))
    valid_pixels = int(np.count_nonzero(np.isfinite(arr)))
    nodata_pixels = total_mask_pixels - valid_pixels

    area_stats = {
        "pixel_area": pixel_area,  # m² per pixel if CRS in meters
        "window_mask_pixels": total_mask_pixels,
        "valid_pixels": valid_pixels,
        "nodata_pixels": nodata_pixels,
        "window_mask_area_m2": float(total_mask_pixels * pixel_area),
        "valid_area_m2": float(valid_pixels * pixel_area),
        "nodata_area_m2": float(nodata_pixels * pixel_area),
        "coverage_ratio": (
            float(valid_pixels / total_mask_pixels) if total_mask_pixels else 0.0
        ),
    }
    stats.update(area_stats)

    return StatsOut(
        raster_id=q.raster_id,
        stats=stats,
        nodata=(None if nodata is None else nodata),
        units=units,
        geometry=q.geometry,
    )
