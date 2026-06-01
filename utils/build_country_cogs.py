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
from concurrent.futures import ThreadPoolExecutor, as_completed
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


@dataclass(frozen=True)
class NodataPolicy:
    """Describes source-to-output nodata handling for one manifest.

    Attributes:
        raster_name: Manifest stem the policy applies to.
        source_nodata: Value that should be treated as nodata before stitching.
        output_nodata: Value that should be written as nodata for stitching and
            COG publication.
    """

    raster_name: str
    source_nodata: str
    output_nodata: str


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
        "--gdal-warp",
        default="gdalwarp",
        help="gdalwarp executable to use for nodata normalization.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help=(
            "Directory for temporary manifests and normalized nodata rasters. "
            "Defaults to <manifest-dir>/_build_country_cogs."
        ),
    )
    parser.add_argument(
        "--nodata-policy",
        action="append",
        default=[],
        metavar="NAME:SOURCE:OUTPUT",
        help=(
            "Per-raster nodata normalization applied before stitching. NAME "
            "is the manifest stem without .txt, SOURCE is the value to treat "
            "as nodata in source rasters, and OUTPUT is the nodata value to "
            "write for stitching and COGs. Repeat for multiple rasters."
        ),
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
        "--cog-workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help=(
            "Number of rasters to convert to COGs in parallel. Keep "
            "--gdal-threads low when raising this to avoid oversubscribing "
            "CPU and memory. Defaults to min(4, CPU count)."
        ),
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


def parse_nodata_policies(policy_values: Sequence[str]) -> dict[str, NodataPolicy]:
    """Parse per-raster nodata policy CLI values.

    Args:
        policy_values: Values formatted as NAME:SOURCE:OUTPUT.

    Returns:
        Mapping of manifest stem to nodata policy.

    Raises:
        ValueError: If a policy value does not have exactly three parts.
    """

    policies = {}
    for policy_value in policy_values:
        parts = policy_value.split(":")
        if len(parts) != 3:
            raise ValueError(
                "Nodata policies must use NAME:SOURCE:OUTPUT format. "
                f"Got: {policy_value}"
            )
        raster_name, source_nodata, output_nodata = parts
        policies[raster_name] = NodataPolicy(
            raster_name=raster_name,
            source_nodata=source_nodata,
            output_nodata=output_nodata,
        )
    return policies


def read_manifest_sources(manifest_path: Path) -> list[Path]:
    """Read source raster paths from a stitch manifest.

    Args:
        manifest_path: Text file with one source raster path per line.

    Returns:
        Source raster paths listed in the manifest.
    """

    manifest_text = read_manifest_text(manifest_path)
    return [
        Path(line.strip())
        for line in manifest_text.splitlines()
        if line.strip()
    ]


def read_manifest_text(manifest_path: Path) -> str:
    """Read a stitch manifest using common text encodings.

    Args:
        manifest_path: Text file with one source raster path per line.

    Returns:
        Decoded manifest text.

    Raises:
        UnicodeDecodeError: If the manifest cannot be decoded with supported
            encodings.
    """

    manifest_bytes = manifest_path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return manifest_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return manifest_bytes.decode("utf-8")


def write_manifest(manifest_path: Path, source_paths: Sequence[Path]) -> None:
    """Write a stitch manifest.

    Args:
        manifest_path: Destination manifest path.
        source_paths: Source raster paths to write.
    """

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "\n".join(str(path) for path in source_paths) + "\n",
        encoding="utf-8",
    )


def normalized_source_path(
    source_path: Path, policy: NodataPolicy, output_dir: Path, index: int
) -> Path:
    """Build a deterministic path for a normalized source raster.

    Args:
        source_path: Original source raster path.
        policy: Nodata policy being applied.
        output_dir: Directory for normalized sources.
        index: Source index within the manifest.

    Returns:
        Path for the normalized source raster or VRT.
    """

    suffix = ".vrt" if policy.source_nodata == policy.output_nodata else ".tif"
    return output_dir / f"{index:05d}_{source_path.stem}{suffix}"


