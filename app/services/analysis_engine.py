"""Shared analysis execution used by every analysis endpoint and background job."""

from __future__ import annotations

from typing import Any, Iterable

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

SUPPORTED_METHODS = frozenset(
    {
        "linear_regression",
        "mann_kendall",
        "modified_mann_kendall",
        "seasonal_mann_kendall",
        "t_test",
    }
)


def to_stats_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate canonical variable records into the statistical-analysis schema."""
    transformed: list[dict[str, Any]] = []
    for record in records:
        value = record.get("value")
        if value is None:
            value = record.get("mean_lst")
        if value is None:
            value = record.get("mean_temperature_celsius")
        missing = bool(record.get("missing")) or value is None
        transformed.append(
            {
                "observation_date": record.get("observation_date"),
                "mean_lst": None if missing else float(value),
                "valid_pixel_count": int(record.get("valid_pixel_count") or record.get("valid_pixels") or 0),
                "total_pixel_count": int(record.get("total_pixel_count") or record.get("total_pixels") or 0),
                "valid_data_percentage": float(record.get("valid_data_percentage") or 0.0),
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
        return paired_period_t_test(stats_records, (first_date, mid_date), (second_date, last_date))
    except ValueError as exc:
        return {"status": "insufficient_data", "reason": str(exc)}


def run_series_analysis(
    records: Iterable[dict[str, Any]],
    methods: Iterable[str],
    aoi: dict[str, float],
    *,
    product: str,
    unit: str,
    label: str,
    mean_raster: Any = None,
    raster_bounds: tuple[float, float, float, float] | None = None,
    include_visualizations: bool = True,
) -> dict[str, Any]:
    """Validate, run requested methods, and render per-method base64 charts."""
    stats_records = to_stats_records(records)
    requested_methods = [str(method).lower() for method in methods]
    unsupported = sorted(set(requested_methods) - SUPPORTED_METHODS)
    if unsupported:
        raise ValueError(f"Unsupported analysis methods: {unsupported}. Supported methods: {sorted(SUPPORTED_METHODS)}")

    if not stats_records:
        raise ValueError("No records were returned for the requested variable and date range.")

    try:
        validation = validate_time_series(stats_records)
    except ValueError as exc:
        return {
            "status": "insufficient_data",
            "reason": str(exc),
            "data_quality": {
                "observation_count": len(stats_records),
                "valid_observation_count": 0,
                "missing_observation_count": len(stats_records),
                "missing_observation_percentage": 100.0,
                "data_coverage_percentage": 0.0,
                "warnings": [str(exc)],
            },
            "analysis_results": {},
            "visualizations": {},
            "warnings": [str(exc)],
        }

    summary = validation["summary"]
    data_quality = {
        "observation_count": int(summary.get("observation_count", 0)),
        "valid_observation_count": int(summary.get("valid_observation_count", 0)),
        "missing_observation_count": int(summary.get("missing_observation_count", 0)),
        "missing_observation_percentage": float(summary.get("missing_observation_percentage", 0.0)),
        "data_coverage_percentage": float(summary.get("data_coverage_percentage", 0.0)),
        "date_range_start": summary.get("date_range_start"),
        "date_range_end": summary.get("date_range_end"),
        "warnings": list(summary.get("warnings", [])),
    }

    analysis_results: dict[str, Any] = {}
    detected_warnings: list[str] = list(data_quality["warnings"])

    for method in requested_methods:
        log(f"analysis: running '{method}' on {len(stats_records)} record(s) for {label}")
        if method == "linear_regression":
            result = linear_regression_analysis(stats_records)
            detected_warnings.extend(result.get("warnings", []))
        elif method == "mann_kendall":
            result = mann_kendall_analysis(stats_records)
            detected_warnings.extend(result.get("warnings", []))
        elif method == "modified_mann_kendall":
            result = modified_mann_kendall_analysis(stats_records)
            if result.get("status") in {"insufficient_data", "not_applicable"}:
                detected_warnings.append(result.get("reason", "Modified Mann-Kendall was not calculated."))
        elif method == "seasonal_mann_kendall":
            result = seasonal_mann_kendall_analysis(stats_records)
            if result.get("status") in {"insufficient_data", "not_supported"}:
                detected_warnings.append(
                    result.get("insufficient_data_reason", result.get("reason", "Seasonal Mann-Kendall was not calculated."))
                )
        else:
            result = _calculate_t_test(stats_records)
            detected_warnings.extend(result.get("warnings", []))
        analysis_results[method] = result

    visualizations: dict[str, Any] = {}
    if include_visualizations:
        visualizations = generate_trend_visualizations(
            stats_records,
            analysis_results,
            aoi,
            product,
            mean_raster=mean_raster,
            raster_bounds=raster_bounds,
            unit=unit,
            label=label,
        )

    return {
        "status": "calculated",
        "data_quality": data_quality,
        "analysis_results": analysis_results,
        "visualizations": visualizations,
        "warnings": sorted(set(detected_warnings)),
    }
