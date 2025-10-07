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

from typing import Any
from pathlib import Path
import argparse
import os
import sys
import time

from dotenv import load_dotenv
import requests
import yaml

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


def create_workspace_if_missing(
    geoserver_client: Gs, name: str, make_default: bool
) -> None:
    """Creates a GeoServer workspace if it does not already exist.

    This function checks whether a workspace with the specified name exists in the
    GeoServer instance. If it does not exist, a new workspace is created. Optionally,
    the workspace can be set as the default workspace for subsequent REST operations.

    Args:
        geoserver_client (Gs): An authenticated GeoServer REST client.
        name (str): The name of the workspace to create.
        namespace_uri (str, optional): The namespace URI associated with the workspace.
            If not provided, GeoServer will assign one automatically. Defaults to None.
        make_default (bool, optional): Whether to make this workspace the default
            workspace in GeoServer. Defaults to False.

    Raises:
        RuntimeError: If the workspace creation request fails with a non-success status.
    """
    r = geoserver_client.get(f"/rest/workspaces/{name}.json")
    if r.status_code == 200:
        if make_default:
            geoserver_client.put(
                "/rest/workspaces/default.json", {"workspace": {"name": name}}
            )
        return
    payload = {"workspace": {"name": name}}
    payload["workspace"]["isolated"] = False
    payload["workspace"]["namespace"] = {
        "name": name,
        "atom:link": [],
    }
    r = geoserver_client.post("/rest/workspaces", payload)
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"workspace create failed {name}: {r.status_code} {r.text}"
        )
    if make_default:
        geoserver_client.put(
            "/rest/workspaces/default.json", {"workspace": {"name": name}}
        )


def create_layer_if_not_exists(
    geoserver_client: Gs,
    workspace_name: str,
    geotiff_path: str,
    spatial_ref_system: str,
    default_style_name: str,
) -> str:
    """Publishes a GeoTIFF as a new raster layer if it does not already exist.

    Registers a GeoTIFF file in GeoServer by creating a coverage store,
    defining the coverage (resource), and publishing it as a WMS/WCS layer
    using the specified default style. If the layer already exists, no changes
    are made.

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
    coverage_name = Path(geotiff_path).stem
    store_name = f"{coverage_name}_store"

    # Create the coverage store
    store_payload = {
        "coverageStore": {
            "name": store_name,
            "type": "GeoTIFF",
            "enabled": True,
            "workspace": workspace_name,
            "url": f"file:{geotiff_path}",
        }
    }

    store_response = geoserver_client.post(
        f"/rest/workspaces/{workspace_name}/coveragestores", store_payload
    )
    if store_response.status_code not in (200, 201):
        raise RuntimeError(
            f"Coverage store creation failed {workspace_name}:{coverage_name}: "
            f"{store_response.status_code} {store_response.text}"
        )

    # Create the coverage resource
    coverage_payload = {
        "coverage": {
            "name": coverage_name,
            "nativeName": coverage_name,
            "enabled": True,
            "srs": spatial_ref_system,
            "projectionPolicy": "FORCE_DECLARED",
        }
    }

    coverage_response = geoserver_client.post(
        f"/rest/workspaces/{workspace_name}/coveragestores/{store_name}/coverages",
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
            f"Setting default style failed {workspace_name}:{coverage_name}:{default_style_name}: "
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
    """Main entry point for GeoServer initialization and configuration."""

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
        create_workspace_if_missing(
            geoserver_client,
            workspace_def["name"],
            workspace_def.get("default", False),
        )

    for style_def in config_data.get("styles", []):
        create_style_if_not_exists(
            geoserver_client,
            style_def["workspace"],
            style_def["name"],
            style_def.get("format", "sld"),
            style_def["file_path"],
        )

    for layer_def in config_data.get("layers", []):
        print(f"Working on layer definition: {layer_def}")

        layer_type = layer_def["type"]
        workspace_name = layer_def["workspace"]
        default_style = layer_def.get("default_style")
        spatial_ref_system = layer_def.get("srs")
        file_path = layer_def["file_path"]

        if layer_type == "raster_geotiff":
            create_layer_if_not_exists(
                geoserver_client,
                workspace_name,
                file_path,
                spatial_ref_system,
                default_style,
            )
        else:
            raise ValueError(f"Unknown layer type: {layer_type}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