def normalize_source_nodata(
    source_path: Path,
    output_path: Path,
    policy: NodataPolicy,
    gdal_translate_exe: str,
    gdal_warp_exe: str,
) -> None:
    """Create a source raster view/copy with corrected nodata semantics.

    When source and output nodata are the same, a lightweight VRT with corrected
    nodata metadata is enough. When values differ, a temporary GeoTIFF is
    written so source nodata pixels become the desired output nodata value.

    Args:
        source_path: Original source raster path.
        output_path: Normalized source path to create.
        policy: Nodata policy to apply.
        gdal_translate_exe: gdal_translate executable.
        gdal_warp_exe: gdalwarp executable.

    Raises:
        subprocess.CalledProcessError: If GDAL normalization fails.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    if policy.source_nodata == policy.output_nodata:
        command = [
            gdal_translate_exe,
            str(source_path),
            str(output_path),
            "-of",
            "VRT",
            "-a_nodata",
            policy.output_nodata,
        ]
    else:
        command = [
            gdal_warp_exe,
            "-overwrite",
            "-srcnodata",
            policy.source_nodata,
            "-dstnodata",
            policy.output_nodata,
            "-of",
            "GTiff",
            "-co",
            "TILED=YES",
            "-co",
            "COMPRESS=DEFLATE",
            str(source_path),
            str(output_path),
        ]
    subprocess.run(command, check=True)


def prepare_manifest_for_nodata_policy(
    manifest_path: Path,
    policy: NodataPolicy,
    prepared_manifest_path: Path,
    normalized_source_dir: Path,
    gdal_translate_exe: str,
    gdal_warp_exe: str,
) -> Path:
    """Create a temporary manifest with normalized source rasters.

    Args:
        manifest_path: Original stitch manifest path.
        policy: Nodata policy to apply.
        prepared_manifest_path: Temporary manifest path to write.
        normalized_source_dir: Directory for normalized source rasters.
        gdal_translate_exe: gdal_translate executable.
        gdal_warp_exe: gdalwarp executable.

    Returns:
        Temporary manifest path.
    """

    source_paths = read_manifest_sources(manifest_path)
    normalized_paths = []
    total = len(source_paths)
    print(
        f"Preparing nodata policy for {manifest_path.stem}: "
        f"{policy.source_nodata} -> {policy.output_nodata} ({total} source rasters)"
    )
    for index, source_path in enumerate(source_paths, start=1):
        normalized_path = normalized_source_path(
            source_path=source_path,
            policy=policy,
            output_dir=normalized_source_dir,
            index=index,
        )
        print(f"  {index}/{total} {source_path.name}")
        normalize_source_nodata(
            source_path=source_path,
            output_path=normalized_path,
            policy=policy,
            gdal_translate_exe=gdal_translate_exe,
            gdal_warp_exe=gdal_warp_exe,
        )
        normalized_paths.append(normalized_path)
    write_manifest(prepared_manifest_path, normalized_paths)
    return prepared_manifest_path


def prepare_manifests_for_stitching(
    manifest_paths: Sequence[Path],
    policies: dict[str, NodataPolicy],
    work_dir: Path,
    gdal_translate_exe: str,
    gdal_warp_exe: str,
    dry_run: bool,
) -> tuple[list[Path], Path | None]:
    """Prepare temporary manifests for nodata-aware stitching.

    Args:
        manifest_paths: Original stitch manifests.
        policies: Per-manifest nodata policies.
        work_dir: Temporary working directory.
        gdal_translate_exe: gdal_translate executable.
        gdal_warp_exe: gdalwarp executable.
        dry_run: Whether to describe work without creating files.

    Returns:
        Prepared manifest paths and the directory containing them. If no
        policies are defined, the original manifests and None are returned.
    """

    if not policies:
        return list(manifest_paths), None

    prepared_manifest_dir = work_dir / "manifests"
    normalized_source_root = work_dir / "nodata_sources"
    prepared_paths = []
    for manifest_path in manifest_paths:
        prepared_manifest_path = prepared_manifest_dir / manifest_path.name
        policy = policies.get(manifest_path.stem)
        if dry_run:
            if policy is None:
                print(f"Would copy manifest unchanged: {manifest_path.name}")
            else:
                print(
                    f"Would normalize {manifest_path.name}: "
                    f"{policy.source_nodata} -> {policy.output_nodata}"
                )
            prepared_paths.append(prepared_manifest_path)
            continue

        if policy is None:
            write_manifest(prepared_manifest_path, read_manifest_sources(manifest_path))
        else:
            prepare_manifest_for_nodata_policy(
                manifest_path=manifest_path,
                policy=policy,
                prepared_manifest_path=prepared_manifest_path,
                normalized_source_dir=normalized_source_root / manifest_path.stem,
                gdal_translate_exe=gdal_translate_exe,
                gdal_warp_exe=gdal_warp_exe,
            )
        prepared_paths.append(prepared_manifest_path)
    return prepared_paths, prepared_manifest_dir


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
    output_dir: Path,
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
        output_dir: Directory where stitched rasters should be written.
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
    command.extend(["--output-dir", str(output_dir)])
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


def progress_completed(
    futures: dict, total: int, label: str
) -> Iterator[tuple[RasterJob, object]]:
    """Yield completed futures with tqdm-style progress output.

    Args:
        futures: Mapping of future objects to raster jobs.
        total: Total number of futures.
        label: Progress label.

    Yields:
        Raster jobs and future results in completion order.
    """

    try:
        from tqdm import tqdm
    except ImportError:
        for index, future in enumerate(as_completed(futures), start=1):
            job = futures[future]
            result = future.result()
            print(f"{label}: {index}/{total} {job.stitched_path.name}")
            yield job, result
    else:
        for future in tqdm(as_completed(futures), total=total, desc=label, unit="raster"):
            job = futures[future]
            yield job, future.result()


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
    cog_workers: int,
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
        cog_workers: Number of COG conversions to run in parallel.
        dry_run: Whether to print without executing.
    """

    if cog_workers <= 1 or dry_run:
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
        return

    with ThreadPoolExecutor(max_workers=cog_workers) as executor:
        futures = {
            executor.submit(
                convert_to_cog,
                gdal_translate_exe=gdal_translate_exe,
                job=job,
                nodata=nodata,
                resampling_option=resampling_option,
                compression=compression,
                blocksize=blocksize,
                gdal_threads=gdal_threads,
                dry_run=dry_run,
            ): job
            for job in jobs
        }
        for _job, _result in progress_completed(
            futures, total=len(futures), label="Converting COGs"
        ):
            pass


