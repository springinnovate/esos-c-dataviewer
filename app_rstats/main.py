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

from pathlib import Path
from typing import Literal, Optional, Dict, Tuple, Any
import logging
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


from rasterio.warp import reproject, Resampling

load_dotenv()

RASTERS_YAML_PATH = Path(os.getenv("RASTERS_YAML_PATH"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class MinMaxIn(BaseModel):
    """Input model for min max value query.

    Attributes:
        raster_id (str): Identifier of the target raster in the registry.
    """

    raster_id: str


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
    histogram_range: Optional[Tuple[float, float]] = (
        None  # min,max or None to auto
    )


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
    raster_id_x: str
    raster_id_y: str
    geometry: dict
    from_crs: str = "EPSG:4326"
    bins: int = 50
    max_points: int = 50000
    all_touched: bool = False


class ScatterOut(BaseModel):
    raster_id_x: str
    raster_id_y: str
    n_pairs: int
    x: Optional[List[float]] = None
    y: Optional[List[float]] = None
    hist2d: Optional[List[List[int]]] = None
    x_edges: Optional[List[float]] = None
    y_edges: Optional[List[float]] = None
    corr: Optional[float] = None
    slope: Optional[float] = None
    intercept: Optional[float] = None
    units_x: Optional[float] = None
    units_y: Optional[float] = None
    nodata_x: Optional[float] = None
    nodata_y: Optional[float] = None
    window_mask_pixels: Optional[int] = None
    valid_pixels: Optional[int] = None
    coverage_ratio: Optional[float] = None
    geometry: dict


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
    layer_dict = {k.lower(): v for k, v in y.get("layers", {}).items()}
    return layer_dict


REGISTRY = _load_registry()


def _open_raster(raster_id: str):
    """Open a registered raster dataset and return its metadata.

    Looks up the raster entry from the global `REGISTRY` using the provided
    raster ID, verifies that the file exists, and opens it with Rasterio.
    Returns the dataset handle along with its nodata value and units.

    Args:
        raster_id (str): Identifier of the raster to open, matching an entry in `REGISTRY`.

    Returns:
        tuple:
            ds (rasterio.io.DatasetReader): Opened Rasterio dataset.
            nodata (float | None): Nodata value from metadata or dataset.
            units (str | None): Units of measurement from metadata, if available.

    Raises:
        HTTPException: If the raster ID is not found in the registry (404) or
            if the raster file does not exist on disk (500).

    Side Effects:
        Prints diagnostic information about the raster being opened.
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
    units = meta.get("units")
    return ds, nodata, units


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


@app.post("/stats/pixel", response_model=WindowStatsOut)
def pixel_stats(q: PixelWindowStatsIn):
    """Compute summary statistics for a pixel-centered raster window.

    Given a geographic coordinate or bounding box, this endpoint samples the
    corresponding window from a registered raster and computes basic
    statistics (min, max, mean, median, std) and a histogram of pixel values.
    The input coordinate or bounding box is automatically reprojected to the
    raster’s CRS if necessary. Nodata and non-finite values are excluded from
    calculations.

    Args:
        q (PixelWindowStatsIn): Request model specifying the raster ID, input
            coordinate (lon/lat), optional pixel radius or bounding box, and
            histogram parameters.

    Returns:
        WindowStatsOut: Object containing raster window metadata, computed
        statistics, histogram data, and nodata/unit information.

    Raises:
        HTTPException: If the raster ID is not found, the file is missing, or
            the target point/window lies outside the raster extent.
    """
    ds, nodata, units = _open_raster(q.raster_id)

    # reproject center point to raster CRS if needed
    if q.crs != ds.crs.to_string():
        transformer = Transformer.from_crs(q.crs, ds.crs, always_xy=True)
        x, y = transformer.transform(q.lon, q.lat)
    else:
        x, y = q.lon, q.lat

    r, c = rowcol(ds.transform, x, y, op=round)
    if not (0 <= r < ds.height and 0 <= c < ds.width):
        raise HTTPException(
            status_code=400, detail="point outside raster extent"
        )

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


def _sample_percentiles(src, samples, frac):
    """Estimate approximate 5th and 95th percentiles from random raster windows.

    This function samples multiple random windows from the input raster, extracts
    valid (non-masked) pixel values, and computes approximate percentile bounds.
    The sampling is fractional, meaning each window covers a fraction of the total
    raster area defined by `frac`.

    Args:
        src (rasterio.io.DatasetReader): An open rasterio dataset to sample from.
        samples (int): Number of random windows to sample.
        frac (float): Fraction (0-1) of the raster dimensions to use for each window
            in both height and width.

    Returns:
        numpy.ndarray: A 1D array of two elements `[p5, p95]` representing the
        approximate 5th and 95th percentile values of the sampled pixels.

    Raises:
        ValueError: If `frac` is not within the range (0, 1].
    """
    sample_values = []
    raster_height, raster_width = src.height, src.width
    window_height, window_width = int(raster_height * frac), int(
        raster_width * frac
    )

    for _ in range(samples):
        row_offset = np.random.randint(0, raster_height - window_height)
        col_offset = np.random.randint(0, raster_width - window_width)
        window = rasterio.windows.Window(
            col_offset, row_offset, window_width, window_height
        )
        band_data = src.read(1, window=window, masked=True)
        sample_values.append(band_data.compressed())

    sample_values = np.concatenate(sample_values)
    return np.nanpercentile(sample_values, [5, 95])


@app.post("/stats/minmax", response_model=RasterMinMaxOut)
def minmax_stats(r: MinMaxIn):
    """Compute approximate 5th and 95th percentile values for a raster.

    This endpoint estimates the low and high value range for a given raster by
    sampling multiple random windows and aggregating valid pixel values.
    The computed percentiles are used as approximate minimum and maximum values
    for visualization or dynamic styling.

    Args:
        r (MinMaxIn): Input model containing the raster identifier (`raster_id`)
            to locate and open the raster file.

    Returns:
        RasterMinMaxOut: Object containing the raster ID along with the estimated
        5th percentile (`min_`) and 95th percentile (`max_`) values.

    Raises:
        HTTPException: If any error occurs while reading the raster or computing
        percentiles. Returns HTTP 500 with detail "Failed to compute min/max".
    """
    try:
        ds, _, _ = _open_raster(r.raster_id)
        # 10 samples  0.05 proportion
        p5, p95 = _sample_percentiles(ds, 10, 0.05)
        return RasterMinMaxOut(
            raster_id=r.raster_id, min_=float(p5), max_=float(p95)
        )

    except Exception:
        logger.exception("minmax_stats failed")
        raise HTTPException(status_code=500, detail="Failed to compute min/max")


@app.post("/stats/geometry", response_model=StatsOut)
def geometry_stats(q: GeometryStatsIn):
    """Compute zonal statistics for a polygon geometry over a raster.

    Given a GeoJSON geometry and a registered raster ID, this endpoint computes
    statistics (min, max, mean, median, sum, std, histogram) over the pixels
    within the polygon footprint. The geometry is automatically reprojected
    to the raster’s CRS if necessary. Nodata values and non-finite pixels are
    excluded from all calculations. The function also computes per-pixel and
    total area metrics.

    Args:
        q (GeometryStatsIn): Request model containing the raster ID, input
            geometry, input CRS, and optional histogram settings.

    Returns:
        StatsOut: Object containing computed statistics, histogram data,
        nodata and unit information, and derived area metrics.

    Raises:
        HTTPException: If the geometry lies outside the raster extent or
            produces an empty read window.

    Notes:
        - For projected rasters, area metrics are expressed in square meters.
        - For geographic rasters, area metrics are in square degrees.
        - Uses a safe windowing strategy to handle small or edge geometries.
    """
    try:
        ds, nodata, units = _open_raster(q.raster_id)

        geom = shape(q.geometry)

        # Reproject geometry to raster CRS if needed
        if q.from_crs != ds.crs.to_string():
            transformer = Transformer.from_crs(
                q.from_crs, ds.crs, always_xy=True
            )
            geom = shp_transform(
                lambda x, y, z=None: transformer.transform(x, y), geom
            )

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

        window = _safe_window_for_geom(ds, geom)
        logger.debug(f"this is the safe window: {window}")
        data = ds.read(1, window=window, boundless=True, masked=False)
        logger.debug(f"here's the data: {data}")
        if data.size == 0:
            raise HTTPException(
                status_code=400,
                detail="Empty read window (geometry outside raster).",
            )

        # Build geometry mask in the window’s transform
        window_transform = ds.window_transform(window)
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
        logger.debug(f"here's the masked data: {arr}")

        # Stats over valid (finite) values
        vals = arr[np.isfinite(arr)]
        logger.debug(f"here is the finite data: {vals}")

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
                    "std": float(
                        np.std(vals, ddof=1) if vals.size > 1 else 0.0
                    ),
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
            # at least 1 to 64 bins
            num_bins = max(1, min(num_bins_fd, 64))
            logger.debug(
                f"number of bins {num_bins}; bin width {bin_width}; bin_edges {bin_edges}; qs: {qs}"
            )

            hist, bin_edges = np.histogram(vals, bins=num_bins)
            # hist, bin_edges = np.histogram(vals, bins="doane")
            stats["hist"] = hist.tolist()
            stats["bin_edges"] = bin_edges.tolist()

        # Area metrics (assumes projected CRS in meters; for geographic CRS these are in "degree^2")
        # pixel area from affine determinant (handles rotations too)
        det = ds.transform.a * ds.transform.e - ds.transform.b * ds.transform.d
        pixel_area = abs(det)
        total_mask_pixels = int(np.count_nonzero(mask))
        valid_pixels = int(np.count_nonzero(np.isfinite(arr)))
        nodata_pixels = total_mask_pixels - valid_pixels

        area_stats = {
            "pixel_area": pixel_area,  # m^2 per pixel if CRS in meters
            "window_mask_pixels": total_mask_pixels,
            "valid_pixels": valid_pixels,
            "nodata_pixels": nodata_pixels,
            "window_mask_area_m2": float(total_mask_pixels * pixel_area),
            "valid_area_m2": float(valid_pixels * pixel_area),
            "nodata_area_m2": float(nodata_pixels * pixel_area),
            "coverage_ratio": (
                float(valid_pixels / total_mask_pixels)
                if total_mask_pixels
                else 0.0
            ),
        }
        stats.update(area_stats)

        # json does not handle nans so we need to convert to nones
        clean_units = (
            units if units is not None and np.isfinite(units) else None
        )
        clean_nodata = (
            nodata if nodata is not None and np.isfinite(nodata) else None
        )
        result = StatsOut(
            raster_id=q.raster_id,
            stats=stats,
            nodata=clean_nodata,
            units=clean_units,
            geometry=q.geometry,
        )
        return result
    except Exception:
        logger.exception("something bad happened")


@app.post("/stats/scatter", response_model=ScatterOut)
def geometry_scatter(q: GeometryScatterIn):
    try:
        logging.debug("Opening rasters")
        dsx, nodata_x, units_x = _open_raster(q.raster_id_x)
        dsy, nodata_y, units_y = _open_raster(q.raster_id_y)

        logging.debug("Shaping geometry")
        geom = shape(q.geometry)

        if q.from_crs != dsx.crs.to_string():
            logging.debug("Reprojecting geometry to X raster CRS")
            transformer = Transformer.from_crs(
                q.from_crs, dsx.crs, always_xy=True
            )
            geom = shp_transform(
                lambda x, y, z=None: transformer.transform(x, y), geom
            )

        def _safe_window_for_geom(dataset, g):
            b = g.bounds
            w = rasterio.windows.from_bounds(*b, transform=dataset.transform)
            w = w.round_offsets().round_lengths()
            w = w.intersection(Window(0, 0, dataset.width, dataset.height))
            if int(w.width) > 0 and int(w.height) > 0:
                return w
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
            if int(w.width) == 0 or int(w.height) == 0:
                cx, cy = g.centroid.x, g.centroid.y
                rr, cc = rasterio.transform.rowcol(dataset.transform, cx, cy)
                rr = min(max(rr, 0), dataset.height - 1)
                cc = min(max(cc, 0), dataset.width - 1)
                w = Window(cc, rr, 1, 1)
            return w

        logging.debug("Building window on X grid")
        win_x = _safe_window_for_geom(dsx, geom)
        logging.debug("Reading data_x from raster")
        data_x = dsx.read(1, window=win_x, boundless=True, masked=False)
        if data_x.size == 0:
            raise HTTPException(
                status_code=400,
                detail="Empty read window (geometry outside raster_x).",
            )

        logging.debug("Computing transform for X window")
        transform_x = dsx.window_transform(win_x)

        logging.debug("Building geometry mask")
        mask = geometry_mask(
            [mapping(geom)],
            transform=transform_x,
            invert=True,
            out_shape=data_x.shape,
            all_touched=bool(q.all_touched),
        )

        logging.debug("Applying nodata mask for X")
        arr_x = data_x.astype("float64", copy=False)
        if nodata_x is not None:
            arr_x = np.where(np.isclose(arr_x, nodata_x), np.nan, arr_x)
        arr_x = np.where(mask, arr_x, np.nan)

        logging.debug("Reprojecting Y raster into X window grid")
        dest_y = np.full(arr_x.shape, np.nan, dtype="float64")
        reproject(
            source=dsy.read(1, masked=False).astype("float64", copy=False),
            destination=dest_y,
            src_transform=dsy.transform,
            src_crs=dsy.crs,
            src_nodata=nodata_y if nodata_y is not None else None,
            dst_transform=transform_x,
            dst_crs=dsx.crs,
            dst_nodata=np.nan,
            resampling=Resampling.nearest,
        )

        logging.debug("Applying same polygon mask to Y array")
        arr_y = np.where(mask, dest_y, np.nan)

        logging.debug("Extracting finite paired values")
        finite_mask = np.isfinite(arr_x) & np.isfinite(arr_y)
        x_vals = arr_x[finite_mask]
        y_vals = arr_y[finite_mask]

        n_pairs = int(x_vals.size)
        logging.debug(f"Found {n_pairs} finite pairs")

        if n_pairs == 0:
            logging.debug("No valid pairs found; returning empty ScatterOut")
            return ScatterOut(
                raster_id_x=q.raster_id_x,
                raster_id_y=q.raster_id_y,
                n_pairs=0,
                units_x=(
                    units_x
                    if units_x is not None and np.isfinite(units_x)
                    else None
                ),
                units_y=(
                    units_y
                    if units_y is not None and np.isfinite(units_y)
                    else None
                ),
                nodata_x=(
                    nodata_x
                    if nodata_x is not None and np.isfinite(nodata_x)
                    else None
                ),
                nodata_y=(
                    nodata_y
                    if nodata_y is not None and np.isfinite(nodata_y)
                    else None
                ),
                window_mask_pixels=int(np.count_nonzero(mask)),
                valid_pixels=0,
                coverage_ratio=0.0,
                geometry=q.geometry,
            )

        logging.debug("Downsampling data if needed")
        if n_pairs > q.max_points:
            rng = np.random.default_rng(0)
            idx = rng.choice(n_pairs, size=q.max_points, replace=False)
            x_plot = x_vals[idx]
            y_plot = y_vals[idx]
        else:
            x_plot = x_vals
            y_plot = y_vals

        logging.debug("Computing correlation and linear fit")
        corr = float(np.corrcoef(x_vals, y_vals)[0, 1]) if n_pairs > 1 else None
        if n_pairs > 1:
            A = np.vstack([x_vals, np.ones_like(x_vals)]).T
            slope, intercept = np.linalg.lstsq(A, y_vals, rcond=None)[0]
            slope = float(slope)
            intercept = float(intercept)
        else:
            slope = None
            intercept = None

        logging.debug("Computing 2D histogram")
        bins = int(max(1, min(256, q.bins)))
        H, x_edges, y_edges = np.histogram2d(x_vals, y_vals, bins=bins)
        H = H.astype("int64")

        total_mask_pixels = int(np.count_nonzero(mask))
        valid_pixels = int(n_pairs)

        logging.debug("Assembling ScatterOut response")
        return ScatterOut(
            raster_id_x=q.raster_id_x,
            raster_id_y=q.raster_id_y,
            n_pairs=n_pairs,
            x=x_plot.astype("float64").tolist(),
            y=y_plot.astype("float64").tolist(),
            hist2d=H.tolist(),
            x_edges=x_edges.astype("float64").tolist(),
            y_edges=y_edges.astype("float64").tolist(),
            corr=corr,
            slope=slope,
            intercept=intercept,
            units_x=(
                units_x
                if units_x is not None and np.isfinite(units_x)
                else None
            ),
            units_y=(
                units_y
                if units_y is not None and np.isfinite(units_y)
                else None
            ),
            nodata_x=(
                nodata_x
                if nodata_x is not None and np.isfinite(nodata_x)
                else None
            ),
            nodata_y=(
                nodata_y
                if nodata_y is not None and np.isfinite(nodata_y)
                else None
            ),
            window_mask_pixels=total_mask_pixels,
            valid_pixels=valid_pixels,
            coverage_ratio=(
                float(valid_pixels / total_mask_pixels)
                if total_mask_pixels
                else 0.0
            ),
            geometry=q.geometry,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("scatter stats failed")
        raise HTTPException(
            status_code=500, detail="Scatter computation failed."
        )
