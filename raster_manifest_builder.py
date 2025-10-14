"""Layer manifest compiler and style generator.

This module scans a directory recursively for GeoTIFF rasters, automatically
builds YAML configuration entries for GeoServer, and generates simple
Sequential color-ramp SLD styles for each raster. The resulting YAML document
is printed to standard output and can be used to initialize GeoServer layers.
"""

from pathlib import Path
import argparse
import logging
import math
import sys
import time

import numpy as np
import rasterio
import yaml


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def find_geotiffs(root: Path, exts):
    """Recursively yield GeoTIFF paths under a directory.

    Args:
        root (Path): Directory to search.
        exts (set[str]): Allowed file extensions (e.g., {'.tif', '.tiff'}).

    Yields:
        Path: Paths to matching GeoTIFF files.
    """
    for p in root.rglob("*"):
        if p.is_file():
            if p.suffix.lower() in exts:
                yield p


def crs_to_srs(crs):
    """Convert a rasterio CRS to an EPSG or PROJ string.

    Args:
        crs (rasterio.crs.CRS): CRS object.

    Returns:
        str | None: Corresponding EPSG code (e.g., 'EPSG:4326') or CRS string,
        or None if conversion fails.
    """
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
    raster_type: str,
    default_style: str | None,
):
    """Build a layer configuration entry for a raster.

    Args:
        path (Path): Path to the raster file.
        raster_type (str): Type label for the raster (e.g., 'raster_geotiff').
        default_style (str | None): Path to the default SLD style.

    Returns:
        tuple[str, dict]: Layer key and metadata dictionary.
    """
    stem = path.stem
    entry = {}
    entry["type"] = raster_type
    entry["file_path"] = Path(path).as_posix()
    if default_style:
        entry["default_style"] = default_style
    return stem, entry


