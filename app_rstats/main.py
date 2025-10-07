# app_rstats/main.py
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional

import numpy as np
import rasterio
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from rasterio.features import geometry_mask
from rasterio.transform import rowcol
from shapely.geometry import shape, mapping
from shapely.ops import transform as shp_transform
from pyproj import Transformer

RASTERS_YAML_PATH = Path(os.getenv("RASTERS_YAML_PATH"))

load_dotenv()


class PixelStatsIn(BaseModel):
    raster_id: str
    lon: float
    lat: float
    crs: str = Field(default="EPSG:4326")  # incoming coordinates


class GeometryStatsIn(BaseModel):
    raster_id: str
    geometry: dict  # GeoJSON geometry
    from_crs: str = Field(default="EPSG:4326")
    reducer: Literal[
        "mean", "sum", "min", "max", "std", "count", "median", "histogram"
    ] = "mean"
    histogram_bins: Optional[int] = 16
    histogram_range: Optional[tuple[float, float]] = None


class StatsOut(BaseModel):
    raster_id: str
    band: int = 1
    reducer: Optional[str] = None
    value: Optional[float] = None
    stats: Optional[dict] = None
    units: Optional[str] = None
    nodata: Optional[float] = None
    pixel: Optional[dict] = None
    geometry: Optional[dict] = None


def _load_registry() -> dict:
    try:
        print(f"try to load {RASTERS_YAML_PATH}")
        if not RASTERS_YAML_PATH.exists():
            raise RuntimeError("rasters.yml not found")
        raw_yaml = RASTERS_YAML_PATH.read_text()
        expanded_yaml = os.path.expandvars(raw_yaml)
        y = yaml.safe_load(expanded_yaml)
        return y.get("layers", {})
    except Exception as e:
        print(f"could not load registery {e}")
        raise


REGISTRY = _load_registry()
print(f"this is the registery {REGISTRY}", flush=True)


def _open_raster(raster_id: str):
    print(f"getting {raster_id} from {REGISTRY}", flush=True)
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True, "rasters": list(REGISTRY.keys())}


@app.get("/rasters")
def rasters():
    return {"rasters": list(REGISTRY.keys())}


@app.post("/stats/pixel", response_model=StatsOut)
def pixel_stats(q: PixelStatsIn):
    try:
        print(f"try to fetch {q.raster_id}", flush=True)
        ds, nodata, units = _open_raster(q.raster_id)

        # Reproject input lon/lat to raster CRS if needed
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

        val = ds.read(1, window=((r, r + 1), (c, c + 1)))[0, 0]
        out = StatsOut(
            raster_id=q.raster_id,
            value=(
                None
                if (nodata is not None and np.isclose(val, nodata))
                else float(val)
            ),
            nodata=nodata if nodata is not None else None,
            units=units,
            pixel={"row": int(r), "col": int(c), "x": x, "y": y},
        )
        print(out, flush=True)
        return out
    except Exception as e:
        print(f"bad error {e}")
        raise


@app.post("/stats/geometry", response_model=StatsOut)
def geometry_stats(q: GeometryStatsIn):
    ds, nodata, units = _open_raster(q.raster_id)

    geom = shape(q.geometry)

    # Reproject geometry to raster CRS if needed
    if q.from_crs != ds.crs.to_string():
        transformer = Transformer.from_crs(q.from_crs, ds.crs, always_xy=True)
        geom = shp_transform(
            lambda x, y, z=None: transformer.transform(x, y), geom
        )

    # Window the raster to geometry bounds for efficiency
    window = rasterio.windows.from_bounds(*geom.bounds, transform=ds.transform)
    data = ds.read(1, window=window, masked=False)

    # Build geometry mask in the window’s transform
    window_transform = ds.window_transform(window)
    mask = geometry_mask(
        [mapping(geom)],
        transform=window_transform,
        invert=True,
        out_shape=data.shape,
    )

    # Apply mask
    masked = np.where(mask, data, np.nan).astype("float64")
    if nodata is not None:
        masked = np.where(np.isclose(masked, nodata), np.nan, masked)

    vals = masked[~np.isnan(masked)]
    stats = {}

    if vals.size == 0:
        stats = {"count": 0}
    else:
        if q.reducer in ("mean", "sum", "min", "max", "std", "median"):
            if q.reducer == "mean":
                stats["mean"] = float(np.mean(vals))
            elif q.reducer == "sum":
                stats["sum"] = float(np.sum(vals))
            elif q.reducer == "min":
                stats["min"] = float(np.min(vals))
            elif q.reducer == "max":
                stats["max"] = float(np.max(vals))
            elif q.reducer == "std":
                stats["std"] = float(
                    np.std(vals, ddof=1) if vals.size > 1 else 0.0
                )
            elif q.reducer == "median":
                stats["median"] = float(np.median(vals))
            stats["count"] = int(vals.size)
        elif q.reducer == "histogram":
            bins = q.histogram_bins or 16
            rng = q.histogram_range
            hist, bin_edges = np.histogram(vals, bins=bins, range=rng)
            stats = {
                "hist": hist.tolist(),
                "bin_edges": bin_edges.tolist(),
                "count": int(vals.size),
            }
        else:
            raise HTTPException(
                status_code=400, detail=f"unknown reducer: {q.reducer}"
            )

    return StatsOut(
        raster_id=q.raster_id,
        reducer=q.reducer,
        stats=stats,
        nodata=nodata if nodata is not None else None,
        units=units,
        geometry=q.geometry,
    )
