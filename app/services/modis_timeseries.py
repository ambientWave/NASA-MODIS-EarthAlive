from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from app.services.modis_processor import (
        PROCESSED_DATA_DIR,
        RAW_DATA_DIR,
        convert_kelvin_to_celsius,
        destination_grid_size,
        discover_mod11a1_granules,
        download_mod11a1_granules,
        ensure_earthaccess_auth,
        extract_subdataset_to_tiff,
        find_gdal_translate,
        mandatory_qa_mask,
        read_raster,
        reproject_to_4326,
        valid_lst_mask,
    )
from app.utils.logging import log, log_progress

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TIME_SERIES_DIR = PROCESSED_DATA_DIR / "time_series"
DEFAULT_CACHE_PATH = PROJECT_ROOT / "data" / "cache" / "modis_granule_cache.json"


@dataclass
class TimeSeriesConfig:
    aoi: dict[str, float]
    start_date: str
    end_date: str
    product_short_name: str = "MOD11A1"
    temporal_aggregation: str = "daily"
    output_dir: str | Path | None = None
    cache_path: str | Path | None = None
    region_name: str | None = None

    def __post_init__(self) -> None:
        self.aoi = {
            "west": float(self.aoi["west"]),
            "south": float(self.aoi["south"]),
            "east": float(self.aoi["east"]),
            "north": float(self.aoi["north"]),
        }
        if self.aoi["west"] >= self.aoi["east"]:
            raise ValueError("AOI west must be less than east.")
        if self.aoi["south"] >= self.aoi["north"]:
            raise ValueError("AOI south must be less than north.")
        if self.product_short_name.upper() != "MOD11A1":
            raise ValueError("This service currently supports the MOD11A1 product only.")
        aggregation = self.temporal_aggregation.lower()
        if aggregation not in {"daily", "monthly", "yearly"}:
            raise ValueError("temporal_aggregation must be one of: daily, monthly, yearly.")
        self.temporal_aggregation = aggregation
        self.output_dir = Path(self.output_dir) if self.output_dir is not None else TIME_SERIES_DIR
        self.cache_path = Path(self.cache_path) if self.cache_path is not None else DEFAULT_CACHE_PATH
        if self.region_name is None:
            self.region_name = "custom_aoi"
        self.region_name = str(self.region_name).strip().lower() or "custom_aoi"


class GranuleCache:
    def __init__(self, cache_path: str | Path = DEFAULT_CACHE_PATH):
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = self._load()

    def _load(self) -> dict[str, str]:
        if not self.cache_path.exists():
            return {}
        try:
            with self.cache_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return {str(key): str(value) for key, value in payload.items()}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        with self.cache_path.open("w", encoding="utf-8") as handle:
            json.dump(self.store, handle, indent=2, sort_keys=True)

    @staticmethod
    def granule_identity(granule: Any) -> str:
        if isinstance(granule, str):
            return granule.strip().lower()

        possible_values = [
            granule.get("umm", {}).get("GranuleUR", ""),
            granule.get("umm", {}).get("DataGranule", {}).get("ProducerGranuleId", ""),
            str(granule),
        ]
        for value in possible_values:
            text = str(value).strip()
            if text:
                candidate = text.split("/")[-1]
                return candidate.lower()
        return "unknown-granule"

    def is_cached(self, granule: Any) -> bool:
        key = self.granule_identity(granule)
        return key in self.store and Path(self.store[key]).exists()

    def mark_downloaded(self, granule: Any, download_path: str | Path) -> None:
        key = self.granule_identity(granule)
        self.store[key] = str(download_path)
        self._save()

    def download_missing(self, granules: Iterable[Any], target_dir: str | Path) -> list[Path]:
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        granule_list = list(granules)

        missing_granules: list[Any] = []
        for granule in granule_list:
            if not self.is_cached(granule):
                missing_granules.append(granule)

        if not missing_granules:
            log(f"timeseries: all {len(granule_list)} granules already cached")
            return [Path(self.store[self.granule_identity(granule)]) for granule in granule_list if self.is_cached(granule)]

        log(f"timeseries: downloading {len(missing_granules)} granule(s)")
        downloaded = download_mod11a1_granules(missing_granules, target_dir)
        log(f"timeseries: downloaded {len(downloaded)} granule(s)")
        for granule, path in zip(missing_granules, downloaded):
            self.mark_downloaded(granule, path)

        cached_paths: list[Path] = []
        for granule in granule_list:
            key = self.granule_identity(granule)
            stored_path = self.store.get(key)
            if stored_path:
                cached_paths.append(Path(stored_path))
        return cached_paths


