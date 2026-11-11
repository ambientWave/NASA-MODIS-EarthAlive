from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import earthaccess
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import reproject

try:
    from app.services.earthdata_auth import ensure_earthaccess_auth
except ModuleNotFoundError as exc:  # pragma: no cover - repo-root compatibility
    if exc.name not in {"app", "backend"}:
        raise
    from backend.app.services.earthdata_auth import ensure_earthaccess_auth

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

    return sorted(filtered, key=lambda granule: granule_tile_id(granule))


def download_mod11a1_granules(granules: list[Any], target_dir: str | Path | None = None) -> list[Path]:
    target_dir = Path(target_dir) if target_dir is not None else RAW_DATA_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    paths = earthaccess.download(granules, local_path=str(target_dir), threads=4, show_progress=False)
    return [Path(path) for path in paths]


def extract_subdataset_to_tiff(hdf_path: str | Path, subdataset_name: str, output_tif: str | Path) -> Path:
    hdf_path = str(hdf_path)
    output_tif = Path(output_tif)
    output_tif.parent.mkdir(parents=True, exist_ok=True)
    gdal_uri = f'HDF4_EOS:EOS_GRID:"{hdf_path}":MODIS_Grid_Daily_1km_LST:{subdataset_name}'
    subprocess.run(
        [
            "gdal_translate",
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
