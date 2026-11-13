"""Background execution of the MOD11A1 trend-analysis job.

The HTTP route validates the request and enqueues it. Everything that talks to
Earthdata, GDAL, or matplotlib happens here, inside the RQ worker, so the API
request returns immediately.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from app.jobs.errors import JobError
from app.jobs.store import JOBS_DIR
from app.services.modis_timeseries import TimeSeriesConfig, collect_mod11a1_timeseries
from app.services.statistical_analysis import (
    generate_trend_visualizations,
    linear_regression_analysis,
    mann_kendall_analysis,
    modified_mann_kendall_analysis,
    paired_period_t_test,
    seasonal_mann_kendall_analysis,
    validate_time_series,
)
from app.utils.logging import log

ProgressCallback = Callable[[str, str, int, int], None]


def _as_stats_records(time_series_payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = time_series_payload.get("records", [])
    if not records:
        return []

    transformed: list[dict[str, Any]] = []
    for record in records:
        if record.get("missing_observation") or record.get("mean_temperature_celsius") is None:
            mean_lst = None
            missing = True
        else:
            mean_lst = float(record["mean_temperature_celsius"])
            missing = False

        transformed.append(
            {
                "observation_date": record.get("observation_date"),
                "mean_lst": mean_lst,
                "valid_pixel_count": int(record.get("valid_pixels", record.get("valid_pixel_count", 0)) or 0),
                "total_pixel_count": int(record.get("total_pixels", record.get("total_pixel_count", 0)) or 0),
                "valid_data_percentage": float(record.get("valid_data_percentage", 0.0) or 0.0),
                "missing": missing,
            }
        )
    return transformed


def _calculate_t_test(stats_records: list[dict[str, Any]]) -> dict[str, Any]:
    sorted_records = sorted(stats_records, key=lambda item: str(item["observation_date"]))
    if len(sorted_records) < 4:
        return {
            "status": "insufficient_data",
            "reason": "At least four observations are required to compare early and late periods.",
        }

    split_index = max(2, len(sorted_records) // 2)
    first_date = str(sorted_records[0]["observation_date"])
    mid_date = str(sorted_records[split_index - 1]["observation_date"])
    second_date = str(sorted_records[split_index]["observation_date"])
    last_date = str(sorted_records[-1]["observation_date"])

    try:
        return paired_period_t_test(
            stats_records,
            (first_date, mid_date),
            (second_date, last_date),
        )
    except ValueError as exc:
        return {"status": "insufficient_data", "reason": str(exc)}


def execute_trend_analysis(
    payload: dict[str, Any],
    job_id: str,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Collect the MOD11A1 series and run the requested trend methods."""
    started_at = time.perf_counter()
    product = str(payload["product"]).upper()
    aoi = payload["aoi"]
    start_date = payload["start_date"]
    end_date = payload["end_date"]
    temporal_resolution = str(payload["temporal_resolution"]).lower()
    requested_methods = [str(method).lower() for method in payload["analysis_methods"]]

    log(
        f"trend: job {job_id} starting product={product} aoi={aoi} "
        f"start={start_date} end={end_date} resolution={temporal_resolution} methods={requested_methods}"
    )

    output_dir = JOBS_DIR / job_id / "trend"
    config = TimeSeriesConfig(
        aoi=aoi,
        start_date=start_date,
        end_date=end_date,
        product_short_name=product,
        temporal_aggregation=temporal_resolution,
        output_dir=output_dir / "time_series",
        region_name=f"trend_{job_id}",
    )

    if progress is not None:
        progress("collecting", product, 0, 1)
    collection_started_at = time.perf_counter()
    try:
        time_series_payload = collect_mod11a1_timeseries(config)
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
    if progress is not None:
        progress("collected", product, 1, 1)

    log(
        f"trend: job {job_id} time series collected in "
        f"{time.perf_counter() - collection_started_at:.1f}s "
        f"({len(time_series_payload.get('records', []))} records, "
        f"files={time_series_payload.get('output_files')})"
    )

    records = _as_stats_records(time_series_payload)
    if not records:
        raise JobError(404, "No valid time series records were returned for the requested AOI and date range.")

    log(f"trend: job {job_id} validating time series ({len(records)} records)")
    try:
        validation = validate_time_series(records)
    except ValueError as exc:
        reasons = sorted(
            {
                str(record.get("missing_reason") or "").strip()
                for record in time_series_payload.get("records", [])
                if record.get("missing_reason")
            }
        )
        log(f"trend: job {job_id} validation failed: {exc} reasons={reasons[:3]}")
        if any("gdal_translate was not found" in reason for reason in reasons):
            raise JobError(
                503,
                (
                    "Processing environment is not configured: "
                    "gdal_translate with HDF4 support was not found. "
                    "Install GDAL (OSGeo4W or conda-forge) and put it on PATH, "
                    "or set the GDAL_TRANSLATE environment variable to its full path."
                ),
            ) from exc

        detail = f"No valid observations: {exc}"
        if reasons:
            detail += f" Reasons: {'; '.join(reasons[:3])}"
        raise JobError(404, detail) from exc

    data_quality = {
        "observation_count": int(validation["summary"].get("observation_count", 0)),
        "valid_observation_count": int(validation["summary"].get("valid_observation_count", 0)),
        "missing_observation_count": int(validation["summary"].get("missing_observation_count", 0)),
        "missing_observation_percentage": float(validation["summary"].get("missing_observation_percentage", 0.0)),
        "data_coverage_percentage": float(validation["summary"].get("data_coverage_percentage", 0.0)),
        "date_range_start": validation["summary"].get("date_range_start"),
        "date_range_end": validation["summary"].get("date_range_end"),
        "warnings": validation["summary"].get("warnings", []),
    }

    if len(validation["dataframe"]) < 3:
        raise JobError(404, "Insufficient observations for statistical inference.")

    analysis_results: dict[str, Any] = {}
    detected_warnings: list[str] = list(data_quality["warnings"])

    for index, method in enumerate(requested_methods, start=1):
        method_started_at = time.perf_counter()
        log(f"trend: job {job_id} running analysis method '{method}'")
        if method == "linear_regression":
            result = linear_regression_analysis(records)
            analysis_results[method] = result
            detected_warnings.extend(result.get("warnings", []))
        elif method == "mann_kendall":
            result = mann_kendall_analysis(records)
            analysis_results[method] = result
            detected_warnings.extend(result.get("warnings", []))
        elif method == "modified_mann_kendall":
            result = modified_mann_kendall_analysis(records)
            analysis_results[method] = result
            if result.get("status") in {"insufficient_data", "not_applicable"}:
                detected_warnings.append(result.get("reason", "Modified Mann-Kendall was not calculated."))
        elif method == "seasonal_mann_kendall":
            if temporal_resolution != "monthly":
                analysis_results[method] = {
                    "status": "not_supported",
                    "reason": "Seasonal Mann-Kendall is implemented for monthly observations only.",
                }
            else:
                result = seasonal_mann_kendall_analysis(records)
                analysis_results[method] = result
                if result.get("status") in {"insufficient_data", "not_supported"}:
                    detected_warnings.append(
                        result.get("insufficient_data_reason", result.get("reason", "Seasonal Mann-Kendall was not calculated."))
                    )
        else:
            result = _calculate_t_test(records)
            analysis_results[method] = result
            detected_warnings.extend(result.get("warnings", []))
        log(
            f"trend: job {job_id} analysis method '{method}' finished in "
            f"{time.perf_counter() - method_started_at:.2f}s "
            f"status={analysis_results.get(method, {}).get('status')}"
        )
        if progress is not None:
            progress("analyzing", method, index, len(requested_methods))

    visualizations: dict[str, Any] = {}
    if progress is not None:
        progress("rendering", "visualizations", 0, 1)
    try:
        raster_bounds = time_series_payload.get("raster_bounds")
        visualizations = generate_trend_visualizations(
            records,
            analysis_results,
            aoi,
            product,
            mean_raster=time_series_payload.get("mean_lst_raster"),
            raster_bounds=tuple(raster_bounds) if raster_bounds else None,
        )
        log(f"trend: job {job_id} generated {len(visualizations)} visualization(s): {sorted(visualizations)}")
    except Exception as exc:  # pragma: no cover - visualization is non-critical
        log(f"trend: job {job_id} visualization generation failed: {type(exc).__name__}: {exc}")
    if progress is not None:
        progress("rendering", "visualizations", 1, 1)

    response = {
        "success": True,
        "job_id": job_id,
        "metadata": {
            "mode": "trend_analysis",
            "product": product,
            "temporal_resolution": temporal_resolution,
            "start_date": start_date,
            "end_date": end_date,
            "aoi": aoi,
            "analysis_methods": requested_methods,
            "output_files": time_series_payload.get("output_files"),
        },
        "data_quality": data_quality,
        "analysis_results": analysis_results,
        "visualizations": visualizations,
        "warnings": sorted(set(detected_warnings)),
    }
    log(
        f"trend: job {job_id} completed in {time.perf_counter() - started_at:.1f}s "
        f"(valid_observations={data_quality['valid_observation_count']}, methods={list(analysis_results)})"
    )
    return response