def parse_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def date_range(start_date: str | date | datetime, end_date: str | date | datetime) -> list[str]:
    start = parse_date(start_date)
    end = parse_date(end_date)
    if end < start:
        raise ValueError("end_date must be on or after start_date.")

    dates: list[str] = []
    current = start
    while current <= end:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def build_missing_observation_record(
    observation_date: str,
    reason: str,
    *,
    warnings: Iterable[str] | None = None,
    granules_used: Iterable[str] | None = None,
    total_pixels: int = 0,
) -> dict[str, Any]:
    return {
        "observation_date": observation_date,
        "mean_temperature_celsius": None,
        "minimum_temperature_celsius": None,
        "maximum_temperature_celsius": None,
        "valid_pixels": 0,
        "total_pixels": int(total_pixels),
        "valid_data_percentage": 0.0,
        "missing_observation": True,
        "missing_reason": reason,
        "granules_used": list(granules_used or []),
        "coverage_status": "missing",
        "warnings": list(dict.fromkeys(warnings or [])),
    }


def summarize_temperature_array(temperature_array: np.ndarray) -> dict[str, Any]:
    array = np.asarray(temperature_array, dtype=np.float32)
    finite = array[np.isfinite(array)]
    total_pixels = int(array.size)
    valid_pixels = int(finite.size)
    valid_pct = 0.0 if total_pixels == 0 else float((valid_pixels / total_pixels) * 100.0)

    if valid_pixels == 0:
        return {
            "mean_temperature_celsius": None,
            "minimum_temperature_celsius": None,
            "maximum_temperature_celsius": None,
            "valid_pixels": 0,
            "total_pixels": total_pixels,
            "valid_data_percentage": valid_pct,
        }

    return {
        "mean_temperature_celsius": float(np.mean(finite)),
        "minimum_temperature_celsius": float(np.min(finite)),
        "maximum_temperature_celsius": float(np.max(finite)),
        "valid_pixels": valid_pixels,
        "total_pixels": total_pixels,
        "valid_data_percentage": valid_pct,
    }


def process_aoi_for_date(
    date_str: str,
    aoi: dict[str, float],
    output_dir: str | Path,
    granule_files: Iterable[str | Path],
) -> tuple[np.ndarray, list[str], dict[str, Any]]:
    bounds = (aoi["west"], aoi["south"], aoi["east"], aoi["north"])
    width, height = destination_grid_size(bounds, 0.01)
    merged = np.full((height, width), np.nan, dtype=np.float32)
    warnings: list[str] = []
    granule_paths: list[str] = []

    for hdf_path in granule_files:
        hdf_path = Path(hdf_path)
        if not hdf_path.exists():
            log(f"timeseries: {date_str}: missing downloaded file {hdf_path.name}")
            warnings.append(f"Missing downloaded file: {hdf_path.name}")
            continue

        granule_paths.append(str(hdf_path))
        lst_tif = Path(output_dir) / f"{hdf_path.stem}_LST_Day_1km.tif"
        qc_tif = Path(output_dir) / f"{hdf_path.stem}_QC_Day.tif"

        try:
            log(f"timeseries: {date_str}: extracting LST/QC from {hdf_path.name}")
            extract_subdataset_to_tiff(hdf_path, "LST_Day_1km", lst_tif)
            extract_subdataset_to_tiff(hdf_path, "QC_Day", qc_tif)
            lst_data, lst_profile = read_raster(lst_tif)
            qc_data, _ = read_raster(qc_tif)
        except Exception as exc:
            log(f"timeseries: {date_str}: failed to process {hdf_path.name}: {exc}")
            warnings.append(f"Could not process {hdf_path.name}: {exc}")
            continue

        valid_mask = valid_lst_mask(lst_data) & mandatory_qa_mask(qc_data)
        if not np.any(valid_mask):
            log(f"timeseries: {date_str}: no valid pixels after QA for {hdf_path.name}")
            warnings.append(f"No valid MODIS LST pixels after QA filtering for {hdf_path.name}")
            continue

        valid_temperature = convert_kelvin_to_celsius(lst_data[valid_mask])
        tile_temperature = np.full(lst_data.shape, np.nan, dtype=np.float32)
        tile_temperature[valid_mask] = valid_temperature
        projected = reproject_to_4326(tile_temperature, lst_profile, bounds, width, height)
        merged = np.where(np.isfinite(projected), projected, merged)

    summary = summarize_temperature_array(merged)
    log(
        f"timeseries: {date_str}: processed {len(granule_paths)} granule(s), "
        f"valid_pixels={summary['valid_pixels']}/{summary['total_pixels']}, "
        f"mean={summary['mean_temperature_celsius']}"
    )
    return merged, warnings, {"date": date_str, "granules_used": granule_paths, **summary}


