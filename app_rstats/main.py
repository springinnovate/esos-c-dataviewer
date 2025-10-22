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
from typing import Optional, Tuple, List
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pyproj import Transformer
from rasterio.features import geometry_mask
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds, Window
from shapely.geometry import shape, mapping
from shapely.ops import transform as shp_transform
import numpy as np
import rasterio
import yaml


from rasterio.warp import reproject, Resampling

load_dotenv()

RASTERS_YAML_PATH = Path(os.getenv("RASTERS_YAML_PATH"))

logging.basicConfig(
    level=logging.INFO,
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
    raster_id_x: str
    raster_id_y: str
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
        window_mask_pixels (Optional[int]): Number of pixels included in the window mask, or None if not applicable.
        valid_pixels (Optional[int]): Number of valid (non-null) paired pixels, or None if not available.
        coverage_ratio (Optional[float]): Ratio of valid pixels to total mask pixels, or None if not available.
        geometry (dict): GeoJSON-like geometry defining the analysis window.
    """

    raster_id_x: str
    raster_id_y: str
    n_pairs: int
    x: Optional[List[float]] = None
    y: Optional[List[float]] = None
    hist2d: Optional[List[List[int]]] = None
    x_edges: Optional[List[float]] = None
    y_edges: Optional[List[float]] = None
    pearson_r: Optional[float] = None
    slope: Optional[float] = None
    intercept: Optional[float] = None
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
        ds, _, _ = _open_raster(r.raster_id)
        # 10 samples  0.05 proportion
        p5, p95 = _sample_percentiles(ds, 10, 0.05)
        return RasterMinMaxOut(
            raster_id=r.raster_id, min_=float(p5), max_=float(p95)
        )

    except Exception:
        logger.exception("minmax_stats failed")
        raise HTTPException(status_code=500, detail="Failed to compute min/max")


@app.post("/stats/scatter", response_model=ScatterOut)
def geometry_scatter(scatter_request: GeometryScatterIn):
    try:
        logging.debug("Opening rasters")
        dsx, nodata_x, units_x = _open_raster(scatter_request.raster_id_x)
        dsy, nodata_y, units_y = _open_raster(scatter_request.raster_id_y)

        logging.debug("Shaping geometry")
        geom = shape(scatter_request.geometry)

        if scatter_request.from_crs != dsx.crs.to_string():
            logging.debug("Reprojecting geometry to X raster CRS")
            transformer = Transformer.from_crs(
                scatter_request.from_crs, dsx.crs, always_xy=True
            )
            geom_x = shp_transform(
                lambda x, y, z=None: transformer.transform(x, y), geom
            )

        def _safe_window_for_geom(dataset, geometry):
            """Compute a rasterio window safely enclosing a given geometry.

            Ensures that the resulting window:
            - Falls within the raster bounds
            - Has nonzero dimensions (expands slightly if needed)
            - Falls back to a single pixel window if geometry is too small

            Args:
                dataset (rasterio.io.DatasetReader): Open raster dataset.
                geometry (shapely.geometry.base.BaseGeometry): Geometry to bound.

            Returns:
                rasterio.windows.Window: Window enclosing the geometry or a fallback pixel window.
            """
            geometry_bounds = geometry.bounds
            candidate_window = rasterio.windows.from_bounds(
                *geometry_bounds, transform=dataset.transform
            )
            candidate_window = candidate_window.round_offsets().round_lengths()
            candidate_window = candidate_window.intersection(
                Window(0, 0, dataset.width, dataset.height)
            )

            if (
                int(candidate_window.width) > 0
                and int(candidate_window.height) > 0
            ):
                return candidate_window

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

        logging.debug("Building window on X grid")
        win_x = _safe_window_for_geom(dsx, geom_x)
        logging.debug("Reading data_x from raster")
        data_x = dsx.read(1, window=win_x, boundless=True, masked=False)
        if data_x.size == 0:
            raise HTTPException(
                status_code=400,
                detail="Empty read window (geometry outside raster_x).",
            )

        logging.debug("Computing transform for X window")
        window_transform_x = dsx.window_transform(win_x)

        logging.debug("Building geometry mask")
        mask = geometry_mask(
            [mapping(geom_x)],
            transform=window_transform_x,
            invert=True,
            out_shape=data_x.shape,
            all_touched=bool(scatter_request.all_touched),
        )

        logging.debug("Applying nodata mask for X")
        arr_x = data_x.astype("float64", copy=False)
        if nodata_x is not None:
            arr_x = np.where(np.isclose(arr_x, nodata_x), np.nan, arr_x)
        arr_x = np.where(mask, arr_x, np.nan)

        logging.debug("Computing Y window on its own grid")

        # Build a tight window on Y raster
        logging.debug("Reprojecting Y window into X window grid")
        upper_left_x, upper_left_y = window_transform_x * (0, 0)
        lower_right_x, lower_right_y = window_transform_x * (
            data_x.shape[1],
            data_x.shape[0],
        )
        min_x_in_x_crs, max_x_in_x_crs = sorted([upper_left_x, lower_right_x])
        min_y_in_x_crs, max_y_in_x_crs = sorted([upper_left_y, lower_right_y])

        # transform the X window extent into Y CRS and build a minimal Y read window
        min_x_in_y_crs, min_y_in_y_crs, max_x_in_y_crs, max_y_in_y_crs = (
            transform_bounds(
                dsx.crs,
                dsy.crs,
                min_x_in_x_crs,
                min_y_in_x_crs,
                max_x_in_x_crs,
                max_y_in_x_crs,
                densify_pts=0,
            )
        )

        y_window_candidate = from_bounds(
            min_x_in_y_crs,
            min_y_in_y_crs,
            max_x_in_y_crs,
            max_y_in_y_crs,
            transform=dsy.transform,
        )
        y_read_window = (
            y_window_candidate.round_offsets()
            .round_lengths()
            .intersection(Window(0, 0, dsy.width, dsy.height))
        )

        # read only the needed Y data and reproject onto the X window grid
        y_source_band = dsy.read(1, window=y_read_window, masked=False).astype(
            "float64", copy=False
        )
        y_source_transform = dsy.window_transform(y_read_window)

        y_on_x_grid = np.full(data_x.shape, np.nan, dtype="float64")

        reproject(
            source=y_source_band,
            destination=y_on_x_grid,
            src_transform=y_source_transform,
            src_crs=dsy.crs,
            src_nodata=nodata_y if nodata_y is not None else None,
            dst_transform=window_transform_x,
            dst_crs=dsx.crs,
            dst_nodata=np.nan,
            resampling=Resampling.nearest,
            num_threads=0,
        )

        logging.debug("Applying same polygon mask to Y array")
        arr_y = np.where(mask, y_on_x_grid, np.nan)

        logging.debug("Extracting finite paired values")
        finite_mask = np.isfinite(arr_x) & np.isfinite(arr_y)
        x_vals = arr_x[finite_mask]
        y_vals = arr_y[finite_mask]
        n_pairs = int(x_vals.size)
        logging.debug(f"Found {n_pairs} finite pairs")

        if n_pairs == 0:
            logging.debug("No valid pairs found; returning empty ScatterOut")
            return ScatterOut(
                raster_id_x=scatter_request.raster_id_x,
                raster_id_y=scatter_request.raster_id_y,
                n_pairs=0,
                window_mask_pixels=int(np.count_nonzero(mask)),
                valid_pixels=0,
                coverage_ratio=0.0,
                geometry=scatter_request.geometry,
            )

        logging.debug("Downsampling data if needed")
        if n_pairs > scatter_request.max_points:
            rng = np.random.default_rng(0)
            idx = rng.choice(
                n_pairs, size=scatter_request.max_points, replace=False
            )
            x_plot = x_vals[idx]
            y_plot = y_vals[idx]
        else:
            x_plot = x_vals
            y_plot = y_vals

        logging.debug("Computing correlation and linear fit")
        pearson_r = (
            float(np.corrcoef(x_vals, y_vals)[0, 1]) if n_pairs > 1 else None
        )
        if n_pairs > 1:
            A = np.vstack([x_vals, np.ones_like(x_vals)]).T
            slope, intercept = np.linalg.lstsq(A, y_vals, rcond=None)[0]
            slope = float(slope)
            intercept = float(intercept)
        else:
            slope = None
            intercept = None

        logging.debug("Computing 2D histogram")
        hist2d_counts, x_edges, y_edges = np.histogram2d(
            x_vals, y_vals, bins=scatter_request.histogram_bins
        )
        hist2d_counts = hist2d_counts.astype("int64")

        total_mask_pixels = int(np.count_nonzero(mask))
        valid_pixels = int(n_pairs)

        logging.debug("Assembling ScatterOut response")
        return ScatterOut(
            raster_id_x=scatter_request.raster_id_x,
            raster_id_y=scatter_request.raster_id_y,
            n_pairs=n_pairs,
            x=x_plot.astype("float64").tolist(),
            y=y_plot.astype("float64").tolist(),
            hist2d=hist2d_counts.tolist(),
            x_edges=x_edges.astype("float64").tolist(),
            y_edges=y_edges.astype("float64").tolist(),
            pearson_r=pearson_r,
            slope=slope,
            intercept=intercept,
            window_mask_pixels=total_mask_pixels,
            valid_pixels=valid_pixels,
            coverage_ratio=(
                float(valid_pixels / total_mask_pixels)
                if total_mask_pixels
                else 0.0
            ),
            geometry=scatter_request.geometry,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("scatter stats failed")
        raise HTTPException(
            status_code=500,
            detail=f"Scatter computation failed: {type(e).__name__}: {e}",
        )
