"""Build stitched country rasters and publish COGs for GeoServer.

This utility wraps the local raster preparation workflow:

1. Discover raster stitch manifest text files.
2. Run the existing parallel stitcher once.
3. Inspect each stitched raster dtype.
4. Convert each stitched raster to a Cloud Optimized GeoTIFF using average
   overview resampling for floating-point rasters and mode resampling for
   integer/categorical rasters.
5. Write the finished COGs directly to a GeoServer-ready publish directory.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Iterator, Sequence


FLOAT_GDAL_TYPES = {"Float16", "Float32", "Float64", "CFloat32", "CFloat64"}


@dataclass(frozen=True)
class RasterJob:
    """Describes a stitched raster and the COG that should be created.

    Attributes:
        manifest_path: Path to the text manifest used by the stitcher.
        stitched_path: Expected stitched raster path.
        output_path: Final COG path in the publish directory.
        dtype: GDAL raster data type, populated after inspection.
        resampling: GDAL overview resampling method, populated after inspection.
    """

    manifest_path: Path
    stitched_path: Path
    output_path: Path
    dtype: str | None = None
    resampling: str | None = None


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed arguments for the pipeline.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Stitch country raster manifests, convert stitched rasters to COGs, "
            "and publish them to a GeoServer-ready directory."
        )
    )
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        required=True,
        help="Directory containing the stitcher .txt manifest files.",
    )
    parser.add_argument(
        "--publish-dir",
        type=Path,
        required=True,
        help="Directory where final COGs should be written for GeoServer.",
    )
    parser.add_argument(
        "--stitched-dir",
        type=Path,
        default=None,
        help=(
            "Directory where stitched rasters are expected. Defaults to "
            "--manifest-dir."
        ),
    )
    parser.add_argument(
        "--manifest-glob",
        default="*.txt",
        help="Glob used to discover manifest files inside --manifest-dir.",
    )
    parser.add_argument(
        "--stitch-script",
        type=Path,
        default=Path(__file__).with_name("run_parallel_stitch_rasters.py"),
        help=(
            "Path to run_parallel_stitch_rasters.py. Defaults to the same "
            "directory as this script."
        ),
    )
    parser.add_argument(
        "--stitch-input-mode",
        choices=("glob", "files"),
        default="glob",
        help=(
            "Pass one glob pattern to the stitcher, matching the existing ad "
            "hoc command, or pass every discovered manifest as a separate "
            "argument."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=12,
        help="Worker count passed to the stitcher.",
    )
    parser.add_argument(
        "--output-suffix",
        default="_stitched",
        help="Suffix used by the stitcher for output rasters.",
    )
    parser.add_argument(
        "--nodata",
        default="0",
        help=(
            "Nodata value passed to the stitcher and assigned to output COGs. "
            "Use an empty string to skip -a_nodata during COG creation."
        ),
    )
    parser.add_argument(
        "--gdal-translate",
        default="gdal_translate",
        help="gdal_translate executable to use.",
    )
    parser.add_argument(
        "--gdalinfo",
        default="gdalinfo",
        help="gdalinfo executable to use for dtype inspection.",
    )
    parser.add_argument(
        "--resampling-option",
        default="RESAMPLING",
        help=(
            "COG creation option name for overview resampling. GDAL's COG "
            "driver commonly uses RESAMPLING; use OVERVIEW_RESAMPLING if your "
            "installed GDAL build expects that option."
        ),
    )
    parser.add_argument(
        "--compression",
        default="DEFLATE",
        help="COG compression creation option.",
    )
    parser.add_argument(
        "--blocksize",
        default="512",
        help="COG block size creation option.",
    )
    parser.add_argument(
        "--gdal-threads",
        default="1",
        help="NUM_THREADS creation option passed to gdal_translate.",
    )
    parser.add_argument(
        "--skip-stitch",
        action="store_true",
        help="Skip the stitch step and only COG existing stitched rasters.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned work without running stitch or COG commands.",
    )
    return parser.parse_args()


def discover_manifests(manifest_dir: Path, manifest_glob: str) -> list[Path]:
    """Find stitch manifest files.

    Args:
        manifest_dir: Directory containing manifest text files.
        manifest_glob: Glob pattern used to select manifest files.

    Returns:
        Sorted list of manifest paths.

    Raises:
        FileNotFoundError: If the manifest directory does not exist.
        RuntimeError: If no manifest files match the glob.
    """

    if not manifest_dir.is_dir():
        raise FileNotFoundError(f"Manifest directory does not exist: {manifest_dir}")

    manifest_paths = sorted(manifest_dir.glob(manifest_glob))
    if not manifest_paths:
        raise RuntimeError(f"No manifests matched {manifest_glob!r} in {manifest_dir}")
    return manifest_paths


def make_jobs(
    manifest_paths: Sequence[Path],
    stitched_dir: Path,
    publish_dir: Path,
    output_suffix: str,
) -> list[RasterJob]:
    """Create planned raster conversion jobs from manifest paths.

    Args:
        manifest_paths: Stitch manifest paths.
        stitched_dir: Directory where stitched rasters are expected.
        publish_dir: Directory for final COG outputs.
        output_suffix: Suffix used for stitched raster names.

    Returns:
        Planned raster jobs.
    """

    jobs = []
    for manifest_path in manifest_paths:
        stitched_name = f"{manifest_path.stem}{output_suffix}.tif"
        jobs.append(
            RasterJob(
                manifest_path=manifest_path,
                stitched_path=stitched_dir / stitched_name,
                output_path=publish_dir / stitched_name,
            )
        )
    return jobs


def format_command(command: Sequence[str]) -> str:
    """Format a command for readable logs.

    Args:
        command: Command arguments.

    Returns:
        A command string suitable for console output.
    """

    if os.name == "nt":
        return subprocess.list2cmdline([str(part) for part in command])
    return " ".join(shlex_quote(str(part)) for part in command)


def shlex_quote(value: str) -> str:
    """Quote a shell argument for POSIX-style display.

    Args:
        value: Raw shell argument.

    Returns:
        Quoted argument.
    """

    import shlex

    return shlex.quote(value)


def run_stitcher(
    stitch_script: Path,
    manifest_dir: Path,
    manifest_glob: str,
    manifest_paths: Sequence[Path],
    stitch_input_mode: str,
    workers: int,
    output_suffix: str,
    nodata: str,
    dry_run: bool,
) -> None:
    """Run the existing parallel stitcher script.

    Args:
        stitch_script: Path to the stitcher script.
        manifest_dir: Directory containing manifests.
        manifest_glob: Glob used when stitch_input_mode is "glob".
        manifest_paths: Discovered manifest paths.
        stitch_input_mode: Whether to pass one glob or individual files.
        workers: Worker count passed to the stitcher.
        output_suffix: Output suffix passed to the stitcher.
        nodata: Nodata value passed to the stitcher.
        dry_run: Whether to print without executing.

    Raises:
        FileNotFoundError: If the stitcher script is missing.
        subprocess.CalledProcessError: If the stitcher fails.
    """

    if not dry_run and not stitch_script.is_file():
        raise FileNotFoundError(
            f"Stitch script does not exist: {stitch_script}. "
            "Pass --stitch-script if it lives elsewhere."
        )

    command = [sys.executable, str(stitch_script)]
    if stitch_input_mode == "glob":
        command.append(str(manifest_dir / manifest_glob))
    else:
        command.extend(str(path) for path in manifest_paths)
    command.extend(["--workers", str(workers), "--output-suffix", output_suffix])
    if nodata != "":
        command.extend(["--nodata", nodata])

    print(f"Stitch command: {format_command(command)}")
    if dry_run:
        return
    subprocess.run(command, check=True)


def inspect_gdal_dtype(gdalinfo_exe: str, raster_path: Path) -> str:
    """Inspect a raster's first-band dtype with gdalinfo.

    Args:
        gdalinfo_exe: gdalinfo executable.
        raster_path: Raster path to inspect.

    Returns:
        GDAL type string for the first band.

    Raises:
        RuntimeError: If gdalinfo output does not include band type metadata.
        subprocess.CalledProcessError: If gdalinfo fails.
    """

    result = subprocess.run(
        [gdalinfo_exe, "-json", str(raster_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = json.loads(result.stdout)
    bands = metadata.get("bands", [])
    if not bands or "type" not in bands[0]:
        raise RuntimeError(f"Could not determine raster dtype from {raster_path}")
    return str(bands[0]["type"])


def resampling_for_dtype(dtype: str) -> str:
    """Choose overview resampling for a GDAL dtype.

    Args:
        dtype: GDAL raster type string.

    Returns:
        "AVERAGE" for floating-point rasters and "MODE" otherwise.
    """

    if dtype in FLOAT_GDAL_TYPES:
        return "AVERAGE"
    return "MODE"


def progress_iter(items: Sequence[RasterJob], label: str) -> Iterator[RasterJob]:
    """Yield jobs with tqdm-style progress output.

    Args:
        items: Jobs to iterate over.
        label: Progress label.

    Yields:
        Raster jobs.
    """

    try:
        from tqdm import tqdm
    except ImportError:
        total = len(items)
        for index, item in enumerate(items, start=1):
            print(f"{label}: {index}/{total} {item.stitched_path.name}")
            yield item
    else:
        yield from tqdm(items, desc=label, unit="raster")


def inspect_jobs(gdalinfo_exe: str, jobs: Sequence[RasterJob]) -> list[RasterJob]:
    """Populate dtype and resampling for planned jobs.

    Args:
        gdalinfo_exe: gdalinfo executable.
        jobs: Planned raster jobs.

    Returns:
        Jobs with dtype and resampling populated.
    """

    inspected_jobs = []
    for job in progress_iter(list(jobs), "Inspecting rasters"):
        dtype = inspect_gdal_dtype(gdalinfo_exe, job.stitched_path)
        inspected_jobs.append(
            RasterJob(
                manifest_path=job.manifest_path,
                stitched_path=job.stitched_path,
                output_path=job.output_path,
                dtype=dtype,
                resampling=resampling_for_dtype(dtype),
            )
        )
    return inspected_jobs


def ensure_stitched_outputs(jobs: Sequence[RasterJob]) -> None:
    """Check that every expected stitched raster exists.

    Args:
        jobs: Planned raster jobs.

    Raises:
        FileNotFoundError: If any stitched raster is missing.
    """

    missing_paths = [job.stitched_path for job in jobs if not job.stitched_path.is_file()]
    if missing_paths:
        missing_lines = "\n".join(f"  - {path}" for path in missing_paths)
        raise FileNotFoundError(
            "Expected stitched raster outputs were not found:\n" f"{missing_lines}"
        )


def convert_to_cog(
    gdal_translate_exe: str,
    job: RasterJob,
    nodata: str,
    resampling_option: str,
    compression: str,
    blocksize: str,
    gdal_threads: str,
    dry_run: bool,
) -> None:
    """Convert one stitched raster to a COG in the publish directory.

    Args:
        gdal_translate_exe: gdal_translate executable.
        job: Raster conversion job with dtype/resampling populated.
        nodata: Nodata value assigned with -a_nodata, or empty string to skip.
        resampling_option: COG creation option name for overview resampling.
        compression: Compression creation option value.
        blocksize: Block size creation option value.
        gdal_threads: NUM_THREADS creation option value.
        dry_run: Whether to print without executing.

    Raises:
        subprocess.CalledProcessError: If gdal_translate fails.
    """

    temp_output_path = job.output_path.with_name(
        f"{job.output_path.stem}.tmp{job.output_path.suffix}"
    )
    command = [
        gdal_translate_exe,
        str(job.stitched_path),
        str(temp_output_path),
        "-of",
        "COG",
        "-co",
        f"COMPRESS={compression}",
        "-co",
        "PREDICTOR=YES",
        "-co",
        f"BLOCKSIZE={blocksize}",
        "-co",
        f"{resampling_option}={job.resampling}",
        "-co",
        f"NUM_THREADS={gdal_threads}",
    ]
    if nodata != "":
        command.extend(["-a_nodata", nodata])

    if dry_run:
        print(f"COG command: {format_command(command)}")
        return

    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    if temp_output_path.exists():
        temp_output_path.unlink()
    subprocess.run(command, check=True)
    os.replace(temp_output_path, job.output_path)


def convert_jobs_to_cogs(
    gdal_translate_exe: str,
    jobs: Sequence[RasterJob],
    nodata: str,
    resampling_option: str,
    compression: str,
    blocksize: str,
    gdal_threads: str,
    dry_run: bool,
) -> None:
    """Convert all planned jobs to COGs.

    Args:
        gdal_translate_exe: gdal_translate executable.
        jobs: Raster jobs to convert.
        nodata: Nodata value assigned with -a_nodata, or empty string to skip.
        resampling_option: COG creation option name for overview resampling.
        compression: Compression creation option value.
        blocksize: Block size creation option value.
        gdal_threads: NUM_THREADS creation option value.
        dry_run: Whether to print without executing.
    """

    for job in progress_iter(list(jobs), "Converting COGs"):
        convert_to_cog(
            gdal_translate_exe=gdal_translate_exe,
            job=job,
            nodata=nodata,
            resampling_option=resampling_option,
            compression=compression,
            blocksize=blocksize,
            gdal_threads=gdal_threads,
            dry_run=dry_run,
        )


def check_executable(name: str) -> None:
    """Check that an executable can be found on PATH.

    Args:
        name: Executable name or path.

    Raises:
        FileNotFoundError: If the executable is not available.
    """

    if Path(name).is_file():
        return
    if shutil.which(name) is None:
        raise FileNotFoundError(f"Required executable not found: {name}")


def print_summary(jobs: Sequence[RasterJob]) -> None:
    """Print a final summary table.

    Args:
        jobs: Completed or planned raster jobs.
    """

    rows = [
        (
            job.stitched_path.name,
            job.dtype or "unknown",
            job.resampling or "unknown",
            str(job.output_path),
        )
        for job in jobs
    ]
    widths = [
        max(len("raster"), *(len(row[0]) for row in rows)),
        max(len("dtype"), *(len(row[1]) for row in rows)),
        max(len("resampling"), *(len(row[2]) for row in rows)),
    ]
    print("\nSummary")
    print(
        f"{'raster':<{widths[0]}}  "
        f"{'dtype':<{widths[1]}}  "
        f"{'resampling':<{widths[2]}}  output"
    )
    print(
        f"{'-' * widths[0]}  "
        f"{'-' * widths[1]}  "
        f"{'-' * widths[2]}  {'-' * 6}"
    )
    for raster_name, dtype, resampling, output_path in rows:
        print(
            f"{raster_name:<{widths[0]}}  "
            f"{dtype:<{widths[1]}}  "
            f"{resampling:<{widths[2]}}  {output_path}"
        )


def main() -> None:
    """Run the stitch-to-COG pipeline."""

    args = parse_args()
    manifest_dir = args.manifest_dir.resolve()
    stitched_dir = (args.stitched_dir or args.manifest_dir).resolve()
    publish_dir = args.publish_dir.resolve()

    manifest_paths = discover_manifests(manifest_dir, args.manifest_glob)
    jobs = make_jobs(
        manifest_paths=manifest_paths,
        stitched_dir=stitched_dir,
        publish_dir=publish_dir,
        output_suffix=args.output_suffix,
    )

    print(f"Found {len(manifest_paths)} manifest(s) in {manifest_dir}")
    print(f"Expected stitched rasters in {stitched_dir}")
    print(f"Publishing COGs to {publish_dir}")

    if not args.skip_stitch:
        run_stitcher(
            stitch_script=args.stitch_script.resolve(),
            manifest_dir=manifest_dir,
            manifest_glob=args.manifest_glob,
            manifest_paths=manifest_paths,
            stitch_input_mode=args.stitch_input_mode,
            workers=args.workers,
            output_suffix=args.output_suffix,
            nodata=args.nodata,
            dry_run=args.dry_run,
        )

    if args.dry_run:
        print_summary(jobs)
        return

    check_executable(args.gdalinfo)
    check_executable(args.gdal_translate)
    ensure_stitched_outputs(jobs)
    inspected_jobs = inspect_jobs(args.gdalinfo, jobs)
    convert_jobs_to_cogs(
        gdal_translate_exe=args.gdal_translate,
        jobs=inspected_jobs,
        nodata=args.nodata,
        resampling_option=args.resampling_option,
        compression=args.compression,
        blocksize=args.blocksize,
        gdal_threads=args.gdal_threads,
        dry_run=args.dry_run,
    )
    print_summary(inspected_jobs)


if __name__ == "__main__":
    main()
