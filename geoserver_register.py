"""Script for GeoServer configuration automation.

Reads a YAML configuration file that defines workspaces, styles, and raster layers,
and applies those settings to a running GeoServer instance. Waits for GeoServer
to become ready before applying changes, and creates any missing resources as needed.

The configuration file should define sections for:
    - geoserver: connection details
    - workspaces: list of workspaces to create
    - styles: list of styles with their files and formats
    - layers: list of GeoTIFF-based raster layers to publish

Example:
    layers.yml:
        geoserver:
          base_url: http://geoserver:8080/geoserver
          user: admin
          password: geoserver
        workspaces:
          - name: my_workspace
            default: true
        styles:
          - name: my_style
            workspace: my_workspace
            format: sld
            file_path: /opt/geoserver/local_data/styles/my_style.sld
        layers:
          - type: raster_geotiff
            workspace: my_workspace
            file_path: /opt/geoserver/local_data/rasters/my_raster.tif
            srs: EPSG:3347
            default_style: my_style
"""

from pathlib import Path
from typing import Any
import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv
import requests
import yaml
import rasterio


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

requests.packages.urllib3.disable_warnings()  # noqa: E402

load_dotenv()


class Gs:
    """A lightweight GeoServer REST API client.

    This class provides convenience wrappers for HTTP methods (`GET`, `POST`,
    `PUT`, `DELETE`) against a GeoServer instance, handling authentication,
    content-type headers, and timeouts. It is designed to simplify creating,
    updating, and querying GeoServer resources (e.g., workspaces, stores,
    layers, and styles) via its REST interface.
    """

    def __init__(self, base_url: str, user: str, password: str, timeout: int):
        """Initializes the GeoServer REST client.

        Args:
            base_url (str): The base URL of the GeoServer REST API endpoint
                (for example, ``http://localhost:8080/geoserver``).
            user (str): The GeoServer username for basic authentication.
            password (str): The GeoServer password for basic authentication.
            timeout (int, optional): Timeout (in seconds) for HTTP requests.
        """
        self.base = base_url.rstrip("/")
        self.auth = (user, password)
        self.timeout = timeout
        self.headers_xml = {"Content-Type": "text/xml"}
        self.headers_json = {"Content-Type": "application/json"}

    def _url(self, path: str) -> str:
        """Builds a fully qualified GeoServer REST URL.

        Args:
            path (str): The REST API path, starting with ``/rest``.

        Returns:
            str: A full URL combining the base URL and the provided path.
        """
        return f"{self.base}{path}"

    def get(self, path: str) -> requests.Response:
        """Performs a GET request against the GeoServer REST API.

        Args:
            path (str): The REST API path (e.g., ``/rest/workspaces.json``).

        Returns:
            requests.Response: The HTTP response object.
        """
        return requests.get(
            self._url(path),
            auth=self.auth,
            headers={"Accept": "application/json"},
            timeout=self.timeout,
            verify=False,
        )

    def post(self, path: str, data: Any) -> requests.Response:
        """Performs a POST request to create a new GeoServer resource.

        Args:
            path (str): The REST API path for the resource creation endpoint.
            data (Any): The request payload, either a Python object (for JSON)
                or an XML string.

        Returns:
            requests.Response: The HTTP response object.
        """
        return requests.post(
            self._url(path),
            auth=self.auth,
            headers=self.headers_json,
            json=data,
            timeout=self.timeout,
            verify=False,
        )

    def put(self, path: str, data: Any) -> requests.Response:
        """Performs a PUT request to update an existing GeoServer resource.

        Args:
            path (str): The REST API path of the resource to update.
            data (Any): The request payload, either a Python object (for JSON)
                or an XML string.

        Returns:
            requests.Response: The HTTP response object.
        """
        return requests.put(
            self._url(path),
            auth=self.auth,
            headers=self.headers_json,
            json=data,
            timeout=self.timeout,
            verify=False,
        )

    def delete(self, path: str) -> requests.Response:
        """Send a DELETE request to a GeoServer REST endpoint.

        This method constructs a full GeoServer URL from the provided path and
        issues an HTTP DELETE request using the configured authentication and
        timeout.

        Args:
            path (str): The relative REST path to delete (e.g.,
                '/rest/workspaces/example').

        Returns:
            requests.Response: The HTTP response object returned by the
            GeoServer server.

        Raises:
            requests.RequestException: If the request fails due to a network
        """
        return requests.delete(
            self._url(path), auth=self.auth, timeout=self.timeout, verify=False
        )


