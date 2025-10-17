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
from shutil import copy2
from typing import Any
import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv
from ecoshard import taskgraph
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject
import numpy as np
import psutil
import rasterio
import requests
import yaml


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

    # defining these so the identity is stable for the taskgraph
    def _key(self):
        return (self.base, self.auth[0], self._cred_fp, int(self.timeout))

    def __eq__(self, other):
        """Check equality between two GeoServer client instances.

        Args:
            other (Gs): Another GeoServer client to compare.

        Returns:
            bool: True if both instances have identical configuration parameters,
            False otherwise.
        """
        return isinstance(other, Gs) and self._key() == other._key()

    def __hash__(self):
        """Return a hash value based on the client's configuration.

        Returns:
            int: Deterministic hash value derived from the connection parameters.
        """
        return hash(self._key())

    def __repr__(self):
        """Return a readable and deterministic string representation of the client.

        Returns:
            str: A concise representation showing base URL, user, and timeout.
        """
        return f"Gs(base={self.base!r}, user={self.auth[0]!r}, timeout={self.timeout!r})"

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


def purge_and_create_workspace(
    geoserver_client: Gs, workspace_name: str
) -> None:
    """Deletes all existing GeoServer workspaces and creates a single new one.

    This deletes all workspaces from the GeoServer using its REST API.
    After purging, it creates a new workspace with the given name and sets it as
    the default workspace.

    Args:
        geoserver_client (Gs): An authenticated GeoServer client used to send REST requests.
        workspace_name (str): The name of the new workspace to create.

    Raises:
        RuntimeError: If any of the following operations fail:
            - Listing existing workspaces.
            - Deleting one or more workspaces.
            - Creating the new workspace.
            - Setting the new workspace as the default.

    """
    list_resp = geoserver_client.get("/rest/workspaces.json")
    if list_resp.status_code != 200:
        raise RuntimeError(
            f"Failed to list workspaces: {list_resp.status_code} {list_resp.text}"
        )

    data = list_resp.json()
    workspaces = data.get("workspaces", {}).get("workspace", [])
    if not isinstance(workspaces, list):
        workspaces = [workspaces]

    for ws in workspaces:
        name = ws.get("name")
        if not name:
            continue
        del_resp = geoserver_client.delete(
            f"/rest/workspaces/{name}?recurse=true"
        )
        if del_resp.status_code in (200, 202, 204, 404):
            logger.info("Deleted workspace '%s' (if existed).", name)
        else:
            raise RuntimeError(
                f"Failed to delete workspace {name}: "
                f"{del_resp.status_code} {del_resp.text}"
            )

    # create the single new workspace
    create_payload = {"workspace": {"name": workspace_name}}
    create_response = geoserver_client.post("/rest/workspaces", create_payload)
    if create_response.status_code not in (200, 201):
        raise RuntimeError(
            f"Workspace creation failed {workspace_name}: "
            f"{create_response.status_code} {create_response.text}"
        )

    default_resp = geoserver_client.put(
        "/rest/workspaces/default.json",
        {"workspace": {"name": workspace_name}},
    )
    if default_resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to make {workspace_name} default: "
            f"{default_resp.status_code} {default_resp.text}"
        )

    logger.info(
        "Workspace '%s' (re)created successfully after clearing all workspaces.",
        workspace_name,
    )

    logger.info("Workspace '%s' (re)created successfully.", workspace_name)


