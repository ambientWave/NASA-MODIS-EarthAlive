"""Vegetation collector backed by 16-day MODIS NDVI.

The default product is **MOD13A1 (500 m)** rather than MOD13Q1 (250 m). At the
analysis resolution used here (0.005°, about 500 m) the 250 m product is
downsampled anyway, but its granules are ~4x larger: a full-AOI year of
MOD13A1 is roughly 4.5 GB against 18 GB for MOD13Q1. Set
``NDVI_PRODUCT=MOD13Q1`` to switch back.

The source product is 16-day, not daily. To keep every variable on the same
daily response frequency, each composite value is carried forward across its
16-day window and the record declares ``source_frequency="16-day"``. This is an
explicit period value, not an interpolated synthetic observation.

Work is organised in *waves*: a bounded set of whole composites is downloaded
together (many parallel HTTP streams), then every granule in the wave is warped
straight onto the AOI grid in a thread pool, then the wave's per-composite
means are computed and the shared cache is trimmed. That keeps peak disk use
bounded by one wave instead of one year of granules.
"""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

import earthaccess
import numpy as np

from app.services.earthdata_auth import ensure_earthaccess_auth
from app.services.modis_processor import (
    destination_grid_size,
    download_granules,
    granule_filename,
    granule_temporal_start,
    granule_tile_id,
    read_raster,
    warp_subdataset_to_aoi,
)
from app.services.variables.base import CollectionRequest, VariableSeries, VariableSpec, build_record
from app.services.variables.remote_grid import date_chunks, granule_date
from app.utils import cache
from app.utils.logging import log

PRODUCTS: dict[str, dict[str, Any]] = {
    "MOD13A1": {
        "grid": "MODIS_Grid_16DAY_500m_VI",
        "ndvi": "500m 16 days NDVI",
        "qa": "500m 16 days pixel reliability",
        "resolution_deg": 0.005,
    },
    "MOD13Q1": {
        "grid": "MODIS_Grid_16DAY_250m_500m_VI",
        "ndvi": "250m 16 days NDVI",
        "qa": "250m 16 days pixel reliability",
        "resolution_deg": 0.0025,
    },
}

PRODUCT = os.environ.get("NDVI_PRODUCT", "MOD13A1").strip().upper()
if PRODUCT not in PRODUCTS:  # fail loudly at import: a typo must not silently change datasets
    raise ValueError(f"Unsupported NDVI_PRODUCT '{PRODUCT}'. Choose one of {sorted(PRODUCTS)}.")
CONFIG = PRODUCTS[PRODUCT]

SPEC = VariableSpec(
    variable_id="ndvi",
    display_name="Vegetation Index (NDVI)",
    dataset=PRODUCT,
    unit="NDVI",
    native_resolution="16-day",
    value_label="NDVI",
)

DAILY_FILL_DAYS = 16
GRANULE_BATCH = int(os.environ.get("NDVI_GRANULE_BATCH", "8"))
WARP_WORKERS = max(1, int(os.environ.get("NDVI_WARP_WORKERS", "4")))
DOWNLOAD_THREADS = max(1, int(os.environ.get("NDVI_DOWNLOAD_THREADS", "8")))

# MOD13 NDVI uses -3000 as fill and pixel reliability 255 as fill.
NDVI_FILL = -3000
QA_FILL = 255
NDVI_DST_NODATA = -32768
QA_DST_NODATA = 255


def _cache_root() -> Path:
    return cache.cache_root() / PRODUCT.lower()


def _aoi_key(bounds: tuple[float, float, float, float], resolution_deg: float) -> str:
    """Stable key for one AOI + resolution on one product."""
    text = "|".join(f"{value:.6f}" for value in bounds) + f"|{resolution_deg:.6f}|{PRODUCT}"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _raster_paths(aoi_hash: str, granule_stem: str) -> tuple[Path, Path]:
    base = _cache_root() / "aoi" / aoi_hash
    return base / f"{granule_stem}_ndvi.tif", base / f"{granule_stem}_qa.tif"


def _waves(
    dates: list[str],
    grouped: dict[str, list[Any]],
    max_granules: int,
) -> list[list[str]]:
    """Split composite dates into wave boundaries that never split a composite."""
    waves: list[list[str]] = []
    wave: list[str] = []
    granules = 0
    for composite_date in dates:
        wave.append(composite_date)
        granules += len(grouped[composite_date])
        if granules >= max_granules:
            waves.append(wave)
            wave, granules = [], 0
    if wave:
        waves.append(wave)
    return waves