def recreate_workspace(
    geoserver_client: Gs, workspace_name: str, make_default: bool
) -> None:
    delete_response = geoserver_client.delete(
        f"/rest/workspaces/{workspace_name}?recurse=true"
    )
    if delete_response.status_code in (200, 202, 204, 404):
        logger.info("Deleted workspace '%s' (if existed).", workspace_name)
    else:
        raise RuntimeError(
            f"Failed to delete workspace {workspace_name}: "
            f"{delete_response.status_code} {delete_response.text}"
        )

    create_payload = {"workspace": {"name": workspace_name}}
    create_response = geoserver_client.post("/rest/workspaces", create_payload)
    if create_response.status_code not in (200, 201):
        raise RuntimeError(
            f"Workspace creation failed {workspace_name}: "
            f"{create_response.status_code} {create_response.text}"
        )

    if make_default:
        default_resp = geoserver_client.put(
            "/rest/workspaces/default.json",
            {"workspace": {"name": workspace_name}},
        )
        if default_resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to make {workspace_name} default: "
                f"{default_resp.status_code} {default_resp.text}"
            )

    logger.info("Workspace '%s' (re)created successfully.", workspace_name)


def crs_info_from_rasterio_crs(crs):
    # prefer an EPSG code if present; otherwise fall back to WKT
    logger.info(f"processing crs from {crs}")
    try:
        auth, code = crs.to_authority() or (None, None)
        logger.info(f"got {auth} / {code}")
    except Exception:
        auth, code = None, None
    if auth == "EPSG" and code:
        return {
            "declared_srs": f"EPSG:{code}",
            "native_wkt": crs.to_wkt(),
            "policy": "FORCE_DECLARED",
        }
    else:
        # choose a declared CRS you want clients to see (e.g., EPSG:4326)
        return {
            "declared_srs": "EPSG:4326",
            "native_wkt": crs.to_wkt(),
            "policy": "REPROJECT_TO_DECLARED",
        }


def create_layer(
    geoserver_client: Gs,
    workspace_name: str,
    geotiff_path: str,
    spatial_ref_system: str,
    default_style_name: str,
) -> str:
    """Publishes a GeoTIFF as a new raster layer.

    Registers a GeoTIFF file in GeoServer by creating a coverage store,
    defining the coverage (resource), and publishing it as a WMS/WCS layer
    using the specified default style.

    Args:
        geoserver_client (Gs): An authenticated GeoServer REST client.
        workspace_name (str): The workspace in which to publish the layer.
        geotiff_path (str): The absolute path to the GeoTIFF file inside the
            GeoServer data directory or mounted volume.
        spatial_ref_system (str): The EPSG code for the spatial reference
            system (e.g., ``"EPSG:3347"``).
        default_style_name (str): The name of the default style to apply to
            the published layer.

    Returns:
        str: The name of the published coverage/layer.

    Raises:
        RuntimeError: If any REST API request for creating the store,
            coverage, or layer fails.
    """
    # coverage is confusing but it's an internal metadata object that is
    # created when a raster layer is created, so by creating a "coverage" you
    # create a raster layer...
    coverage_name = Path(geotiff_path).stem

    # the store is where the data are 'stored' and a coveragestore is where
    # raster data are stored
    coveragestore_name = f"{coverage_name}_store"

    coveragestore_payload = {
        "coverageStore": {
            "name": coveragestore_name,
            "type": "GeoTIFF",
            "enabled": True,
            "workspace": workspace_name,
            "url": f"file://{geotiff_path}",
        }
    }
    store_response = geoserver_client.post(
        f"/rest/workspaces/{workspace_name}/coveragestores",
        coveragestore_payload,
    )
    if store_response.status_code not in (200, 201):
        raise RuntimeError(
            f"Coverage store creation failed {workspace_name}:{coverage_name}: "
            f"{store_response.status_code} {store_response.text}"
        )

    info = crs_info_from_rasterio_crs(spatial_ref_system)
    coverage_payload = {
        "coverage": {
            "name": coverage_name,
            "nativeName": coverage_name,
            "enabled": True,
            "projectionPolicy": info["policy"],
            "srs": info["declared_srs"],
            "nativeCRS": info["native_wkt"],
            "requestSRS": {"string": ["EPSG:4326", "EPSG:3857"]},
            "responseSRS": {"string": ["EPSG:4326", "EPSG:3857"]},
        }
    }

    coverage_response = geoserver_client.post(
        f"/rest/workspaces/{workspace_name}/coveragestores"
        f"/{coveragestore_name}/coverages",
        coverage_payload,
    )
    if coverage_response.status_code not in (200, 201):
        raise RuntimeError(
            f"Coverage creation failed {workspace_name}:{coverage_name}: "
            f"{coverage_response.status_code} {coverage_response.text}"
        )

    # Set the layer style and enable the layer
    style_payload = {
        "layer": {
            "defaultStyle": {"name": default_style_name},
            "enabled": True,
        }
    }

    style_response = geoserver_client.put(
        f"/rest/layers/{workspace_name}:{coverage_name}.json",
        style_payload,
    )
    if style_response.status_code not in (200, 201):
        raise RuntimeError(
            f"Setting default style failed "
            f"{workspace_name}:{coverage_name}:{default_style_name}: "
            f"{style_response.status_code} {style_response.text}"
        )

    return coverage_name