def generate_dynamic_sld(
    raster_path: str | Path,
    styles_root: str | Path,
    n_colors: int = 7,
) -> str:
    """Generate and write a sequential color-ramp SLD for a raster.

    Samples valid pixels from a raster and computes a 5th–95th percentile range
    to avoid outliers. Builds a color ramp with `n_colors` steps and writes a
    style file named '<stem>_default_style.sld' under '<styles_root>/styles'.

    Args:
        raster_path (str | Path): Path to the input raster file.
        styles_root (str | Path): Directory where the 'styles' folder will be created.
        n_colors (int, optional): Number of colors in the ramp. Defaults to 7.
        logger (logging.Logger | None, optional): Optional logger instance.

    Returns:
        str: Filesystem path to the generated SLD file.
    """
    t0 = time.perf_counter()
    logger.info("SLD generation started: raster=%s", raster_path)

    raster_path = Path(raster_path)
    styles_root = Path(styles_root)
    styles_dir = styles_root / "styles"
    styles_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Ensured styles directory exists: %s", styles_dir)

    style_stem = f"{raster_path.stem}_default_style"
    sld_path = styles_dir / f"{style_stem}.sld"
    logger.debug("Output SLD path resolved: %s", sld_path)

    t_open = time.perf_counter()
    logger.info("Opening raster with rasterio...")
    with rasterio.open(raster_path) as ds:
        logger.debug(
            "Raster opened: width=%d height=%d dtype=%s nodata=%s",
            ds.width,
            ds.height,
            ds.dtypes[0],
            ds.nodata,
        )
        t_read = time.perf_counter()
        logger.info("Reading band 1 as masked array...")
        arr = ds.read(1, masked=True)
        logger.debug(
            "Band read complete in %.3fs; masked=%s",
            time.perf_counter() - t_read,
            bool(np.ma.isMaskedArray(arr)),
        )

        nodata = (
            ds.nodata
            if ds.nodata is not None
            else (
                ds.nodatavals[0]
                if ds.nodatavals and ds.nodatavals[0] is not None
                else None
            )
        )
        logger.debug("Resolved nodata value: %s", str(nodata))

        t_compress = time.perf_counter()
        valid = arr.compressed().astype("float64")
        logger.info(
            "Compressed valid data in %.3fs; valid_count=%d (of %d)",
            time.perf_counter() - t_compress,
            valid.size,
            arr.size,
        )

    logger.debug(
        "Raster open+read total time: %.3fs", time.perf_counter() - t_open
    )

    if valid.size == 0:
        logger.warning("No valid data found; using trivial range [0,1]")
        qmin, qmax = 0.0, 1.0
    else:
        t_pct = time.perf_counter()
        logger.info("Computing 5th/95th percentiles...")
        qmin, qmax = np.percentile(valid, [5, 95])
        logger.debug(
            "Percentiles computed in %.3fs: p5=%.6g p95=%.6g",
            time.perf_counter() - t_pct,
            qmin,
            qmax,
        )

        if not np.isfinite(qmin) or not np.isfinite(qmax) or qmin == qmax:
            logger.info(
                "Percentiles degenerate or non-finite; using min/max..."
            )
            t_mm = time.perf_counter()
            qmin = float(np.nanmin(valid))
            qmax = float(np.nanmax(valid))
            logger.debug(
                "Min/Max computed in %.3fs: min=%.6g max=%.6g",
                time.perf_counter() - t_mm,
                qmin,
                qmax,
            )

        if qmin == qmax:
            eps = 1.0 if qmin == 0 else abs(qmin) * 0.01
            logger.info("Flat data detected; expanding range by ±%.6g", eps)
            qmin -= eps
            qmax += eps

    def _nice(v: float) -> float:
        if v == 0:
            return 0.0
        mag = 10 ** math.floor(math.log10(abs(v)))
        return round(v / mag, 3) * mag

    qmin_n = _nice(qmin)
    qmax_n = _nice(qmax)
    if qmin_n >= qmax_n:
        logger.debug("Nice range collapsed; reverting to raw [qmin,qmax]")
        qmin_n, qmax_n = qmin, qmax
    logger.info("Final range for ramp: [%.6g, %.6g]", qmin_n, qmax_n)

    palette = [
        "#f7fcb9",
        "#d9f0a3",
        "#addd8e",
        "#78c679",
        "#41ab5d",
        "#238443",
        "#005a32",
    ]
    if n_colors < 2:
        logger.debug("Requested n_colors < 2; bumping to 2")
        n_colors = 2

    logger.info("Preparing color ramp entries: n_colors=%d", n_colors)
    colors_idx = np.linspace(0, len(palette) - 1, n_colors).round().astype(int)
    colors = [palette[i] for i in colors_idx]
    logger.debug("Palette indices: %s; colors: %s", colors_idx.tolist(), colors)

    quantities = np.linspace(qmin_n, qmax_n, n_colors)
    logger.debug("Quantities: %s", [float(x) for x in quantities])

    def _lbl(x: float) -> str:
        ax = abs(x)
        if (ax >= 1000) or (0 < ax < 0.01):
            return f"{x:.3g}"
        if ax >= 100:
            return f"{x:.0f}"
        if ax >= 10:
            return f"{x:.1f}"
        return f"{x:.2f}"

    logger.info("Building ColorMap entries...")
    entries = []

    # Collect all entries as (quantity, xml_string) tuples
    if nodata is not None and np.isfinite(nodata):
        entries.append(
            (
                float(nodata),
                f'              <ColorMapEntry color="#000000" quantity="{nodata}" label="NoData" opacity="0.0"/>',
            )
        )

    for c, q in zip(colors, quantities):
        entries.append(
            (
                float(q),
                f'              <ColorMapEntry color="{c}" quantity="{q}" label="{_lbl(q)}" opacity="1.0"/>',
            )
        )

    beyond = qmax_n + (qmax_n - qmin_n) * 0.01
    entries.append(
        (
            float(beyond),
            f'              <ColorMapEntry color="{colors[-1]}" quantity="{beyond}" opacity="0.0"/>',
        )
    )

    # Sort by numeric quantity ascending before writing XML
    entries.sort(key=lambda x: x[0])

    # Keep only the XML strings
    entries = [xml for _, xml in entries]
    logger.debug("ColorMap entries built: %d", len(entries))

    layer_name = raster_path.stem
    title = f"{layer_name} (auto)"
    abstract = f"Auto-generated ramp from {_lbl(qmin_n)} to {_lbl(qmax_n)}; nodata transparent."

    logger.info("Composing SLD XML...")
    sld = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<StyledLayerDescriptor version="1.0.0"\n'
        '  xmlns="http://www.opengis.net/sld"\n'
        '  xmlns:ogc="http://www.opengis.net/ogc"\n'
        '  xmlns:xlink="http://www.w3.org/1999/xlink"\n'
        '  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '  xsi:schemaLocation="http://www.opengis.net/sld\n'
        '                      http://schemas.opengis.net/sld/1.0.0/StyledLayerDescriptor.xsd">\n\n'
        f"  <NamedLayer>\n"
        f"    <Name>{layer_name}</Name>\n"
        f"    <UserStyle>\n"
        f"      <Title>{title}</Title>\n"
        f"      <Abstract>{abstract}</Abstract>\n"
        f"      <FeatureTypeStyle>\n"
        f"        <Rule>\n"
        f"          <RasterSymbolizer>\n"
        f"            <Opacity>1.0</Opacity>\n"
        f'            <ColorMap type="ramp">\n'
        + "\n".join(entries)  # noqa: W503
        + "\n"  # noqa: F541, W503
        f"            </ColorMap>\n"
        f"            <ContrastEnhancement>\n"
        f"              <Normalize/>\n"
        f"            </ContrastEnhancement>\n"
        f"          </RasterSymbolizer>\n"
        f"        </Rule>\n"
        f"      </FeatureTypeStyle>\n"
        f"    </UserStyle>\n"
        f"  </NamedLayer>\n"
        f"</StyledLayerDescriptor>\n"
    )

    logger.info("Writing SLD to disk: %s", sld_path)
    t_write = time.perf_counter()
    sld_path.write_text(sld, encoding="utf-8")
    logger.debug("SLD write time: %.3fs", time.perf_counter() - t_write)

    logger.info(
        "SLD generation finished in %.3fs: %s",
        time.perf_counter() - t0,
        sld_path,
    )
    return str(sld_path)


def main():
    """Command-line entry point for generating GeoServer layer manifests."""
    parser = argparse.ArgumentParser(
        prog="layers_compiler",
        description="Recursively scan a directory for GeoTIFFs and emit YAML layer entries.",
    )
    parser.add_argument("directory", type=Path, help="Root directory to scan")
    parser.add_argument(
        "-w", "--workspace", default="esosc", help="Workspace name to assign"
    )

    args = parser.parse_args()

    exts = {".tif", ".tiff"}

    root = args.directory.resolve()
    if not root.exists() or not root.is_dir():
        logger.error(f"error: directory not found: {root}", file=sys.stderr)
        sys.exit(2)

    layers = {}
    for raster_path in sorted(find_geotiffs(root, exts)):
        path_to_style = generate_dynamic_sld(raster_path, styles_root=root)
        key, entry = build_layer_entry(
            path=raster_path,
            raster_type="raster_geotiff",
            default_style=Path(path_to_style).as_posix(),
        )
        if key in layers:
            logger.warning(
                f'duplicate layer id "{key}" from {raster_path}, skipping',
                file=sys.stderr,
            )
            continue
        layers[key] = entry

    doc = {}
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
    sys.stdout.write(out_s)


if __name__ == "__main__":
    main()
