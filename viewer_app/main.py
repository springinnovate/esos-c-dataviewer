# viewer_app/main.py
from pathlib import Path
import os
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request

app = FastAPI(title="ESSOSC Viewer")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def _load_layers_config(cfg_path: str) -> dict:
    if not Path(cfg_path).exists():
        raise FileNotFoundError(cfg_path)
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _collect_layers(cfg: dict) -> list:
    layers = []
    workspace = cfg.get("workspaces")[0]["name"]
    for layer_id, layer in cfg.get("layers", []).items():
        if layer.get("type") != "raster_geotiff":
            continue
        style = Path(layer.get("default_style")).stem
        layers.append(
            {
                "workspace": workspace,
                "name": layer_id,
                "default_style": style,
            }
        )
    return layers


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/config")
def api_config():
    cfg_path = os.getenv("LAYERS_YAML_PATH")
    geoserver_base_url = os.getenv("GEOSERVER_BASE_URL").rstrip("/")
    rstats_base_url = os.getenv("RSTATS_BASE_URL").rstrip()
    try:
        cfg = _load_layers_config(cfg_path)
    except FileNotFoundError:
        raise HTTPException(
            status_code=500, detail=f"layers.yml not found at {cfg_path}"
        )
    return {
        "geoserver_base_url": geoserver_base_url,
        "layers": _collect_layers(cfg),
        "rstats_base_url": rstats_base_url,
    }
