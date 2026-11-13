"""Vegetation collector backed by MOD13Q1 16-day NDVI.

The source product is 16-day, not daily. To keep every variable on the same
daily response frequency, each composite value is carried forward across its
16-day window and the record declares ``source_frequency="16-day"``. This is an
explicit period value, not an interpolated synthetic observation.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import earthaccess
import numpy as np

from app.services.modis_processor import (
    download_mod11a1_granules,
    extract_subdataset_to_tiff,
    granule_temporal_start,
    granule_tile_id,
    read_raster,
    reproject_to_4326,
)
from app.services.variables.base import CollectionRequest, VariableSeries, VariableSpec, build_record
from app.services.variables.remote_grid import date_chunks, granule_date
from app.services.modis_processor import destination_grid_size
from app.utils.logging import log

SPEC = VariableSpec(
    variable_id="ndvi",
    display_name="Vegetation Index (NDVI)",
    dataset="MOD13Q1",
    unit="NDVI",
    native_resolution="16-day",
    value_label="NDVI",
)

GRID_NAME = "MODIS_Grid_16DAY_250m_500m_VI"
NDVI_SUBDATASET = "250m 16 days NDVI"
QA_SUBDATASET = "250m 16 days pixel reliability"
DAILY_FILL_DAYS = 16


def _composite_mean(
    hdf_files: list[Path],
    aoi: dict[str, float],
    scratch_dir: Path,
) -> tuple[float | None, int, int, list[str], np.ndarray | None]:
    bounds = (aoi["west"], aoi["south"], aoi["east"], aoi["north"])
    width, height = destination_grid_size(bounds, 0.0025)
    merged = np.full((height, width), np.nan, dtype=np.float32)
    granules: list[str] = []

    for hdf_path in hdf_files:
        if not hdf_path.exists():
            continue
        granules.append(str(hdf_path))
        ndvi_tif = scratch_dir / f"{hdf_path.stem}_NDVI.tif"
        qa_tif = scratch_dir / f"{hdf_path.stem}_pixel_reliability.tif"
        try:
            extract_subdataset_to_tiff(hdf_path, NDVI_SUBDATASET, ndvi_tif, grid_name=GRID_NAME)
            extract_subdataset_to_tiff(hdf_path, QA_SUBDATASET, qa_tif, grid_name=GRID_NAME)
            ndvi, profile = read_raster(ndvi_tif)
            qa, _ = read_raster(qa_tif)
        except Exception as exc:
            log(f"variables: ndvi: could not process {hdf_path.name}: {exc}")
            continue

        # Pixel reliability: 0 = good, 1 = marginal, 2 = snow/ice, 3 = cloudy.
        valid = (ndvi >= -2000) & (ndvi <= 10000) & (qa <= 1)
        if not np.any(valid):
            continue

        values = ndvi[valid].astype(np.float32) * 0.0001
        tile = np.full(ndvi.shape, np.nan, dtype=np.float32)
        tile[valid] = values
        projected = reproject_to_4326(tile, profile, bounds, width, height)
        merged = np.where(np.isfinite(projected), projected, merged)

    finite = merged[np.isfinite(merged)]
    total = int(merged.size)
    if finite.size == 0:
        return None, 0, total, granules, None
    return float(np.mean(finite)), int(finite.size), total, granules, merged


def collect(request: CollectionRequest) -> VariableSeries:
    output_dir = Path(request.output_dir)
    raw_dir = output_dir / "raw"
    scratch_dir = output_dir / "scratch"
    raw_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    composite_records: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    raster_sum: np.ndarray | None = None
    raster_count: np.ndarray | None = None

    for chunk_start, chunk_end in date_chunks(request.start_date, request.end_date, 366):
        log(f"variables: ndvi: searching MOD13Q1 {chunk_start} -> {chunk_end}")
        granules = earthaccess.search_data(
            short_name="MOD13Q1",
            version="061",
            count=-1,
            temporal=(chunk_start, chunk_end),
            bounding_box=(
                request.aoi["west"],
                request.aoi["south"],
                request.aoi["east"],
                request.aoi["north"],
            ),
        )
        grouped: dict[str, list[Any]] = {}
        for granule in granules:
            composite_date = granule_date(granule) or granule_temporal_start(granule)
            if composite_date:
                grouped.setdefault(composite_date, []).append(granule)

        for composite_date in sorted(grouped):
            day_granules = grouped[composite_date]
            log(
                f"variables: ndvi: composite {composite_date} "
                f"({len(day_granules)} granule(s), tiles={sorted({granule_tile_id(g) for g in day_granules})})"
            )
            downloaded = download_mod11a1_granules(day_granules, raw_dir)
            if not downloaded:
                warnings.append(f"{composite_date}: MOD13Q1 granules could not be downloaded.")
                continue
            value, valid, total, used, merged = _composite_mean([Path(p) for p in downloaded], request.aoi, scratch_dir)
            composite_records[composite_date] = {
                "value": value,
                "valid_pixels": valid,
                "total_pixels": total,
                "granules": used,
            }
            if merged is not None:
                finite = np.isfinite(merged)
                if np.any(finite):
                    if raster_sum is None:
                        raster_sum = np.zeros(merged.shape, dtype=np.float64)
                        raster_count = np.zeros(merged.shape, dtype=np.int32)
                    raster_sum[finite] += merged[finite]
                    raster_count[finite] += 1

    # Carry each 16-day composite forward across its own window to produce a
    # daily response series. Days without a composite remain explicit misses.
    composite_dates = sorted(composite_records)
    records: list[dict[str, Any]] = []
    current = date.fromisoformat(request.start_date)
    end = date.fromisoformat(request.end_date)
    while current <= end:
        day = current.isoformat()
        source = None
        for composite_date in reversed(composite_dates):
            if composite_date <= day:
                source = composite_date
                break
        if source is None:
            records.append(
                build_record(
                    day,
                    None,
                    SPEC,
                    missing_reason="No MOD13Q1 composite is available on or before this date.",
                    source_frequency="16-day",
                )
            )
        else:
            source_day = date.fromisoformat(source)
            if (current - source_day).days >= DAILY_FILL_DAYS:
                records.append(
                    build_record(
                        day,
                        None,
                        SPEC,
                        missing_reason=f"The most recent MOD13Q1 composite ({source}) is outside its 16-day window.",
                        source_frequency="16-day",
                    )
                )
            else:
                composite = composite_records[source]
                records.append(
                    build_record(
                        day,
                        composite["value"],
                        SPEC,
                        valid_pixels=composite["valid_pixels"],
                        total_pixels=composite["total_pixels"],
                        missing_reason=(
                            None
                            if composite["value"] is not None
                            else "No valid NDVI pixels after pixel-reliability filtering."
                        ),
                        granules_used=composite["granules"],
                        coverage_status="period_value",
                        source_frequency="16-day",
                    )
                )
        current += timedelta(days=1)

    mean_raster = None
    if raster_sum is not None and raster_count is not None:
        mean_raster = np.full(raster_sum.shape, np.nan, dtype=np.float32)
        valid = raster_count > 0
        mean_raster[valid] = (raster_sum[valid] / raster_count[valid]).astype(np.float32)

    return VariableSeries(
        spec=SPEC,
        records=records,
        warnings=warnings,
        mean_raster=mean_raster,
        raster_bounds=(request.aoi["west"], request.aoi["south"], request.aoi["east"], request.aoi["north"]),
    )
