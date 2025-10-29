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
from osgeo import gdal
from pydantic import BaseModel
from pyproj import Transformer
from rasterio.features import geometry_mask
from rasterio.warp import reproject, Resampling
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds, Window
from shapely.geometry import shape, mapping
from shapely.ops import transform as shp_transform
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
        pixels_sampled (Optional[int]): Number of pixels included in the window mask, or None if not applicable.
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
        geometry (shapely.geometry.base.BaseGeometry): Geometry to bound,
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
        x_ds, x_nodata_val = _open_raster(scatter_request.raster_id_x)
        y_ds, y_nodata_val = _open_raster(scatter_request.raster_id_y)

        logger.debug("Shaping geometry")
        geom_in_shape = shape(scatter_request.geometry)

        if scatter_request.from_crs != x_ds.crs.to_string():
            logger.debug("Reprojecting geometry to X raster CRS")
            transformer_obj = Transformer.from_crs(
                scatter_request.from_crs, x_ds.crs, always_xy=True
            )
            geom_x_shape = shp_transform(
                lambda x, y, z=None: transformer_obj.transform(x, y),
                geom_in_shape,
            )
        else:
            geom_x_shape = geom_in_shape

        logger.debug("Building window on X grid")
        x_win_obj = _safe_window_for_geom(x_ds, geom_x_shape)

        logger.debug("Reading x raster data")
        x_data_nparray = x_ds.read(
            1, window=x_win_obj, boundless=True, masked=False
        )
        if x_data_nparray.size == 0:
            raise HTTPException(
                status_code=400,
                detail="Empty read window (geometry outside raster_x).",
            )

        logger.debug("Computing transform for X window")
        x_win_affine = x_ds.window_transform(x_win_obj)

        logger.debug("Building geometry mask")
        x_mask_nparray = geometry_mask(
            [mapping(geom_x_shape)],
            transform=x_win_affine,
            invert=True,
            out_shape=x_data_nparray.shape,
            all_touched=bool(scatter_request.all_touched),
        )

        logger.debug("Applying nodata mask for X")
        x_arr_nparray = x_data_nparray.astype("float64", copy=False)
        if x_nodata_val is not None:
            x_arr_nparray = np.where(
                np.isclose(x_arr_nparray, x_nodata_val), np.nan, x_arr_nparray
            )
        x_arr_nparray = np.where(x_mask_nparray, x_arr_nparray, np.nan)

        logger.debug("Computing Y window on its own grid")

        logger.debug("Reprojecting X window extent into Y CRS")
        upper_left_xy = x_win_affine * (0, 0)
        lower_right_xy = x_win_affine * (
            x_data_nparray.shape[1],
            x_data_nparray.shape[0],
        )

        min_x_in_xcrs, max_x_in_xcrs = sorted(
            [upper_left_xy[0], lower_right_xy[0]]
        )
        min_y_in_xcrs, max_y_in_xcrs = sorted(
            [upper_left_xy[1], lower_right_xy[1]]
        )

        logger.debug("Transforming X bounds into Y CRS")
        min_x_in_ycrs, min_y_in_ycrs, max_x_in_ycrs, max_y_in_ycrs = (
            transform_bounds(
                x_ds.crs,
                y_ds.crs,
                min_x_in_xcrs,
                min_y_in_xcrs,
                max_x_in_xcrs,
                max_y_in_xcrs,
                densify_pts=0,
            )
        )

        logger.debug("Building Y read window")
        y_win_candidate_obj = from_bounds(  # _win_obj = Window candidate
            min_x_in_ycrs,
            min_y_in_ycrs,
            max_x_in_ycrs,
            max_y_in_ycrs,
            transform=y_ds.transform,
        )
        y_win_obj = (
            y_win_candidate_obj.round_offsets()
            .round_lengths()
            .intersection(Window(0, 0, y_ds.width, y_ds.height))
        )

        logger.debug("Reading Y source data")
        y_src_nparray = y_ds.read(1, window=y_win_obj, masked=False).astype(
            "float64", copy=False
        )
        y_win_affine = y_ds.window_transform(
            y_win_obj
        )  # _affine = Affine transform

        logger.debug("Allocating destination array for reprojected Y on X grid")
        y_on_xgrid_nparray = np.full(
            x_data_nparray.shape, np.nan, dtype="float64"
        )  # _nparray = np.ndarray

        logger.debug("Reprojecting Y data onto X grid")
        reproject(
            source=y_src_nparray,
            destination=y_on_xgrid_nparray,
            src_transform=y_win_affine,
            src_crs=y_ds.crs,
            src_nodata=y_nodata_val if y_nodata_val is not None else None,
            dst_transform=x_win_affine,
            dst_crs=x_ds.crs,
            dst_nodata=np.nan,
            resampling=Resampling.nearest,
            num_threads=0,
        )
        logger.debug("Applying same polygon mask to Y array")
        y_on_xgrid_masked_nparray = np.where(
            x_mask_nparray, y_on_xgrid_nparray, np.nan
        )

        logger.debug("Extracting finite paired values")
        finite_mask_nparray = np.isfinite(x_arr_nparray) & np.isfinite(
            y_on_xgrid_masked_nparray
        )
        x_vals_nparray = x_arr_nparray[finite_mask_nparray]
        y_vals_nparray = y_on_xgrid_masked_nparray[finite_mask_nparray]
        n_pairs_int = int(x_vals_nparray.size)
        logger.debug(f"Found {n_pairs_int} finite pairs")

        if n_pairs_int == 0:
            logger.debug("No valid pairs found; returning empty ScatterOut")
            return ScatterOut(
                raster_id_x=scatter_request.raster_id_x,
                raster_id_y=scatter_request.raster_id_y,
                n_pairs=0,
                pixels_sampled=int(np.count_nonzero(x_mask_nparray)),
                valid_pixels=0,
                coverage_ratio=0.0,
                geometry=scatter_request.geometry,
            )

        logger.debug("Downsampling data if needed")
        if n_pairs_int > scatter_request.max_points:
            rng_obj = np.random.default_rng(0)
            idx_nparray = rng_obj.choice(
                n_pairs_int, size=scatter_request.max_points, replace=False
            )
            x_plot_nparray = x_vals_nparray[idx_nparray]
            y_plot_nparray = y_vals_nparray[idx_nparray]
        else:
            x_plot_nparray = x_vals_nparray
            y_plot_nparray = y_vals_nparray

        logger.debug("Computing correlation and linear fit")
        if n_pairs_int > 1:
            x_std_val, y_std_val = np.std(x_vals_nparray), np.std(
                y_vals_nparray
            )
            if x_std_val == 0 or y_std_val == 0:
                pearson_r_val = None
            else:
                corr_val = np.corrcoef(x_vals_nparray, y_vals_nparray)[0, 1]
                pearson_r_val = (
                    float(corr_val) if np.isfinite(corr_val) else None
                )
        else:
            pearson_r_val = None

        if n_pairs_int > 1:
            design_mtx_nparray = np.vstack(
                [x_vals_nparray, np.ones_like(x_vals_nparray)]
            ).T
            slope_val, intercept_val = np.linalg.lstsq(
                design_mtx_nparray, y_vals_nparray, rcond=None
            )[
                0
            ]  # renamed: slope/intercept -> slope_val/intercept_val
            slope_val = float(slope_val)
            intercept_val = float(intercept_val)
        else:
            slope_val = None
            intercept_val = None

        logger.debug("Computing 2D histogram")
        # existing 2D histogram
        hist2d_counts_nparray, x_edges_nparray, y_edges_nparray = (
            np.histogram2d(
                x_vals_nparray,
                y_vals_nparray,
                bins=scatter_request.histogram_bins,
            )
        )

        # add matching 1D histograms for X and Y using same edges
        hist1d_x_counts_nparray, _ = np.histogram(
            x_vals_nparray, bins=x_edges_nparray
        )
        hist1d_y_counts_nparray, _ = np.histogram(
            y_vals_nparray, bins=y_edges_nparray
        )

        # ensure consistent dtype
        hist2d_counts_nparray = hist2d_counts_nparray.astype("int64")
        hist1d_x_counts_nparray = hist1d_x_counts_nparray.astype("int64")
        hist1d_y_counts_nparray = hist1d_y_counts_nparray.astype("int64")
        total_mask_pixels_int = int(np.count_nonzero(x_mask_nparray))
        valid_pixels_int = int(n_pairs_int)

        logger.debug("Assembling ScatterOut response")
        return ScatterOut(
            raster_id_x=scatter_request.raster_id_x,
            raster_id_y=scatter_request.raster_id_y,
            n_pairs=n_pairs_int,
            x=x_plot_nparray.astype("float64").tolist(),
            y=y_plot_nparray.astype("float64").tolist(),
            hist2d=hist2d_counts_nparray.tolist(),
            x_edges=x_edges_nparray.astype("float64").tolist(),
            y_edges=y_edges_nparray.astype("float64").tolist(),
            pearson_r=pearson_r_val,
            slope=slope_val,
            intercept=intercept_val,
            hist1d_x=hist1d_x_counts_nparray,
            hist1d_y=hist1d_y_counts_nparray,
            pixels_sampled=total_mask_pixels_int,
            valid_pixels=valid_pixels_int,
            coverage_ratio=(
                float(valid_pixels_int / total_mask_pixels_int)
                if total_mask_pixels_int
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
