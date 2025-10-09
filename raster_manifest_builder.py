import argparse
import sys
from pathlib import Path

import yaml
import rasterio


def find_geotiffs(root: Path, exts):
    for p in root.rglob("*"):
        if p.is_file():
            if p.suffix.lower() in exts:
                yield p


def crs_to_srs(crs):
    if not crs:
        return None
    try:
        epsg = crs.to_epsg()
        if epsg:
            return f"EPSG:{epsg}"
    except Exception:
        pass
    try:
        s = crs.to_string()
        return s if s else None
    except Exception:
        return None


def build_layer_entry(
    path: Path,
    base_dir: Path,
    raster_type: str,
    default_style: str | None,
    path_mode: str,
):
    stem = path.stem
    if path_mode == "relative":
        try:
            file_path = str(path.relative_to(base_dir))
        except Exception:
            file_path = str(path)
    else:
        file_path = str(path)

    entry = {}
    entry["type"] = raster_type
    entry["file_path"] = Path(file_path).as_posix()
    if default_style:
        entry["default_style"] = default_style
    return stem, entry


def main():
    parser = argparse.ArgumentParser(
        prog="layers_compiler",
        description="Recursively scan a directory for GeoTIFFs and emit YAML layer entries.",
    )
    parser.add_argument("directory", type=Path, help="Root directory to scan")
    parser.add_argument(
        "-w", "--workspace", default="esosc", help="Workspace name to assign"
    )
    parser.add_argument(
        "--store-suffix",
        default="_store",
        help="Suffix appended to store name (after filename stem)",
    )
    parser.add_argument(
        "-t",
        "--type",
        dest="raster_type",
        default="raster_geotiff",
        help="Layer type value",
    )
    parser.add_argument(
        "-s",
        "--default-style",
        dest="default_style",
        default=None,
        help="Default style name (optional)",
    )
    parser.add_argument(
        "--path-mode",
        choices=["absolute", "relative"],
        default="absolute",
        help="How to write file_path values",
    )
    parser.add_argument(
        "-e",
        "--ext",
        action="append",
        default=None,
        help="Additional file extensions (e.g., .tif .tiff). Case-insensitive.",
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Output YAML file (defaults to stdout)",
    )
    parser.add_argument(
        "--no-header",
        action="store_true",
        help="Emit only layers mapping (omit top-level geoserver/workspaces/styles placeholders)",
    )
    args = parser.parse_args()

    exts = set(
        e.lower() if e.startswith(".") else f".{e.lower()}"
        for e in (args.ext or [])
    )
    if not exts:
        exts = {".tif", ".tiff"}

    root = args.directory.resolve()
    if not root.exists() or not root.is_dir():
        print(f"error: directory not found: {root}", file=sys.stderr)
        sys.exit(2)

    layers = {}
    for tif in sorted(find_geotiffs(root, exts)):
        key, entry = build_layer_entry(
            path=tif,
            base_dir=root,
            raster_type=args.raster_type,
            default_style=args.default_style,
            path_mode=args.path_mode,
        )
        if key in layers:
            print(
                f'warn: duplicate layer id "{key}" from {tif}, skipping',
                file=sys.stderr,
            )
            continue
        layers[key] = entry

    doc = {}
    if args.no_header:
        doc["layers"] = layers
    else:
        doc["geoserver"] = dict(
            [
                ("base_url", "${GEOSERVER_INTERNAL_BASE_URL}"),
                ("user", "${GEOSERVER_ADMIN_USER}"),
                ("password", "${GEOSERVER_ADMIN_PASSWORD}"),
            ]
        )
        doc["workspaces"] = [{"name": args.workspace, "default": True}]
        doc["styles"] = []  # populate as needed
        doc["layers"] = layers

    yaml.SafeDumper.org_represent_str = yaml.SafeDumper.represent_str

    def _repr_str(dumper, data):
        if "\n" in data:
            return dumper.represent_scalar(
                "tag:yaml.org,2002:str", data, style="|"
            )
        return dumper.org_represent_str(data)

    yaml.SafeDumper.represent_str = _repr_str

    out_s = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    if args.out:
        args.out.write_text(out_s, encoding="utf-8")
    else:
        sys.stdout.write(out_s)


if __name__ == "__main__":
    main()