def print_parallelism_note(cog_workers: int, gdal_threads: str) -> None:
    """Print the configured COG conversion parallelism.

    Args:
        cog_workers: Number of COG conversion workers.
        gdal_threads: NUM_THREADS value passed to each GDAL process.
    """

    print(
        "COG conversion parallelism: "
        f"{cog_workers} raster(s) at a time, GDAL NUM_THREADS={gdal_threads}"
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
    publish_dir = args.publish_dir.resolve()
    work_dir = (args.work_dir or (args.manifest_dir / "_build_country_cogs")).resolve()
    policies = parse_nodata_policies(args.nodata_policy)

    manifest_paths = discover_manifests(manifest_dir, args.manifest_glob)
    prepared_manifest_paths = manifest_paths
    prepared_manifest_dir = None
    if policies and not args.skip_stitch:
        if not args.dry_run:
            check_executable(args.gdal_translate)
            check_executable(args.gdal_warp)
        prepared_manifest_paths, prepared_manifest_dir = prepare_manifests_for_stitching(
            manifest_paths=manifest_paths,
            policies=policies,
            work_dir=work_dir,
            gdal_translate_exe=args.gdal_translate,
            gdal_warp_exe=args.gdal_warp,
            dry_run=args.dry_run,
        )

    stitched_dir = (
        args.stitched_dir.resolve()
        if args.stitched_dir
        else manifest_dir.resolve()
    )
    stitch_manifest_dir = prepared_manifest_dir or manifest_dir
    jobs = make_jobs(
        manifest_paths=prepared_manifest_paths,
        stitched_dir=stitched_dir,
        publish_dir=publish_dir,
        output_suffix=args.output_suffix,
    )

    print(f"Found {len(manifest_paths)} manifest(s) in {manifest_dir}")
    print(f"Expected stitched rasters in {stitched_dir}")
    print(f"Publishing COGs to {publish_dir}")
    print_parallelism_note(args.cog_workers, args.gdal_threads)

    if not args.skip_stitch:
        run_stitcher(
            stitch_script=args.stitch_script.resolve(),
            manifest_dir=stitch_manifest_dir,
            manifest_glob=args.manifest_glob,
            manifest_paths=prepared_manifest_paths,
            output_dir=stitched_dir,
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
        cog_workers=args.cog_workers,
        dry_run=args.dry_run,
    )
    print_summary(inspected_jobs)


if __name__ == "__main__":
    main()
