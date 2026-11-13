"""Background execution of the environmental-variable trend job.

The HTTP route validates the request and enqueues it. Everything that talks to
Earthdata, GDAL, or matplotlib happens here, inside the RQ worker, so the API
request returns immediately.

All supported variables are collected through the same registry used by the
comparison endpoints, which keeps the record schema and chunking behavior
consistent across every analysis mode.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from app.jobs.errors import JobError, raise_if_canceled
from app.jobs.store import JOBS_DIR
from app.services.analysis_engine import run_series_analysis, to_stats_records
from app.services.statistical_analysis import validate_time_series
from app.services.variables import get_variable_spec, resolve_variable_id
from app.services.variables.base import VariableSpec, build_record
from app.services.variables.collection import collect_chunked_variable
from app.utils.logging import log

ProgressCallback = Callable[[str, str, int, int], None]


def _period_key(observation_date: str, resolution: str) -> str:
    if resolution == "monthly":
        return observation_date[:7]
    return observation_date[:4]


def _period_label(key: str, resolution: str) -> str:
    return f"{key}-01" if resolution == "monthly" else f"{key}-01-01"


def _aggregate_records(
    records: list[dict[str, Any]],
    spec: VariableSpec,
    resolution: str,
) -> list[dict[str, Any]]:
    """Aggregate daily records to monthly/yearly means.

    Precipitation stays a mean daily rate (mm/day) rather than a monthly total,
    so every variable keeps the same statistic across resolutions.
    """
    if resolution == "daily":
        return records

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        day = str(record.get("observation_date") or "")
        if not day:
            continue
        grouped.setdefault(_period_key(day, resolution), []).append(record)

    aggregated: list[dict[str, Any]] = []
    for key in sorted(grouped):
        batch = grouped[key]
        values = [
            float(record["value"])
            for record in batch
            if record.get("value") is not None and not record.get("missing")
        ]
        valid_pixels = sum(int(record.get("valid_pixel_count") or 0) for record in batch)
        total_pixels = sum(int(record.get("total_pixel_count") or 0) for record in batch)
        value = sum(values) / len(values) if values else None
        aggregated.append(
            build_record(
                _period_label(key, resolution),
                value,
                spec,
                valid_pixels=valid_pixels,
                total_pixels=total_pixels,
                missing_reason=None if values else "No valid observations in this period.",
                coverage_status="aggregated" if values else "missing",
            )
        )
    return aggregated


def execute_trend_analysis(
    payload: dict[str, Any],
    job_id: str,
    progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Collect one variable's series and run the requested trend methods."""
    started_at = time.perf_counter()
    try:
        variable_id = resolve_variable_id(payload["variable"])
    except ValueError as exc:
        raise JobError(400, str(exc)) from exc
    spec = get_variable_spec(variable_id)
    aoi = payload["aoi"]
    start_date = payload["start_date"]
    end_date = payload["end_date"]
    temporal_resolution = str(payload["temporal_resolution"]).lower()
    spatial_strategy = str(payload.get("spatial_strategy") or "coarse").lower()
    sampling = dict(payload.get("sampling") or {})
    sampling_mode = str(sampling.get("mode") or "full_period").lower()
    samples_per_month = int(sampling.get("samples_per_month") or 1)
    sampling_seed = sampling.get("seed")
    sampling_missing = str(sampling.get("missing") or "keep").lower()
    requested_methods = [str(method).lower() for method in payload["analysis_methods"]]

    log(
        f"trend: job {job_id} starting variable={variable_id} dataset={spec.dataset} aoi={aoi} "
        f"start={start_date} end={end_date} resolution={temporal_resolution} "
        f"strategy={spatial_strategy} sampling={sampling_mode} methods={requested_methods}"
    )

    output_dir = JOBS_DIR / job_id / "trend" / variable_id
    collection_started_at = time.perf_counter()
    try:
        series = collect_chunked_variable(
            variable_id,
            aoi,
            start_date,
            end_date,
            output_dir,
            progress=progress,
            progress_label=variable_id,
            should_cancel=should_cancel,
            temporal_resolution=temporal_resolution,
            spatial_strategy=spatial_strategy,
            sampling_mode=sampling_mode,
            samples_per_month=samples_per_month,
            sampling_seed=sampling_seed,
            sampling_missing=sampling_missing,
        )
    except TimeoutError as exc:
        log(f"trend: job {job_id} collection timed out after {time.perf_counter() - collection_started_at:.1f}s: {exc}")
        raise JobError(504, f"Processing timeout: {exc}") from exc
    except RuntimeError as exc:
        log(f"trend: job {job_id} processing environment is not configured: {exc}")
        raise JobError(503, f"Processing environment is not configured: {exc}") from exc
    except (ConnectionError, OSError) as exc:
        log(f"trend: job {job_id} NASA Earthdata connection failure: {exc}")
        raise JobError(503, f"NASA Earthdata connection failure: {exc}") from exc
    except ValueError as exc:
        log(f"trend: job {job_id} no NASA data available: {exc}")
        raise JobError(404, f"No NASA data available: {exc}") from exc

    log(
        f"trend: job {job_id} collected {len(series.records)} {series.frequency} record(s) in "
        f"{time.perf_counter() - collection_started_at:.1f}s "
        f"(strategy={series.collection_strategy})"
    )

    records = series.records
    if series.frequency != temporal_resolution:
        records = _aggregate_records(records, spec, temporal_resolution)
    stats_records = to_stats_records(records)
    if not stats_records:
        raise JobError(404, "No valid time series records were returned for the requested AOI and date range.")

    log(f"trend: job {job_id} validating {len(stats_records)} {temporal_resolution} record(s)")
    try:
        validation = validate_time_series(stats_records)
    except ValueError as exc:
        reasons = sorted(
            {
                str(record.get("missing_reason") or "").strip()
                for record in records
                if record.get("missing_reason")
            }
        )
        log(f"trend: job {job_id} validation failed: {exc} reasons={reasons[:3]}")
        detail = f"No valid observations: {exc}"
        if reasons:
            detail += f" Reasons: {'; '.join(reasons[:3])}"
        raise JobError(404, detail) from exc

    if len(validation["dataframe"]) < 3:
        raise JobError(404, "Insufficient observations for statistical inference.")

    if progress is not None:
        progress("analyzing", variable_id, 0, len(requested_methods))
    raise_if_canceled(should_cancel)
    analysis = run_series_analysis(
        records,
        requested_methods,
        aoi,
        product=spec.dataset,
        unit=spec.unit,
        label=spec.value_label,
        mean_raster=series.mean_raster,
        raster_bounds=series.raster_bounds,
        include_visualizations=True,
    )
    if progress is not None:
        progress("analyzing", variable_id, len(requested_methods), len(requested_methods))

    if analysis.get("status") == "insufficient_data":
        raise JobError(404, analysis.get("reason", "Insufficient observations for statistical inference."))

    response = {
        "success": True,
        "job_id": job_id,
        "metadata": {
            "mode": "trend_analysis",
            "variable": spec.variable_id,
            "display_name": spec.display_name,
            "product": spec.dataset,
            "dataset": spec.dataset,
            "unit": spec.unit,
            "native_resolution": series.source_resolution or spec.native_resolution,
            "temporal_resolution": temporal_resolution,
            "spatial_strategy": spatial_strategy if temporal_resolution != "daily" else None,
            "collection_strategy": series.collection_strategy,
            "source_products": series.source_products or [spec.dataset],
            "sampling": series.sampling or {"mode": "full_period"},
            "start_date": start_date,
            "end_date": end_date,
            "aoi": aoi,
            "analysis_methods": requested_methods,
        },
        "data_quality": analysis.get("data_quality", {}),
        "analysis_results": analysis.get("analysis_results", {}),
        "visualizations": analysis.get("visualizations", {}),
        "warnings": sorted(set(series.warnings + analysis.get("warnings", []))),
    }
    log(
        f"trend: job {job_id} completed in {time.perf_counter() - started_at:.1f}s "
        f"(variable={variable_id}, valid_observations={response['data_quality'].get('valid_observation_count')}, "
        f"methods={list(response['analysis_results'])})"
    )
    return response