def aggregate_period_records(records: list[dict[str, Any]], frequency: str) -> list[dict[str, Any]]:
    frequency = frequency.lower()
    if frequency == "daily":
        return records

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("missing_observation"):
            continue
        period_key = record["observation_date"][:7] if frequency == "monthly" else record["observation_date"][:4]
        grouped.setdefault(period_key, []).append(record)

    aggregated: list[dict[str, Any]] = []
    for key in sorted(grouped):
        observations = grouped[key]
        valid_pixels = int(sum(int(item["valid_pixels"]) for item in observations if item.get("valid_pixels") is not None))
        total_pixels = int(sum(int(item["total_pixels"]) for item in observations if item.get("total_pixels") is not None))
        mean_values = [item["mean_temperature_celsius"] for item in observations if item.get("mean_temperature_celsius") is not None]
        min_values = [item["minimum_temperature_celsius"] for item in observations if item.get("minimum_temperature_celsius") is not None]
        max_values = [item["maximum_temperature_celsius"] for item in observations if item.get("maximum_temperature_celsius") is not None]
        weight_values = [float(item["valid_pixels"]) for item in observations if item.get("valid_pixels") is not None]

        if mean_values:
            if weight_values:
                weighted_mean = float(np.average(mean_values, weights=weight_values))
            else:
                weighted_mean = float(np.mean(mean_values))
        else:
            weighted_mean = None

        record = {
            "observation_date": key,
            "mean_temperature_celsius": weighted_mean,
            "minimum_temperature_celsius": float(np.min(min_values)) if min_values else None,
            "maximum_temperature_celsius": float(np.max(max_values)) if max_values else None,
            "valid_pixels": valid_pixels,
            "total_pixels": total_pixels,
            "valid_data_percentage": 0.0 if total_pixels == 0 else float((valid_pixels / total_pixels) * 100.0),
            "missing_observation": False,
            "missing_reason": None,
            "granules_used": [granule for item in observations for granule in item.get("granules_used", [])],
            "coverage_status": "aggregated",
            "aggregation_frequency": frequency,
        }
        aggregated.append(record)

    return aggregated


def save_timeseries_outputs(records: list[dict[str, Any]], output_dir: str | Path, region_name: str, frequency: str) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / f"{region_name}_modis_timeseries_{frequency}.csv"
    json_path = output_dir / f"{region_name}_modis_timeseries_{frequency}.json"

    fieldnames = [
        "observation_date",
        "mean_temperature_celsius",
        "minimum_temperature_celsius",
        "maximum_temperature_celsius",
        "valid_pixels",
        "total_pixels",
        "valid_data_percentage",
        "missing_observation",
        "missing_reason",
        "granules_used",
        "coverage_status",
    ]

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = {
                "observation_date": record.get("observation_date", ""),
                "mean_temperature_celsius": "" if record.get("mean_temperature_celsius") is None else record["mean_temperature_celsius"],
                "minimum_temperature_celsius": "" if record.get("minimum_temperature_celsius") is None else record["minimum_temperature_celsius"],
                "maximum_temperature_celsius": "" if record.get("maximum_temperature_celsius") is None else record["maximum_temperature_celsius"],
                "valid_pixels": record.get("valid_pixels", 0),
                "total_pixels": record.get("total_pixels", 0),
                "valid_data_percentage": record.get("valid_data_percentage", 0.0),
                "missing_observation": str(record.get("missing_observation", False)),
                "missing_reason": record.get("missing_reason", ""),
                "granules_used": "; ".join(record.get("granules_used", [])),
                "coverage_status": record.get("coverage_status", "unknown"),
            }
            writer.writerow(row)

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(records, handle, indent=2)

    return {"csv": str(csv_path), "json": str(json_path)}


