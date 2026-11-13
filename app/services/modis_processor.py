from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import earthaccess
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import reproject

from app.services.earthdata_auth import ensure_earthaccess_auth
from app.utils.logging import log

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DATA_DIR = PROJECT_ROOT / "data" / "processed"
EGYPT_BBOX = {"west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7}
REQUIRED_TILE_IDS = {"h20v05", "h20v06", "h21v05", "h21v06"}


def granule_tile_id(granule: Any) -> str:
    text = " ".join(
        str(piece)
        for piece in [
            granule.get("umm", {}).get("GranuleUR", ""),
            granule.get("umm", {}).get("DataGranule", {}).get("ProducerGranuleId", ""),
            str(granule),
        ]
        if piece
    )
    match = re.search(r"h\d+v\d+", text.lower())
    return match.group(0) if match else ""


def granule_temporal_start(granule: Any) -> str:
    temporal = granule.get("umm", {}).get("TemporalExtent", {})
    range_dt = temporal.get("RangeDateTime", {})
    if "BeginningDateTime" in range_dt:
        return str(range_dt["BeginningDateTime"])[:10]
    return ""


def discover_mod11a1_granules(date_str: str = "2024-01-01", bbox: dict[str, float] | None = None) -> list[Any]:
    bbox = bbox or EGYPT_BBOX
    search_results = earthaccess.search_data(
        short_name="MOD11A1",
        version="061",
        count=-1,
        temporal=(date_str, date_str),
        bounding_box=(bbox["west"], bbox["south"], bbox["east"], bbox["north"]),
    )

    filtered: list[Any] = []
    seen: set[str] = set()
    for granule in search_results:
        tile_id = granule_tile_id(granule)
        start_date = granule_temporal_start(granule)
        if start_date == date_str and tile_id and tile_id.lower() not in seen:
            seen.add(tile_id.lower())
            filtered.append(granule)
    result = sorted(filtered, key=lambda granule: granule_tile_id(granule))
    log(f"modis: {date_str}: discovered {len(search_results)} result(s), kept {len(result)} unique tile(s) for the AOI")
    return result


def download_mod11a1_granules(granules: list[Any], target_dir: str | Path | None = None) -> list[Path]:
    target_dir = Path(target_dir) if target_dir is not None else RAW_DATA_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    paths = earthaccess.download(granules, local_path=str(target_dir), threads=4, show_progress=False)
    return [Path(path) for path in paths]


def granule_filename(granule: Any) -> str | None:
    """Best-effort local filename for a granule (prefers the ``.hdf`` link)."""
    try:
        links = granule.data_links()
    except Exception:
        links = []
    names = [Path(str(link).split("?")[0]).name for link in links if link]
    for name in names:
        if name.lower().endswith(".hdf"):
            return name
    return names[0] if names else None


def _heartbeat(
    target: Path,
    initial_bytes: int,
    started_at: float,
    stop: threading.Event,
    label: str,
    interval: float,
    on_progress: Callable[[float, int], None] | None = None,
) -> None:
    """Log download progress every ``interval`` seconds until ``stop`` is set.

    ``on_progress(elapsed_seconds, bytes_written)`` lets the caller mirror the
    same numbers into job status, so a slow download stays visible in the API
    without reading worker logs.
    """
    while not stop.wait(interval):
        written = max(_directory_bytes(target) - initial_bytes, 0)
        elapsed = time.perf_counter() - started_at
        log(
            f"modis: still downloading{label}… "
            f"{elapsed:.0f}s elapsed, {written / 1024 / 1024:.1f} MB"
        )
        if on_progress is not None:
            try:
                on_progress(elapsed, written)
            except Exception as exc:  # status writes must never kill a download
                log(f"modis: progress callback failed: {type(exc).__name__}: {exc}")


def _directory_bytes(directory: Path) -> int:
    total = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for path in entries:
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def download_granules(
    granules: list[Any],
    target_dir: str | Path,
    *,
    threads: int = 8,
    label: str = "",
    heartbeat_seconds: float = 15.0,
    on_progress: Callable[[float, int], None] | None = None,
) -> tuple[list[Path], int, int]:
    """Download granules, reusing files already on disk.

    Returns ``(paths, already_present, downloaded_bytes)``. Existing files are
    detected before the call because ``earthaccess`` silently skips them and
    reports nothing about it. A batch failure falls back to one file at a time
    so a single dead link cannot lose the whole wave.
    """
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    suffix = f" {label}" if label else ""

    wanted = [(granule, granule_filename(granule)) for granule in granules]
    wanted = [(granule, name) for granule, name in wanted if name]
    cached = [(granule, name) for granule, name in wanted if (target / name).is_file()]
    missing = [granule for granule, name in wanted if not (target / name).is_file()]

    if cached:
        log(f"modis: already cached {len(cached)} granule(s){suffix}")

    downloaded_bytes = 0
    if missing:
        log(f"modis: downloading {len(missing)} granule(s){suffix}")
        initial_bytes = _directory_bytes(target)
        started_at = time.perf_counter()
        stop = threading.Event()
        if on_progress is not None:
            try:
                on_progress(0.0, 0)
            except Exception:
                pass
        monitor = threading.Thread(
            target=_heartbeat,
            args=(target, initial_bytes, started_at, stop, suffix, heartbeat_seconds, on_progress),
            daemon=True,
            name="modis-download-heartbeat",
        )
        monitor.start()
        try:
            earthaccess.download(missing, local_path=str(target), threads=threads, show_progress=False)
        except Exception as exc:
            log(
                f"modis: batch download failed ({type(exc).__name__}: {exc}); "
                "retrying one granule at a time"
            )
            for granule in missing:
                try:
                    earthaccess.download([granule], local_path=str(target), threads=1, show_progress=False)
                except Exception as inner:
                    log(f"modis: {granule_filename(granule)} failed: {type(inner).__name__}: {inner}")
        finally:
            stop.set()
            monitor.join(timeout=1.0)
        elapsed = time.perf_counter() - started_at
        downloaded_bytes = max(_directory_bytes(target) - initial_bytes, 0)
        still_missing = [name for _, name in wanted if not (target / name).is_file()]
        succeeded = len(missing) - len(still_missing)
        log(
            f"modis: downloaded {succeeded} granule(s) in {elapsed:.1f}s "
            f"({downloaded_bytes / 1024 / 1024:.1f} MB){suffix}"
        )
        if on_progress is not None:
            try:
                on_progress(elapsed, downloaded_bytes)
            except Exception:
                pass
        if still_missing:
            log(f"modis: {len(still_missing)} granule(s) still missing after retries{suffix}")
        if succeeded == 0:
            # Without this, an unauthenticated or fully failed download looks
            # like "no valid observations" much later in the pipeline.
            raise RuntimeError(
                f"Could not download any of {len(missing)} MODIS granule(s){suffix}. "
                "Check Earthdata credentials and network access."
            )

    paths = [target / name for _, name in wanted]
    return paths, len(cached), downloaded_bytes



GDAL_TRANSLATE_ENV_VAR = "GDAL_TRANSLATE"
GDAL_WARP_ENV_VAR = "GDAL_WARP"


def find_gdal_translate() -> str:
    """Return the gdal_translate executable to use for HDF4 extraction.

    Checks the ``GDAL_TRANSLATE`` environment variable first, then ``PATH``.
    Raises ``RuntimeError`` with an actionable message when GDAL is unavailable,
    so callers report a configuration problem instead of a generic QA failure.
    """
    candidate = os.environ.get(GDAL_TRANSLATE_ENV_VAR) or shutil.which("gdal_translate")
    if not candidate:
        raise RuntimeError(
            "gdal_translate was not found. MOD11A1 HDF4 subdatasets need GDAL built with "
            "the HDF4 driver (for example the OSGeo4W or conda-forge 'gdal' package). "
            f"Install it, add it to PATH, or set {GDAL_TRANSLATE_ENV_VAR} to the full executable path."
        )
    log(f"modis: gdal_translate candidate: {candidate}")
    return candidate


def extract_subdataset_to_tiff(
    hdf_path: str | Path,
    subdataset_name: str,
    output_tif: str | Path,
    grid_name: str = "MODIS_Grid_Daily_1km_LST",
) -> Path:
    gdal_translate = find_gdal_translate()
    hdf_path = str(hdf_path)
    output_tif = Path(output_tif)
    output_tif.parent.mkdir(parents=True, exist_ok=True)
    gdal_uri = f'HDF4_EOS:EOS_GRID:"{hdf_path}":{grid_name}:{subdataset_name}'
    subprocess.run(
        [
            gdal_translate,
            "-of",
            "GTiff",
            "-co",
            "COMPRESS=LZW",
            "-a_nodata",
            "0",
            gdal_uri,
            str(output_tif),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    log(f"modis: wrote {output_tif.name}")
    return output_tif


def find_gdalwarp() -> str:
    """Return the ``gdalwarp`` executable, preferring ``GDAL_WARP``.

    The Docker image installs GDAL outside ``PATH`` (only ``GDAL_TRANSLATE`` is
    pointed at it), so the sibling of ``gdal_translate`` is a reliable fallback.
    """
    candidate = os.environ.get(GDAL_WARP_ENV_VAR)
    if candidate and Path(candidate).is_file():
        return candidate
    found = shutil.which("gdalwarp")
    if found:
        return found
    translate = os.environ.get(GDAL_TRANSLATE_ENV_VAR) or shutil.which("gdal_translate")
    if translate:
        sibling = Path(translate).with_name("gdalwarp" + Path(translate).suffix)
        if sibling.is_file():
            return str(sibling)
    raise RuntimeError(
        "gdalwarp was not found. AOI subsetting needs GDAL built with the HDF4 driver. "
        f"Install it, add it to PATH, or set {GDAL_WARP_ENV_VAR} to the full executable path."
    )


def warp_subdataset_to_aoi(
    hdf_path: str | Path,
    subdataset_name: str,
    output_tif: str | Path,
    *,
    grid_name: str,
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
    resampling: str = "bilinear",
    src_nodata: float | None = None,
    dst_nodata: float | None = None,
    output_type: str | None = None,
) -> Path:
    """Warp one HDF4 subdataset straight onto the AOI's WGS84 grid.

    This replaces the old ``gdal_translate`` full-tile dump followed by a
    ``rasterio`` read and ``reproject``. GDAL only reads the source window that
    covers ``bounds``, so a national AOI costs a fraction of the full tile, and
    the numpy side gets an array that is already on the destination grid.
    """
    gdalwarp = find_gdalwarp()
    hdf_path = str(hdf_path)
    output_tif = Path(output_tif)
    output_tif.parent.mkdir(parents=True, exist_ok=True)
    gdal_uri = f'HDF4_EOS:EOS_GRID:"{hdf_path}":{grid_name}:{subdataset_name}'
    west, south, east, north = bounds

    command = [
        gdalwarp,
        "-q",
        "-overwrite",
        "-t_srs",
        "EPSG:4326",
        "-te",
        str(west),
        str(south),
        str(east),
        str(north),
        # -ts (not -tr) guarantees the array matches destination_grid_size().
        "-ts",
        str(int(width)),
        str(int(height)),
        "-r",
        resampling,
        "-of",
        "GTiff",
        "-co",
        "COMPRESS=LZW",
        "-co",
        "TILED=YES",
        "-co",
        "PREDICTOR=2",
        "-co",
        "BIGTIFF=IF_SAFER",
    ]
    if src_nodata is not None:
        command += ["-srcnodata", str(src_nodata)]
    if dst_nodata is not None:
        command += ["-dstnodata", str(dst_nodata)]
    if output_type:
        command += ["-ot", output_type]
    command += [gdal_uri, str(output_tif)]

    subprocess.run(command, check=True, capture_output=True, text=True)
    return output_tif



def read_raster(path: str | Path) -> tuple[np.ndarray, dict[str, Any]]:
    with rasterio.open(path) as src:
        return src.read(1).astype(np.float32), src.profile.copy()


def valid_lst_mask(raw_values: np.ndarray) -> np.ndarray:
    return (raw_values >= 7500) & (raw_values <= 65535)


def mandatory_qa_mask(qc_values: np.ndarray) -> np.ndarray:
    qc_int = np.asarray(qc_values, dtype=np.uint8)
    return (qc_int & 0b11) == 0


def convert_kelvin_to_celsius(raw_values: np.ndarray) -> np.ndarray:
    return (raw_values.astype(np.float32) * 0.02) - 273.15


def bounds_for_egypt() -> tuple[float, float, float, float]:
    return (EGYPT_BBOX["west"], EGYPT_BBOX["south"], EGYPT_BBOX["east"], EGYPT_BBOX["north"])


def destination_grid_size(bounds: tuple[float, float, float, float], resolution_deg: float = 0.01) -> tuple[int, int]:
    west, south, east, north = bounds
    width = max(int(round((east - west) / resolution_deg)), 1)
    height = max(int(round((north - south) / resolution_deg)), 1)
    return width, height


def reproject_to_4326(source: np.ndarray, src_profile: dict[str, Any], bounds: tuple[float, float, float, float], width: int, height: int) -> np.ndarray:
    dst_transform = from_bounds(*bounds, width=width, height=height)
    destination = np.full((height, width), np.nan, dtype=np.float32)
    reproject(
        source=source,
        destination=destination,
        src_transform=src_profile["transform"],
        src_crs=src_profile["crs"],
        dst_transform=dst_transform,
        dst_crs="EPSG:4326",
        src_nodata=np.nan,
        dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )
    return destination


def generate_heatmap(array: np.ndarray, bounds: tuple[float, float, float, float], title: str, output_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 8))
    masked = np.ma.masked_invalid(array)
    image = ax.imshow(
        masked,
        cmap="inferno",
        origin="upper",
        extent=[bounds[0], bounds[2], bounds[1], bounds[3]],
    )
    fig.colorbar(image, ax=ax, label="Temperature (°C)")
    ax.set_title(title)
    ax.set_xlabel("Longitude (°)")
    ax.set_ylabel("Latitude (°)")
    ax.set_aspect("auto")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def run_modis_egypt_pipeline(date_str: str = "2024-01-01") -> dict[str, Any]:
    ensure_earthaccess_auth()
    log(f"modis: running daily pipeline for {date_str}")
    granules = discover_mod11a1_granules(date_str=date_str)
    if not granules:
        raise RuntimeError(f"No MOD11A1 granules found for {date_str} over the Egypt bounding box.")

    found_tile_ids = {granule_tile_id(granule).lower() for granule in granules}
    missing_tiles = sorted(REQUIRED_TILE_IDS - found_tile_ids)
    if missing_tiles:
        raise RuntimeError(f"Missing required MODIS tiles for Egypt coverage: {', '.join(missing_tiles)}")

    downloaded_files = download_mod11a1_granules(granules, RAW_DATA_DIR)
    if not downloaded_files:
        raise RuntimeError("No MOD11A1 granules were downloaded successfully.")

    output_dir = PROCESSED_DATA_DIR / "egypt"
    output_dir.mkdir(parents=True, exist_ok=True)
    bounds = bounds_for_egypt()
    width, height = destination_grid_size(bounds, 0.01)
    merged = np.full((height, width), np.nan, dtype=np.float32)
    all_rgb_values: list[np.ndarray] = []
    good_quality_pixels = 0
    warnings: list[str] = []

    for hdf_path in downloaded_files:
        if not hdf_path.exists():
            warnings.append(f"Missing downloaded file: {hdf_path.name}")
            continue

        lst_tif = output_dir / f"{hdf_path.stem}_LST_Day_1km.tif"
        qc_tif = output_dir / f"{hdf_path.stem}_QC_Day.tif"

        try:
            extract_subdataset_to_tiff(hdf_path, "LST_Day_1km", lst_tif)
            extract_subdataset_to_tiff(hdf_path, "QC_Day", qc_tif)
            lst_data, lst_profile = read_raster(lst_tif)
            qc_data, _ = read_raster(qc_tif)
        except Exception as exc:
            warnings.append(f"Could not process {hdf_path.name}: {exc}")
            continue

        valid_mask = valid_lst_mask(lst_data) & mandatory_qa_mask(qc_data)
        good_quality_pixels += int(np.count_nonzero(valid_mask))
        if not np.any(valid_mask):
            warnings.append(f"No valid MODIS LST pixels after QA filtering for {hdf_path.name}")
            continue

        valid_temperature = convert_kelvin_to_celsius(lst_data[valid_mask])
        if valid_temperature.size:
            all_rgb_values.append(valid_temperature)

        tile_temperature = np.full(lst_data.shape, np.nan, dtype=np.float32)
        tile_temperature[valid_mask] = valid_temperature
        projected = reproject_to_4326(tile_temperature, lst_profile, bounds, width, height)
        merged = np.where(np.isfinite(projected), projected, merged)

    if not all_rgb_values:
        raise RuntimeError("No valid MODIS LST pixels remained after QC filtering for the Egypt crop.")

    finite_values = merged[np.isfinite(merged)]
    if finite_values.size == 0:
        raise RuntimeError("No finite temperature values were preserved in the final Egypt mosaic.")

    mean_temperature = float(np.mean(finite_values))

    final_raster_path = output_dir / f"egypt_lst_day_{date_str.replace('-', '_')}.tif"
    heatmap_path = output_dir / f"egypt_lst_heatmap_{date_str.replace('-', '_')}.png"
    report_path = output_dir / f"egypt_modis_report_{date_str.replace('-', '_')}.json"

    with rasterio.open(
        final_raster_path,
        "w",
        driver="GTiff",
        height=merged.shape[0],
        width=merged.shape[1],
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_bounds(*bounds, width=width, height=height),
        nodata=np.nan,
    ) as dst:
        dst.write(merged.astype(np.float32), 1)

    generate_heatmap(merged, bounds, f"MODIS Land Surface Temperature for Egypt — {date_str}", heatmap_path)

    report = {
        "date": date_str,
        "product_name": "MOD11A1",
        "collection_version": "061",
        "bounding_box": {"west": bounds[0], "south": bounds[1], "east": bounds[2], "north": bounds[3]},
        "granules_downloaded": len(downloaded_files),
        "required_tiles": sorted(REQUIRED_TILE_IDS),
        "detected_tiles": sorted(found_tile_ids),
        "total_pixels": int(merged.size),
        "valid_pixels": int(finite_values.size),
        "good_quality_pixels": int(good_quality_pixels),
        "mean_temperature_celsius": mean_temperature,
        "minimum_temperature_celsius": float(np.min(finite_values)),
        "maximum_temperature_celsius": float(np.max(finite_values)),
        "crs": "EPSG:4326",
        "spatial_resolution_degrees": 0.01,
        "processing_warnings": warnings,
        "limitations": [
            "Approximate Egypt bounding box used instead of an exact administrative boundary polygon.",
            "Only mandatory QA = 00 pixels are accepted in this initial pipeline.",
            "The regional mean is computed after cropping to the Egypt bounding box, not using a single tile average.",
        ],
    }

    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)

    return {
        "date": date_str,
        "granules": [str(path) for path in downloaded_files],
        "raster_path": str(final_raster_path),
        "heatmap_path": str(heatmap_path),
        "report_path": str(report_path),
        "mean_temperature_celsius": mean_temperature,
        "min_temperature_celsius": float(np.min(finite_values)),
        "max_temperature_celsius": float(np.max(finite_values)),
        "valid_pixels": int(finite_values.size),
        "warnings": warnings,
    }


if __name__ == "__main__":
    print(json.dumps(run_modis_egypt_pipeline("2024-01-01"), indent=2))
