"""Collection from provider-produced monthly products.

Monthly requests should not download every daily granule and average them
afterwards. Products such as MOD11C3 (LST), MOD13C2 (NDVI), GPM_3IMERGM
(precipitation) and M2TMNXSLV (air temperature) are produced by the provider
from every source observation, so one file replaces a whole month.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

import earthaccess

from app.services.earthdata_auth import ensure_earthaccess_auth
from app.services.modis_processor import (
    destination_grid_size,
    download_granules,
    read_raster,
    warp_subdataset_to_aoi,
)
from app.services.temporal import days_in_month, monthly_periods
from app.services.variables.base import CollectionRequest, PeriodSource, VariableSeries, VariableSpec, build_record
from app.services.variables.remote_grid import _mean_for_granules, granule_date
from app.utils import cache
from app.utils.logging import log


def _aoi_hash(bounds: tuple[float, float, float, float], product: str) -> str:
    text = "|".join(f"{value:.6f}" for value in bounds) + f"|{product}"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _period_bounds(period: str) -> tuple[date, date]:
    year, month = (int(part) for part in period.split("-"))
    return date(year, month, 1), date(year, month, days_in_month(year, month))


def _mean_xarray_period(
    granules: list[Any],
    aoi: dict[str, float],
    source: PeriodSource,
) -> tuple[float | None, int, int]:
    return _mean_for_granules(
        granules,
        aoi,
        source.variable_names,
        scale=source.scale,
        offset=source.offset,
        valid_min=source.valid_min,
        valid_max=source.valid_max,
        quality_variable=source.quality_variable,
        quality_value=source.quality_value,
    )


def _warp_gdal_period(
    hdf_path: Path,
    period: str,
    source: PeriodSource,
    bounds: tuple[float, float, float, float],
    aoi_hash: str,
    output_dir: Path,
) -> tuple[np.ndarray | None, float | None, int, int]:
    """Warp one coarse HDF4 period onto the AOI grid and return its mean."""
    width, height = destination_grid_size(bounds, source.analysis_resolution)
    cache_dir = cache.cache_root() / source.short_name.lower() / "aoi" / aoi_hash
    raster_path = cache_dir / f"{Path(hdf_path).stem}.tif"

    if not raster_path.is_file():
        with cache.atomic_output(raster_path) as temporary:
            warp_subdataset_to_aoi(
                hdf_path,
                str(source.gdal_subdataset),
                temporary,
                grid_name=str(source.gdal_grid),
                bounds=bounds,
                width=width,
                height=height,
                resampling=source.gdal_resampling,
                src_nodata=source.gdal_src_nodata,
                dst_nodata=source.gdal_src_nodata,
                output_type=source.gdal_output_type,
            )
    else:
        cache.touch(raster_path)

    try:
        raw, _ = read_raster(raster_path)
    except Exception as exc:
        log(f"period: could not read warped raster {raster_path.name}: {type(exc).__name__}: {exc}")
        return None, None, 0, 0

    finite = np.isfinite(raw)
    if source.valid_min is not None:
        finite &= raw >= source.valid_min
    if source.valid_max is not None:
        finite &= raw <= source.valid_max
    total = int(raw.size)
    if not np.any(finite):
        return None, None, 0, total

    values = raw[finite].astype(np.float64) * source.scale + source.offset
    merged = np.full(raw.shape, np.nan, dtype=np.float32)
    merged[finite] = values.astype(np.float32)
    return merged, float(np.mean(values)), int(np.count_nonzero(finite)), total


def collect_period_variable(
    request: CollectionRequest,
    spec: VariableSpec,
    source: PeriodSource,
) -> VariableSeries:
    """Collect one provider-aggregated record per month over the request range."""
    ensure_earthaccess_auth()
    bounds = (request.aoi["west"], request.aoi["south"], request.aoi["east"], request.aoi["north"])
    aoi_hash = _aoi_hash(bounds, source.short_name)
    raw_dir = cache.cache_root() / source.short_name.lower() / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    periods = monthly_periods(request.start_date, request.end_date)
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    raster_sum: np.ndarray | None = None
    raster_count: np.ndarray | None = None

    for index, period in enumerate(periods, start=1):
        request.check_cancel()
        first, last = _period_bounds(period)
        request.report(index - 1, len(periods), detail=f"monthly {period} ({index}/{len(periods)})")
        log(f"period: {spec.variable_id}: {source.short_name} {period}")
        granules = earthaccess.search_data(
            short_name=source.short_name,
            version=source.version,
            count=-1,
            temporal=(first.isoformat(), last.isoformat()),
            bounding_box=bounds,
        )
        if not granules:
            records.append(
                build_record(
                    first,
                    None,
                    spec,
                    missing_reason=f"No {source.short_name} granule was found for this month.",
                    source_frequency="monthly",
                )
            )
            continue

        value: float | None = None
        valid_pixels = 0
        total_pixels = 0
        try:
            if source.reader == "gdal":
                paths, _, _ = download_granules(
                    granules,
                    raw_dir,
                    label=f"for {source.short_name} {period}",
                )
                merged_rasters: list[np.ndarray] = []
                weighted_sum = 0.0
                weighted_pixels = 0
                for path in paths:
                    if not Path(path).is_file():
                        warnings.append(f"{period}: {Path(path).name} could not be downloaded.")
                        continue
                    merged, mean_value, valid, total = _warp_gdal_period(
                        Path(path), period, source, bounds, aoi_hash, request.output_dir
                    )
                    total_pixels += total
                    if mean_value is None or merged is None:
                        continue
                    valid_pixels += valid
                    weighted_sum += mean_value * valid
                    weighted_pixels += valid
                    merged_rasters.append(merged)
                if weighted_pixels:
                    value = weighted_sum / weighted_pixels
                for merged in merged_rasters:
                    finite = np.isfinite(merged)
                    if not np.any(finite):
                        continue
                    if raster_sum is None:
                        raster_sum = np.zeros(merged.shape, dtype=np.float64)
                        raster_count = np.zeros(merged.shape, dtype=np.int32)
                    raster_sum[finite] += merged[finite]
                    raster_count[finite] += 1
            else:
                value, valid_pixels, total_pixels = _mean_xarray_period(granules, request.aoi, source)
        except Exception as exc:
            log(f"period: {spec.variable_id}: {period} failed: {type(exc).__name__}: {exc}")
            warnings.append(f"{period}: could not read {source.short_name} data ({type(exc).__name__}: {exc}).")
            records.append(
                build_record(
                    first,
                    None,
                    spec,
                    missing_reason=str(exc),
                    source_frequency="monthly",
                )
            )
            continue

        if value is not None and source.period_statistic == "total":
            # Provider totals (mm/month) are converted to the variable's mean
            # daily rate so the unit stays consistent across resolutions.
            value = value / float(days_in_month(first.year, first.month))

        records.append(
            build_record(
                first,
                value,
                spec,
                valid_pixels=valid_pixels,
                total_pixels=total_pixels,
                missing_reason=None if value is not None else "No valid pixels after QA filtering.",
                coverage_status="provider_aggregate" if value is not None else "missing",
                source_frequency="monthly",
            )
        )
        request.report(index, len(periods), detail=f"monthly {period} ({index}/{len(periods)})")

    mean_raster = None
    if raster_sum is not None and raster_count is not None:
        mean_raster = np.full(raster_sum.shape, np.nan, dtype=np.float32)
        valid = raster_count > 0
        mean_raster[valid] = (raster_sum[valid] / raster_count[valid]).astype(np.float32)

    return VariableSeries(
        spec=spec,
        records=records,
        warnings=warnings,
        mean_raster=mean_raster,
        raster_bounds=bounds if mean_raster is not None else None,
        frequency="monthly",
        collection_strategy="provider_monthly",
        source_products=[source.short_name],
        source_resolution=source.native_resolution,
    )
