# geoserver_register.py
from typing import Any, Dict
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
    def __init__(
        self, base_url: str, user: str, password: str, timeout: int = 20
    ):
        self.base = base_url.rstrip("/")
        self.auth = (user, password)
        self.timeout = timeout
        self.headers_xml = {"Content-Type": "text/xml"}
        self.headers_json = {"Content-Type": "application/json"}

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def get(
        self, path: str, accept: str = "application/json"
    ) -> requests.Response:
        return requests.get(
            self._url(path),
            auth=self.auth,
            headers={"Accept": accept},
            timeout=self.timeout,
            verify=False,
        )

    def post(
        self, path: str, data: Any, json: bool = True
    ) -> requests.Response:
        if json:
            return requests.post(
                self._url(path),
                auth=self.auth,
                headers=self.headers_json,
                json=data,
                timeout=self.timeout,
                verify=False,
            )
        return requests.post(
            self._url(path),
            auth=self.auth,
            headers=self.headers_xml,
            data=data,
            timeout=self.timeout,
            verify=False,
        )

    def put(self, path: str, data: Any, json: bool = True) -> requests.Response:
        if json:
            return requests.put(
                self._url(path),
                auth=self.auth,
                headers=self.headers_json,
                json=data,
                timeout=self.timeout,
                verify=False,
            )
        return requests.put(
            self._url(path),
            auth=self.auth,
            headers=self.headers_xml,
            data=data,
            timeout=self.timeout,
            verify=False,
        )

    def delete(self, path: str) -> requests.Response:
        return requests.delete(
            self._url(path), auth=self.auth, timeout=self.timeout, verify=False
        )


def ensure_workspace(
    gs: Gs, name: str, namespace_uri: str = None, make_default: bool = False
) -> None:
    r = gs.get(f"/rest/workspaces/{name}.json")
    if r.status_code == 200:
        if make_default:
            gs.put(
                "/rest/workspaces/default.json", {"workspace": {"name": name}}
            )
        return
    payload = {"workspace": {"name": name}}
    if namespace_uri:
        payload["workspace"]["isolated"] = False
        payload["workspace"]["namespace"] = {
            "name": name,
            "atom:link": [],
            "href": namespace_uri,
        }
    r = gs.post("/rest/workspaces", payload)
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"workspace create failed {name}: {r.status_code} {r.text}"
        )
    if make_default:
        gs.put("/rest/workspaces/default.json", {"workspace": {"name": name}})


def ensure_coverage_geotiff(
    gs: Gs, ws: str, geotiff_path: str, srs: str, default_style: str
) -> str:
    """
    Create (or no-op) the raster resource (coverage) under an existing GeoTIFF coverageStore.
    Returns the coverage name to use for subsequent layer/style ops.
    """
    # derive a safe default name from the file basename
    cov_name = os.path.splitext(os.path.basename(geotiff_path))[0]
    store_name = f"{cov_name}_store"
    payload = {
        "coverageStore": {
            "name": store_name,  # published resource name
            "type": "GeoTIFF",
            "enabled": True,
            "workspace": ws,
            "url": f"file:{geotiff_path}",
        }
    }
    r = gs.post(f"/rest/workspaces/{ws}/coveragestores", payload)
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"coverage publish failed {ws}:{cov_name}: {r.status_code} {r.text}"
        )

    cov_payload = {
        "coverage": {
            "name": cov_name,
            "nativeName": cov_name,
            "enabled": True,
            "srs": srs,
            "projectionPolicy": "FORCE_DECLARED",
        }
    }

    r = gs.post(
        f"/rest/workspaces/{ws}/coveragestores/{store_name}/coverages",
        cov_payload,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"coverage creation failed {ws}:{cov_name}: {r.status_code} {r.text}"
        )

    r = gs.put(
        f"/rest/layers/{ws}:{cov_name}.json",
        {
            "layer": {
                "defaultStyle": {"name": default_style},
                "enabled": True,
            }
        },
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"set style failed {ws}:{cov_name}:{default_style}: {r.status_code} {r.text}"
        )

    return cov_name


def discover_first_coverage_name(
    gs: Gs, ws: str, store: str, retries: int = 10, delay: int = 2
) -> str:
    for attempt in range(retries):
        r = gs.get(
            f"/rest/workspaces/{ws}/coveragestores/{store}/coverages.json"
        )
        if r.status_code != 200:
            time.sleep(delay)
            continue

        try:
            data = r.json()
        except ValueError:
            # Not valid JSON yet — retry
            time.sleep(delay)
            continue

        # Sometimes GeoServer returns {'coverages': ''} — skip that
        if not isinstance(data.get("coverages"), dict):
            time.sleep(delay)
            continue

        covs = data["coverages"].get("coverage", [])
        if isinstance(covs, dict):
            covs = [covs]
        if covs:
            return covs[0]["name"]

        time.sleep(delay)

    raise RuntimeError(
        f"no coverages found under store {ws}:{store} after {retries} attempts"
    )


def recalc_coverage_bounds(gs: Gs, ws: str, store: str, name: str) -> None:
    # recompute native & geographic bounds; helps avoid disabled layers
    r = requests.put(
        gs._url(
            f"/rest/workspaces/{ws}/coveragestores/{store}/coverages/{name}.json?recalculate=nativebbox,latlonbbox"
        ),
        auth=gs.auth,
        headers=gs.headers_json,
        json={"coverage": {"enabled": True}},
        timeout=gs.timeout,
        verify=False,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"bbox recalc failed {ws}:{name}: {r.status_code} {r.text}"
        )


def set_default_style(
    gs: Gs, ws: str, layer_name: str, style_name: str
) -> None:
    payload = {"layer": {"defaultStyle": {"name": style_name}, "enabled": True}}
    r = gs.put(f"/rest/layers/{ws}:{layer_name}.json", payload)
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"set style failed {ws}:{layer_name}:{style_name}: {r.status_code} {r.text}"
        )


def ping_until_up(gs: Gs, timeout_sec: int = 120) -> None:
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            r = gs.get("/rest/about/version.json")
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise TimeoutError("geoserver REST did not become ready")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config", help="path to layers.yml")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        raw_yaml = f.read()
    expanded_yaml = os.path.expandvars(raw_yaml)
    cfg = yaml.safe_load(expanded_yaml)
    base_url = cfg["geoserver"]["base_url"]
    user = cfg["geoserver"]["user"]
    pw = cfg["geoserver"]["password"]
    default_ws = cfg["geoserver"].get("default_workspace")

    gs = Gs(base_url, user, pw)
    ping_until_up(gs)
    print("it is up!")

    for ws in cfg.get("workspaces", []):
        ensure_workspace(
            gs, ws["name"], ws.get("namespace_uri"), ws.get("default", False)
        )

    if default_ws:
        ensure_workspace(gs, default_ws)
    print("done with workspaces")

    for layer in cfg.get("layers", []):
        print(f"working on {layer}")
        t = layer["type"]
        ws = layer["workspace"]
        style = layer.get("default_style")
        srs = layer.get("srs")
        file_path = layer["file_path"]

        if t == "raster_geotiff":
            ensure_coverage_geotiff(gs, ws, file_path, srs, style)
        else:
            raise ValueError(f"unknown layer type: {t}")

    print("ok")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