def crs_info_from_rasterio_crs(crs):
    """Extract CRS information from a rasterio CRS object.

    Args:
        crs (rasterio.crs.CRS): The CRS object to process.

    Returns:
        dict: CRS metadata including declared SRS, native WKT, and reprojection policy.
    """
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
    raster_name: str,
    geotiff_path: str,
    default_style_name: str,
) -> str:
    """Publishes a GeoTIFF as a new raster layer.

    Registers a GeoTIFF file in GeoServer by creating a coverage store,
    defining the coverage (resource), and publishing it as a WMS/WCS layer
    using the specified default style.

    Args:
        geoserver_client (Gs): An authenticated GeoServer REST client.
        workspace_name (str): The workspace in which to publish the layer.
        raster_name (str): The id/name used to register the layer in Geoserver
        geotiff_path (str): The absolute path to the GeoTIFF file inside the
            GeoServer data directory or mounted volume.
        spatial_ref_system (str): The EPSG code for the spatial reference
            system (e.g., ``"EPSG:3347"``).
        default_style_name (str): The name of the default style to apply to
            the published layer.

    Raises:
        RuntimeError: If any REST API request for creating the store,
            coverage, or layer fails.
    """
    with rasterio.open(geotiff_path) as ds:
        spatial_ref_system = ds.crs
    # the store is where the data are 'stored' and a coveragestore is where
    # raster data are stored
    coveragestore_name = f"{raster_name}_store"

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
            f"Coverage store creation failed {workspace_name}:{raster_name}: "
            f"{store_response.status_code} {store_response.text}"
        )

    info = crs_info_from_rasterio_crs(spatial_ref_system)
    coverage_payload = {
        "coverage": {
            "name": raster_name,
            "nativeName": raster_name,
            "enabled": True,
            "projectionPolicy": info["policy"],
            "srs": info["declared_srs"],
            "nativeCRS": info["native_wkt"],
            # These are hard-coded to allow common requests and responses in
            # common web and geographic CRSs EPSG:4326 (lat/lon) and
            # EPSG:3857 (Web Mercator) without allowing just any projection
            # you want, I'm not sure these are necessary but if it breaks
            # it will be for a good reason we can figure out then.
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
            f"Coverage creation failed {workspace_name}:{raster_name}: "
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
        f"/rest/layers/{workspace_name}:{raster_name}.json",
        style_payload,
    )
    if style_response.status_code not in (200, 201):
        raise RuntimeError(
            f"Setting default style failed "
            f"{workspace_name}:{raster_name}:{default_style_name}: "
            f"{style_response.status_code} {style_response.text}"
        )


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


def _pick_resampling(
    dtype: str, explicit: Resampling | None = None
) -> Resampling:
    """Select an appropriate resampling method for the given data type.

    Args:
        dtype (str): The NumPy dtype string of the raster band (e.g., 'uint16', 'float32').
        explicit (Resampling | None): Optional. If provided, this resampling method
            will be used directly instead of selecting one automatically.

    Returns:
        Resampling: The chosen resampling method. Uses `Resampling.nearest` for
        integer or unsigned types and `Resampling.bilinear` for others.
    """
    if explicit is not None:
        return explicit
    kind = np.dtype(dtype).kind
    return Resampling.nearest if kind in ("i", "u") else Resampling.bilinear


def _build_overviews_inplace(
    tif_path: str | Path,
    factors: tuple[int, ...],
) -> None:
    """Build internal overviews for a GeoTIFF file in place.

    Args:
        tif_path (str | Path): Path to the GeoTIFF file to process.
        factors (tuple[int, ...]): Downsampling factors to build overviews at
            (e.g., (2, 4, 8)).

    Returns:
        None: This function modifies the GeoTIFF file in place by adding overview levels.

    Notes:
        Overviews are generated with LZW compression, pixel interleaving, and
        a block size of 256. The function automatically chooses a suitable
        resampling method based on the raster's data type.
    """
    env = {
        "COMPRESS_OVERVIEW": "LZW",
        "INTERLEAVE_OVERVIEW": "PIXEL",
        "BIGTIFF_OVERVIEW": "IF_SAFER",
        "GDAL_TIFF_OVR_BLOCKSIZE": "256",
    }
    with rasterio.Env(**env):
        with rasterio.open(tif_path, "r+") as ds:
            rs = _pick_resampling(ds.dtypes[0])
            ds.build_overviews(factors, rs)
            ds.update_tags(ns="rio_overview", resampling=rs.name)


def reproject_and_build_overviews_if_needed(
    src_path: Path,
    target_projection: str,
    resampling: Resampling,
    dst_path: Path,
) -> str:
    """Reproject a raster to a target projection if not already aligned.

    This function checks whether a source raster matches the target CRS. If it
    differs, the raster is reprojected using a specified resampling method and
    written to a new file. If the raster is already in the target projection,
    no reprojection occurs and the original path is returned. Additionally,
    internal overviews are built to improve rendering performance.

    Args:
        src_path (Path): Path to the source raster file.
        target_projection (str): Target projection (e.g., 'EPSG:4326' or 'EPSG:3857').
        resampling (Resampling): Resampling method to use during reprojection.
            Defaults to nearest-neighbor for categorical data if unspecified.
        dst_path (Path): Output path for the reprojected raster.
            If None, a new filename is generated with the EPSG code appended.

    Raises:
        ValueError: If the source raster lacks a valid CRS.

    """
    with rasterio.Env(GDAL_NUM_THREADS="ALL_CPUS"):
        with rasterio.open(src_path) as src:
            # dynamic overview factors: include 2**k while min_side / 2**k > 256
            m = min(src.width, src.height)
            factors = []
            k = 1
            while m / (2**k) > 256:
                factors.append(2**k)
                k += 1
            overview_factors = tuple(factors)
            src_crs = src.crs
            if src_crs is not None and CRS.from_user_input(
                src_crs
            ) == CRS.from_user_input(target_projection):
                copy2(src_path, dst_path)
                out_path = str(dst_path)

                _build_overviews_inplace(
                    out_path,
                    overview_factors,
                )

            if src_crs is None:
                raise ValueError(
                    "source raster has no CRS; cannot reproject reliably"
                )

            dst_crs = CRS.from_user_input(target_projection)

            transform, width, height = calculate_default_transform(
                src_crs, dst_crs, src.width, src.height, *src.bounds
            )

            dst_profile = src.profile.copy()
            dst_profile.update(
                {
                    "crs": dst_crs,
                    "transform": transform,
                    "width": width,
                    "height": height,
                    "compress": "lzw",
                    "tiled": True,
                    "blockxsize": 256,
                    "blockysize": 256,
                    "BIGTIFF": "IF_SAFER",
                }
            )
            rs = _pick_resampling(src.dtypes[0], resampling)

            os.makedirs(dst_path.parent, exist_ok=True)

            with rasterio.open(dst_path, "w", **dst_profile) as dst:
                if src.nodata is not None:
                    dst.nodata = src.nodata
                for bidx in range(1, src.count + 1):
                    reproject(
                        source=rasterio.band(src, bidx),
                        destination=rasterio.band(dst, bidx),
                        src_transform=src.transform,
                        src_crs=src_crs,
                        dst_transform=transform,
                        dst_crs=dst_crs,
                        resampling=rs,
                        num_threads=0,
                    )

    out_path = str(dst_path)
    _build_overviews_inplace(out_path, overview_factors)
    return out_path


def main():
    """Configure a GeoServer instance from a YAML definition file.

    This function parses a configuration YAML file defining GeoServer connection
    parameters, workspaces, styles, and raster layers. It initializes a GeoServer
    REST client, ensures the server is online, recreates the target workspace,
    uploads defined styles, and processes each raster for publication. Raster
    processing tasks (including reprojection and registration) are executed in
    parallel using a TaskGraph for efficiency.

    Command-Line Arguments:
        config (str): Path to the YAML configuration file (e.g., `layers.yml`).

    Workflow:
        1. Load and expand environment variables in the YAML configuration.
        2. Initialize a `Gs` GeoServer client with credentials and timeout.
        3. Wait until the GeoServer instance responds to REST API pings.
        4. Purge and recreate the target workspace.
        5. Upload or create all defined styles.
        6. Schedule raster processing and layer creation tasks.
        7. Wait for all tasks to complete and close the TaskGraph.

    Raises:
        FileNotFoundError: If the configuration file cannot be found or opened.
        KeyError: If required keys are missing from the YAML configuration.
        Exception: For unexpected errors during GeoServer configuration or task execution.

    Returns:
        None
    """
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

    target_projection = config_data["target_projection"]
    local_working_dir = Path(config_data["local_working_dir"])
    local_working_dir.mkdir(parents=True, exist_ok=True)
    task_graph = taskgraph.TaskGraph(
        local_working_dir, psutil.cpu_count(logical=False), 15.0
    )

    timeout_seconds = 30
    geoserver_client = Gs(
        geoserver_base_url, geoserver_user, geoserver_password, timeout_seconds
    )

    seconds_to_wait_for_geoserver_start = 420
    ping_until_up(geoserver_client, seconds_to_wait_for_geoserver_start)

    workspace_id = config_data["workspace_id"]
    purge_and_create_workspace(
        geoserver_client,
        workspace_id,
    )

    style_path = config_data["style"]
    style_id = Path(style_path).stem
    create_style_if_not_exists(
        geoserver_client,
        workspace_id,
        style_id,
        "sld",
        style_path,
    )

    for raster_id, layer_def in config_data.get("layers").items():
        logger.info("Working on layer definition: %s", layer_def)
        file_path = Path(layer_def["file_path"])
        target_path = local_working_dir / Path(file_path).name

        process_task = task_graph.add_task(
            func=reproject_and_build_overviews_if_needed,
            args=(
                file_path,
                target_projection,
                Resampling.nearest,
                target_path,
            ),
            target_path_list=[target_path],
            task_name=f"process {raster_id}",
        )
        task_graph.add_task(
            func=create_layer,
            args=(
                geoserver_client,
                workspace_id,
                raster_id.lower(),
                file_path,
                style_id,
            ),
            dependent_task_list=[process_task],
            task_name=f"create layer {raster_id}",
            transient_run=True,
        )
    task_graph.join()
    task_graph.close()
    logger.info("All done.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Unhandled error during GeoServer configuration")
        sys.exit(1)