def collect_mod11a1_timeseries(config: TimeSeriesConfig) -> dict[str, Any]:
    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Fail fast when GDAL is unavailable, instead of recording every date as
    # "no valid pixels" and downloading granules that cannot be processed.
    gdal_translate = find_gdal_translate()
    log(f"timeseries: using gdal_translate at {gdal_translate}")
    log(f"timeseries: authenticating with NASA Earthdata")
    ensure_earthaccess_auth()

    cache = GranuleCache(config.cache_path)
    date_list = date_range(config.start_date, config.end_date)
    records: list[dict[str, Any]] = []
    raster_sum: np.ndarray | None = None
    raster_count: np.ndarray | None = None
    log(
        f"timeseries: collecting {len(date_list)} day(s) "
        f"({config.start_date} -> {config.end_date}, aoi={config.aoi}, "
        f"aggregation={config.temporal_aggregation})"
    )

    for index, date_str in enumerate(date_list, start=1):
        log_progress("timeseries: date", index, len(date_list), date_str)
        granules = discover_mod11a1_granules(date_str=date_str, bbox=config.aoi)
        if not granules:
            log(f"timeseries: {date_str}: no granules found")
            records.append(build_missing_observation_record(date_str, "No granules found for the AOI on this date."))
            continue
        log(f"timeseries: {date_str}: found {len(granules)} granule(s)")

        downloaded_files = cache.download_missing(granules, output_dir / "raw")
        if not downloaded_files:
            log(f"timeseries: {date_str}: granules were cached but files are missing")
            records.append(build_missing_observation_record(date_str, "Granules were already cached but files are missing or unreadable."))
            continue

        merged, warnings, summary = process_aoi_for_date(date_str, config.aoi, output_dir, downloaded_files)
        if merged is not None:
            finite = np.isfinite(merged)
            if np.any(finite):
                if raster_sum is None:
                    raster_sum = np.zeros(merged.shape, dtype=np.float64)
                    raster_count = np.zeros(merged.shape, dtype=np.int32)
                raster_sum[finite] += merged[finite]
                raster_count[finite] += 1
        obs_record = {
            "observation_date": date_str,
            "mean_temperature_celsius": summary.get("mean_temperature_celsius"),
            "minimum_temperature_celsius": summary.get("minimum_temperature_celsius"),
            "maximum_temperature_celsius": summary.get("maximum_temperature_celsius"),
            "valid_pixels": summary.get("valid_pixels", 0),
            "total_pixels": summary.get("total_pixels", 0),
            "valid_data_percentage": summary.get("valid_data_percentage", 0.0),
            "missing_observation": summary.get("valid_pixels", 0) == 0,
            "missing_reason": None if summary.get("valid_pixels", 0) > 0 else "No valid pixels after MODIS QA filtering.",
            "granules_used": summary.get("granules_used", []),
            "coverage_status": "complete" if summary.get("valid_pixels", 0) > 0 else "missing",
            "warnings": warnings,
        }

        if summary.get("valid_pixels", 0) == 0:
            reason = "; ".join(dict.fromkeys(warnings)) or "No valid pixels after QA filtering."
            records.append(
                build_missing_observation_record(
                    date_str,
                    reason,
                    warnings=warnings,
                    granules_used=summary.get("granules_used", []),
                    total_pixels=int(summary.get("total_pixels", 0) or 0),
                )
            )
        else:
            records.append(obs_record)

    if config.temporal_aggregation != "daily":
        log(f"timeseries: aggregating {len(records)} daily records to {config.temporal_aggregation}")
        records = aggregate_period_records(records, config.temporal_aggregation)

    region_name = config.region_name
    log(f"timeseries: writing {len(records)} record(s) to {output_dir}")
    output_files = save_timeseries_outputs(records, output_dir, region_name, config.temporal_aggregation)
    log(f"timeseries: outputs written: {output_files}")

    mean_raster = None
    if raster_sum is not None and raster_count is not None:
        mean_raster = np.full(raster_sum.shape, np.nan, dtype=np.float32)
        valid = raster_count > 0
        mean_raster[valid] = (raster_sum[valid] / raster_count[valid]).astype(np.float32)

    return {
        "region_name": region_name,
        "aoi": config.aoi,
        "start_date": config.start_date,
        "end_date": config.end_date,
        "product_short_name": config.product_short_name,
        "temporal_aggregation": config.temporal_aggregation,
        "records": records,
        "output_files": output_files,
        "cache_path": str(config.cache_path),
        "mean_lst_raster": mean_raster,
        "raster_bounds": (
            config.aoi["west"],
            config.aoi["south"],
            config.aoi["east"],
            config.aoi["north"],
        ),
    }
