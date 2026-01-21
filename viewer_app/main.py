"""ESSOSC Viewer FastAPI application.

This module defines the main FastAPI application for the ESSOSC Viewer.
It serves a simple web interface for visualizing raster layers hosted on
a GeoServer instance and accessing associated raster statistics through
a REST API.

The application provides:
    * A root endpoint ("/") rendering the main viewer HTML page.
    * An API endpoint ("/api/config") returning configuration metadata,
      including GeoServer and raster stats base URLs, and available raster
      layers parsed from a YAML configuration file.

Environment Variables:
    LAYERS_YAML_PATH (str): Path to the YAML configuration file defining
        workspaces, styles, and layers.
    GEOSERVER_BASE_URL (str): Base URL for the GeoServer service.
    RSTATS_BASE_URL (str): Base URL for the raster statistics service.

Functions:
    _load_layers_config(config_path): Load and parse the YAML configuration file.
    _collect_layers(config): Extract raster GeoTIFF layer definitions.
    index(request): Render the main viewer page.
    api_config(): Return combined configuration for front-end initialization.
"""

from pathlib import Path
from typing import Optional
import logging
import os
import json

from fastapi import FastAPI, HTTPException
from fastapi import Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="ESSOSC Viewer")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

_manifest_path = Path("static/manifest.json")
if not _manifest_path.exists():
    raise RuntimeError(f"Missing manifest.json at {_manifest_path}")

_manifest = json.loads(_manifest_path.read_text(encoding="utf-8"))


def _asset_path(key: str) -> str:
    entry = _manifest[key]  # let KeyError propagate if missing
    return f"static/{entry['file']}"


def _css_paths(key: str) -> list[str]:
    entry = _manifest[key]
    return [f"static/{c}" for c in entry.get("css", [])]


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
    workspace_id = config["workspace_id"]
    for layer in config.get("layers", []).values():
        layer_name = Path(layer["file_path"]).stem
        layers.append(
            {
                "workspace": workspace_id,
                # the geoserver inserts these as lowercase
                "name": layer_name.lower(),
            }
        )
    logging.debug(f"layers: {layers}")
    return layers


def _read_version():
    """Return the application version from environment or a baked file.

    This function checks the 'APP_VERSION' environment variable first. If it is
    unset or empty, it attempts to read a pre-baked version string from
    '/app/version'. If the file is missing, the function returns 'dev'. Other I/O
    errors (e.g., permission errors) will propagate.

    Returns:
        str: A semantic or git-derived version string, or 'dev' if no version
        information is available.
    """
    v = os.getenv("APP_VERSION")
    if v:
        return v
    try:
        with open("/app/version", "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "dev"


@app.get("/")
async def index(
    request: Request, layerA: Optional[str] = None, layerB: Optional[str] = None
):
    """Render the main viewer page.

    This endpoint serves the index template and optionally initializes the A/B
    layers from query parameters.

    Args:
        request (Request): Incoming FastAPI request object.
        layerA (Optional[str]): Optional initial layer identifier for panel A,
            taken from the 'layerA' query parameter.
        layerB (Optional[str]): Optional initial layer identifier for panel B,
            taken from the 'layerB' query parameter.

    Returns:
        TemplateResponse: Rendered 'index.html' with initial layer state,
        asset paths, and application version.
    """
    initial_layers = {"A": layerA or "", "B": layerB or ""}
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "initial_layers": initial_layers,
            "main_js": _asset_path("app.js"),
            "main_css_list": _css_paths("app.js"),
            "app_version": _read_version(),
        },
    )


@app.get("/api/config")
def api_config():
    """Return API configuration for GeoServer and raster stats services.

    Returns:
        dict: Configuration including base URLs and layer definitions.

    Raises:
        HTTPException: If the configuration file is missing or unreadable.
    """
    config_path = os.getenv("LAYERS_YAML_PATH")
    try:
        config = _load_layers_config(config_path)
    except FileNotFoundError:
        raise HTTPException(
            status_code=500, detail=f"layers.yml not found at {config_path}"
        )
    return {
        "geoserver_base_url": os.getenv("GEOSERVER_BASE_URL").rstrip("/"),
        "layers": _collect_layers(config),
        "rstats_base_url": os.getenv("RSTATS_BASE_URL").strip(),
        "global_crs": os.getenv("GLOBAL_CRS").strip(),
    }
