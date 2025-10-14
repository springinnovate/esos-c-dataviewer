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


def _load_layers_config(config_path: str) -> dict:
    """Load a YAML layers configuration file.

    Args:
        config_path (str): Path to the configuration file.

    Returns:
        dict: Parsed configuration data.

    Raises:
        FileNotFoundError: If the configuration file does not exist.
    """
    if not Path(config_path).exists():
        raise FileNotFoundError(config_path)
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _collect_layers(config: dict) -> list:
    """Collect raster GeoTIFF layer metadata from configuration.

    Args:
        config (dict): Loaded configuration data.

    Returns:
        list: List of layer metadata dictionaries.
    """
    layers = []
    workspace = config.get("workspaces")[0]["name"]
    for layer_id, layer in config.get("layers", []).items():
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
    """Render the main viewer page.

    Args:
        request (Request): Incoming HTTP request.

    Returns:
        TemplateResponse: Rendered HTML response for the index page.
    """
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/config")
def api_config():
    """Return API configuration for GeoServer and raster stats services.

    Returns:
        dict: Configuration including base URLs and layer definitions.

    Raises:
        HTTPException: If the configuration file is missing or unreadable.
    """
    config_path = os.getenv("LAYERS_YAML_PATH")
    geoserver_base_url = os.getenv("GEOSERVER_BASE_URL").rstrip("/")
    rstats_base_url = os.getenv("RSTATS_BASE_URL").rstrip()
    try:
        config = _load_layers_config(config_path)
    except FileNotFoundError:
        raise HTTPException(
            status_code=500, detail=f"layers.yml not found at {config_path}"
        )
    return {
        "geoserver_base_url": geoserver_base_url,
        "layers": _collect_layers(config),
        "rstats_base_url": rstats_base_url,
    }