def _granule_rasters(
    hdf_path: Path,
    granule_stem: str,
    aoi_hash: str,
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[Path, Path]:
    """Return cached NDVI/QA rasters for one granule, warping them if needed."""
    ndvi_tif, qa_tif = _raster_paths(aoi_hash, granule_stem)
    if ndvi_tif.is_file() and qa_tif.is_file():
        cache.touch(ndvi_tif)
        cache.touch(qa_tif)
        return ndvi_tif, qa_tif

    with cache.atomic_output(ndvi_tif) as temporary_ndvi, cache.atomic_output(qa_tif) as temporary_qa:
        warp_subdataset_to_aoi(
            hdf_path,
            str(CONFIG["ndvi"]),
            temporary_ndvi,
            grid_name=str(CONFIG["grid"]),
            bounds=bounds,
            width=width,
            height=height,
            resampling="bilinear",
            src_nodata=NDVI_FILL,
            dst_nodata=NDVI_DST_NODATA,
            output_type="Int16",
        )
        warp_subdataset_to_aoi(
            hdf_path,
            str(CONFIG["qa"]),
            temporary_qa,
            grid_name=str(CONFIG["grid"]),
            bounds=bounds,
            width=width,
            height=height,
            resampling="near",
            src_nodata=QA_FILL,
            dst_nodata=QA_DST_NODATA,
            output_type="Byte",
        )
    return ndvi_tif, qa_tif


def _prepare_wave(
    wave_dates: list[str],
    grouped: dict[str, list[Any]],
    raw_dir: Path,
    aoi_hash: str,
    on_download_progress: Callable[[float, int], None] | None = None,
) -> tuple[list[tuple[str, str, Path | None]], list[str]]:
    """Ensure every granule in the wave is available locally.

    Returns ``(plan, warnings)`` where each plan entry is
    ``(composite_date, granule_stem, hdf_path_or_None)``. ``None`` means the AOI
    raster is already cached and the 50-200 MB HDF is not needed at all.
    """
    warnings: list[str] = []
    plan: list[tuple[str, str, Path | None]] = []
    missing: list[tuple[str, Any]] = []
    reused = 0

    for composite_date in wave_dates:
        for granule in grouped[composite_date]:
            filename = granule_filename(granule)
            if not filename:
                warnings.append(f"{composite_date}: granule has no downloadable file.")
                continue
            stem = Path(filename).stem
            ndvi_tif, qa_tif = _raster_paths(aoi_hash, stem)
            raw_path = raw_dir / filename
            if ndvi_tif.is_file() and qa_tif.is_file():
                plan.append((composite_date, stem, None))
                reused += 1
            elif raw_path.is_file():
                plan.append((composite_date, stem, raw_path))
                reused += 1
            else:
                missing.append((composite_date, granule))

    label = wave_dates[0] if len(wave_dates) == 1 else f"{wave_dates[0]}..{wave_dates[-1]} ({len(wave_dates)} composites)"
    if reused:
        log(f"variables: ndvi: already cached {reused} granule(s) for {label}")

    if missing:
        paths, _, _ = download_granules(
            [granule for _, granule in missing],
            raw_dir,
            threads=DOWNLOAD_THREADS,
            label=f"for {label}",
            on_progress=on_download_progress,
        )
        for (composite_date, granule), path in zip(missing, paths):
            if path.is_file():
                plan.append((composite_date, Path(path).stem, path))
            else:
                warnings.append(f"{composite_date}: {Path(path).name} could not be downloaded.")

    return plan, warnings


def _warp_plan(
    plan: list[tuple[str, str, Path | None]],
    aoi_hash: str,
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[dict[str, list[tuple[Path, Path]]], list[str]]:
    """Warp every planned granule, in parallel, and group results by composite."""
    by_date: dict[str, list[tuple[Path, Path]]] = {}
    warnings: list[str] = []
    pending: list[tuple[str, str, Path]] = []

    for composite_date, stem, hdf_path in plan:
        if hdf_path is None:
            by_date.setdefault(composite_date, []).append(_raster_paths(aoi_hash, stem))
        else:
            pending.append((composite_date, stem, hdf_path))

    if not pending:
        return by_date, warnings

    workers = min(WARP_WORKERS, len(pending))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ndvi-warp") as pool:
        futures = {
            pool.submit(_granule_rasters, hdf_path, stem, aoi_hash, bounds, width, height): (
                composite_date,
                stem,
            )
            for composite_date, stem, hdf_path in pending
        }
        for future in as_completed(futures):
            composite_date, stem = futures[future]
            try:
                by_date.setdefault(composite_date, []).append(future.result())
            except Exception as exc:
                log(f"variables: ndvi: could not process {stem}: {type(exc).__name__}: {exc}")
                warnings.append(f"{composite_date}: {stem} could not be processed ({type(exc).__name__}: {exc}).")

    return by_date, warnings


def _composite_mean(
    raster_pairs: Iterable[tuple[Path, Path]],
    width: int,
    height: int,
) -> tuple[float | None, int, int, list[str], np.ndarray | None]:
    """Mean NDVI over one composite, merging every tile that covers the AOI."""
    merged = np.full((height, width), np.nan, dtype=np.float32)
    granules: list[str] = []

    for ndvi_tif, qa_tif in raster_pairs:
        granules.append(ndvi_tif.name.removesuffix("_ndvi.tif"))
        try:
            ndvi, _ = read_raster(ndvi_tif)
            qa, _ = read_raster(qa_tif)
        except Exception as exc:
            log(f"variables: ndvi: could not read {ndvi_tif.name}: {type(exc).__name__}: {exc}")
            continue

        # Pixel reliability: 0 = good, 1 = marginal, 2 = snow/ice, 3 = cloudy.
        # The raster is already on the AOI grid, so no reprojection is needed.
        valid = (ndvi >= -2000) & (ndvi <= 10000) & (qa <= 1)
        if not np.any(valid):
            continue

        tile = np.full(ndvi.shape, np.nan, dtype=np.float32)
        tile[valid] = ndvi[valid].astype(np.float32) * 0.0001
        merged = np.where(np.isfinite(tile), tile, merged)

    finite = merged[np.isfinite(merged)]
    total = int(merged.size)
    if finite.size == 0:
        return None, 0, total, granules, None
    return float(np.mean(finite)), int(finite.size), total, granules, merged


def collect(request: CollectionRequest) -> VariableSeries:
    bounds = (request.aoi["west"], request.aoi["south"], request.aoi["east"], request.aoi["north"])
    resolution = float(CONFIG["resolution_deg"])
    width, height = destination_grid_size(bounds, resolution)
    aoi_hash = _aoi_key(bounds, resolution)

    cache_root = _cache_root()
    raw_dir = cache_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    budget = cache.cache_budget_bytes()

    composite_records: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    raster_sum: np.ndarray | None = None
    raster_count: np.ndarray | None = None

    # ``search_data`` works anonymously, but ``download`` needs an initialized
    # store. Authenticate once per job rather than once per chunk.
    ensure_earthaccess_auth()

    for chunk_start, chunk_end in date_chunks(request.start_date, request.end_date, 366):
        request.check_cancel()
        log(f"variables: ndvi: searching {PRODUCT} {chunk_start} -> {chunk_end}")
        granules = earthaccess.search_data(
            short_name=PRODUCT,
            version="061",
            count=-1,
            temporal=(chunk_start, chunk_end),
            bounding_box=bounds,
        )
        grouped: dict[str, list[Any]] = {}
        for granule in granules:
            composite_date = granule_date(granule) or granule_temporal_start(granule)
            if composite_date:
                grouped.setdefault(composite_date, []).append(granule)

        dates = sorted(grouped)
        if not dates:
            warnings.append(f"No {PRODUCT} composites were found for {chunk_start} -> {chunk_end}.")
            continue

        total = len(dates)
        done = 0
        for wave_dates in _waves(dates, grouped, GRANULE_BATCH):
            request.check_cancel()
            wave_label = (
                wave_dates[0]
                if len(wave_dates) == 1
                else f"{wave_dates[0]}..{wave_dates[-1]}"
            )

            def report_download(
                elapsed: float,
                written: int,
                _done: int = done,
                _total: int = total,
                _label: str = wave_label,
            ) -> None:
                request.report(
                    _done,
                    _total,
                    detail=(
                        f"downloading composites {_label}… "
                        f"{elapsed:.0f}s elapsed, {written / 1024 / 1024:.1f} MB"
                    ),
                )

            report_download(0.0, 0)
            log(
                f"variables: ndvi: processing {len(wave_dates)} composite(s) "
                f"{wave_dates[0]}..{wave_dates[-1]} "
                f"({sum(len(grouped[d]) for d in wave_dates)} granule(s))"
            )

            plan, wave_warnings = _prepare_wave(
                wave_dates,
                grouped,
                raw_dir,
                aoi_hash,
                on_download_progress=report_download,
            )
            warnings.extend(wave_warnings)
            rasters_by_date, warp_warnings = _warp_plan(plan, aoi_hash, bounds, width, height)
            warnings.extend(warp_warnings)

            for composite_date in wave_dates:
                request.check_cancel()
                raster_pairs = rasters_by_date.get(composite_date, [])
                value, valid, total_pixels, used, merged = _composite_mean(raster_pairs, width, height)
                composite_records[composite_date] = {
                    "value": value,
                    "valid_pixels": valid,
                    "total_pixels": total_pixels,
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

                done += 1
                request.report(done, total, detail=f"composite {composite_date} ({done}/{total})")

            cache.trim_cache(cache_root, budget)

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
                    missing_reason="No MODIS composite is available on or before this date.",
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
                        missing_reason=f"The most recent MODIS composite ({source}) is outside its 16-day window.",
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
        raster_bounds=bounds,
    )