def ping_until_up(geoserver_client: Gs, timeout_sec: int) -> None:
    """Waits for the GeoServer REST API to become available.

    This function repeatedly polls the GeoServer REST endpoint until a successful
    connection is established or the timeout is reached. It is useful for ensuring
    GeoServer is ready before attempting configuration operations during startup.

    Args:
        geoserver_client (Gs): An authenticated GeoServer REST client.
        timeout_sec (int): The maximum time (in seconds) to wait for GeoServer to
            respond.

    Raises:
        TimeoutError: If GeoServer does not respond with HTTP 200 within the timeout.
    """
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            r = geoserver_client.get("/rest/about/version.json")
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise TimeoutError("geoserver REST did not become ready")


def create_style_if_not_exists(
    geoserver_client: Gs,
    workspace_name: str,
    style_name: str,
    style_format: str,
    style_file_path: str,
) -> None:
    """Creates or updates a style definition in GeoServer.

    Ensures that a style with the specified name exists within the given workspace.
    If the style does not exist, this function creates a new entry and uploads the
    style file (SLD, GeoCSS, MBStyle, or YSLD). If the style already exists, its
    contents are overwritten.

    Args:
        geoserver_client (Gs): Authenticated GeoServer REST client.
        workspace_name (str): The name of the workspace where the style resides.
        style_name (str): The name of the style to create or update.
        style_format (str): The style format ("sld", "geocss", "mbstyle", or "ysld").
        style_file_path (str): The absolute path to the style file to upload.

    Raises:
        ValueError: If the specified format is not one of the supported formats.
        RuntimeError: If the style creation or upload fails.
    """
    style_format = style_format.lower()
    style_format_map = {
        "sld": ("application/vnd.ogc.sld+xml", ".sld", "sld"),
        "geocss": ("application/vnd.geoserver.geocss+css", ".css", "css"),
        "mbstyle": (
            "application/vnd.geoserver.mbstyle+json",
            ".mbstyle",
            "mbstyle",
        ),
        "ysld": ("application/vnd.geoserver.ysld+yaml", ".ysld", "ysld"),
    }

    if style_format not in style_format_map:
        raise ValueError(f"Unknown style format: {style_format}")

    content_type, file_extension, format_name = style_format_map[style_format]

    # Create style shell if missing
    response = geoserver_client.get(
        f"/rest/workspaces/{workspace_name}/styles/{style_name}.json"
    )
    if response.status_code != 200:
        style_payload = {
            "style": {
                "name": style_name,
                "workspace": workspace_name,
                "format": format_name,
                "filename": Path(style_file_path).name,
            }
        }
        response = geoserver_client.post(
            f"/rest/workspaces/{workspace_name}/styles", style_payload
        )
        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to create style {workspace_name}:{style_name}: "
                f"{response.status_code} {response.text}\nPayload: {style_payload}"
            )

    # Upload or update style body
    with open(style_file_path, "r", encoding="utf-8") as style_file:
        style_body = style_file.read().encode("utf-8")

    style_url = geoserver_client._url(
        f"/rest/workspaces/{workspace_name}/styles/{style_name}{file_extension}?raw=true"
    )
    upload_response = requests.put(
        style_url,
        auth=geoserver_client.auth,
        headers={"Content-Type": content_type},
        data=style_body,
        timeout=geoserver_client.timeout,
        verify=False,
    )
    if upload_response.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to upload style {workspace_name}:{style_name}: "
            f"{upload_response.status_code} {upload_response.text}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Configure GeoServer from a YAML definition file."
    )
    parser.add_argument(
        "config", help="Path to the layers.yml configuration file."
    )
    parsed_args = parser.parse_args()

    with open(parsed_args.config, "r", encoding="utf-8") as yaml_file:
        raw_yaml = yaml_file.read()

    expanded_yaml = os.path.expandvars(raw_yaml)
    config_data = yaml.safe_load(expanded_yaml)

    geoserver_base_url = config_data["geoserver"]["base_url"]
    geoserver_user = config_data["geoserver"]["user"]
    geoserver_password = config_data["geoserver"]["password"]

    timeout_seconds = 30
    geoserver_client = Gs(
        geoserver_base_url, geoserver_user, geoserver_password, timeout_seconds
    )

    seconds_to_wait_for_geoserver_start = 420
    ping_until_up(geoserver_client, seconds_to_wait_for_geoserver_start)

    for workspace_def in config_data.get("workspaces", []):
        recreate_workspace(
            geoserver_client,
            workspace_def["name"],
            workspace_def.get("default", False),
        )
        workspace_name = workspace_def["name"]

    for raster_id, layer_def in config_data.get("layers").items():
        style_path = layer_def["default_style"]
        create_style_if_not_exists(
            geoserver_client,
            workspace_name,
            Path(style_path).stem,
            "sld",
            style_path,
        )
        logger.info("Working on layer definition: %s", layer_def)
        layer_type = layer_def["type"]
        default_style = layer_def.get("default_style")
        file_path = layer_def["file_path"]

        with rasterio.open(file_path) as ds:
            ds_crs = ds.crs

        if layer_type == "raster_geotiff":
            create_layer(
                geoserver_client,
                workspace_name,
                file_path,
                ds_crs,
                default_style,
            )
        else:
            raise ValueError(f"Unknown layer type: {layer_type}")
    logger.info("All done.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Unhandled error during GeoServer configuration")
        sys.exit(1)
