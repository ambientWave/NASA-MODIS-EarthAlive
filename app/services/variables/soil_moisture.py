"""Soil-moisture collector backed by SMAP L3 daily via AppEEARS."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from app.core.config import get_settings
from app.services.variables.base import CollectionRequest, VariableSeries, VariableSpec, build_record
from app.services.variables.remote_grid import collect_daily_grid
from app.utils.logging import log

SPEC = VariableSpec(
    variable_id="soil_moisture",
    display_name="Soil Moisture",
    dataset="SPL3SMP_E",
    unit="m3/m3",
    native_resolution="daily",
    value_label="Soil moisture",
)


def collect_soil_moisture_appeears(request: CollectionRequest) -> VariableSeries:
    """Collect SMAP Soil Moisture (SPL3SMP_E.006) using NASA AppEEARS ARD."""
    from app.services.appeears_client import AppEEARSClient
    from app.services.modis_processor import read_raster
    from app.services.modis_timeseries import parse_appeears_filename_date

    bounds = (request.aoi["west"], request.aoi["south"], request.aoi["east"], request.aoi["north"])
    product_and_version = "SPL3SMP_E.006"
    layer_name = "Soil_Moisture_Retrieval_Data_AM_soil_moisture"

    client = AppEEARSClient()
    client.login()

    task_name = f"smap_{request.start_date.replace('-', '')}"
    task_id = client.submit_area_task(
        task_name=task_name,
        start_date=request.start_date,
        end_date=request.end_date,
        layers=[(product_and_version, layer_name)],
        bbox=request.aoi,
        file_type="geotiff",
        projection="geographic",
    )

    request.check_cancel()
    out_dir = Path(request.output_dir) / "raw_appeears"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        client.wait_for_task(
            task_id=task_id,
            should_cancel=request.should_cancel,
            on_progress=lambda pct, msg: request.report(pct, 100, detail=msg),
        )
        downloaded = client.download_bundle(task_id, target_dir=out_dir, file_patterns=(".tif",))
    finally:
        client.delete_task(task_id)

    files_by_date: dict[str, Path] = {}
    for p in downloaded:
        d_str = parse_appeears_filename_date(p.name)
        if d_str:
            files_by_date[d_str] = p

    daily_values: dict[str, dict[str, Any]] = {}
    raster_sum: np.ndarray | None = None
    raster_count: np.ndarray | None = None

    for day_str, tif_path in sorted(files_by_date.items()):
        try:
            raw, _ = read_raster(tif_path)
            valid_mask = (raw >= 0.0) & (raw <= 0.8) & np.isfinite(raw)
            valid_vals = raw[valid_mask]
            valid_pixels = int(valid_vals.size)
            total_pixels = int(raw.size)
            val = float(np.mean(valid_vals)) if valid_pixels > 0 else None

            daily_values[day_str] = {
                "value": val,
                "valid_pixels": valid_pixels,
                "total_pixels": total_pixels,
                "granules": [tif_path.name],
            }

            grid = np.full(raw.shape, np.nan, dtype=np.float32)
            grid[valid_mask] = valid_vals
            finite = np.isfinite(grid)
            if np.any(finite):
                if raster_sum is None:
                    raster_sum = np.zeros(grid.shape, dtype=np.float64)
                    raster_count = np.zeros(grid.shape, dtype=np.int32)
                raster_sum[finite] += grid[finite]
                raster_count[finite] += 1
        except Exception as exc:
            log(f"variables: soil_moisture: error processing {tif_path.name}: {exc}")

    # Build sequence of daily records for each day in range
    start = date.fromisoformat(request.start_date)
    end = date.fromisoformat(request.end_date)
    sample_days = set(request.sample_days) if request.sample_days else None
    records: list[dict[str, Any]] = []

    current = start
    while current <= end:
        request.check_cancel()
        day_str = current.isoformat()
        current += timedelta(days=1)
        if sample_days is not None and day_str not in sample_days:
            continue

        entry = daily_values.get(day_str)
        if entry and entry["value"] is not None:
            records.append(
                build_record(
                    day_str,
                    entry["value"],
                    SPEC,
                    valid_pixels=entry["valid_pixels"],
                    total_pixels=entry["total_pixels"],
                    granules=entry["granules"],
                )
            )
        else:
            records.append(
                build_record(
                    day_str,
                    None,
                    SPEC,
                    missing_reason=f"No valid SPL3SMP_E observations for {day_str}.",
                )
            )

    mean_raster = None
    if raster_sum is not None and raster_count is not None:
        valid_cells = raster_count > 0
        mean_raster = np.full(raster_sum.shape, np.nan, dtype=np.float32)
        mean_raster[valid_cells] = (raster_sum[valid_cells] / raster_count[valid_cells]).astype(np.float32)

    return VariableSeries(
        spec=SPEC,
        records=records,
        frequency="daily",
        collection_strategy="daily",
        source_products=["SPL3SMP_E.006"],
        source_resolution="9km",
        mean_raster=mean_raster,
        raster_bounds=bounds,
    )


def collect(request: CollectionRequest) -> VariableSeries:
    provider = get_settings().soil_moisture_provider
    if provider != "earthaccess":
        try:
            return collect_soil_moisture_appeears(request)
        except Exception as exc:
            log(f"variables: soil_moisture: AppEEARS collection failed ({exc}), falling back to direct grid")
    return collect_daily_grid(
        request,
        SPEC,
        short_name="SPL3SMP_E",
        version="006",
        variable_names=("soil_moisture", "soil_moisture_am", "soil_moisture_pm"),
        valid_min=0.0,
        valid_max=0.8,
        quality_variable="retrieval_qual_flag",
        quality_value=0,
    )

