"""Execution layer for multi-variable and multi-region comparison jobs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from app.services.analysis_engine import run_series_analysis
from app.services.comparison_charts import generate_comparison_visualizations
from app.services.variables import collect_variable, get_variable_spec
from app.services.variables.collection import collect_chunked_variable
from app.utils.logging import log

PROJECT_ROOT = Path(__file__).resolve().parents[3]
JOBS_DIR = PROJECT_ROOT / "data" / "jobs"
MAX_DAYS_PER_DAILY_JOB = int(os.environ.get("MAX_DAYS_PER_DAILY_JOB", "1826"))

ProgressCallback = Callable[[str, str, int, int], None]


def _collect_chunked(
    variable_id: str,
    aoi: dict[str, float],
    start_date: str,
    end_date: str,
    output_dir: Path,
    progress: ProgressCallback | None = None,
    progress_label: str | None = None,
) -> tuple[list[dict[str, Any]], list[str], Any, Any]:
    series = collect_chunked_variable(
        variable_id,
        aoi,
        start_date,
        end_date,
        output_dir,
        max_days=MAX_DAYS_PER_DAILY_JOB,
        progress=progress,
        progress_label=progress_label,
        collector=collect_variable,
    )
    return series.records, series.warnings, series.mean_raster, series.raster_bounds


def execute_variable_comparison(
    payload: dict[str, Any],
    job_id: str,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    aoi = payload["aoi"]
    start_date = payload["start_date"]
    end_date = payload["end_date"]
    methods = payload["analysis_methods"]
    include_visualizations = bool(payload.get("include_visualizations", True))

    variables: dict[str, Any] = {}
    series_by_label: dict[str, list[dict[str, Any]]] = {}
    analysis_by_label: dict[str, dict[str, Any]] = {}
    metadata_by_label: dict[str, dict[str, Any]] = {}
    all_warnings: list[str] = []

    for index, variable_id in enumerate(payload["variables"], start=1):
        spec = get_variable_spec(variable_id)
        output_dir = JOBS_DIR / job_id / variable_id
        output_dir.mkdir(parents=True, exist_ok=True)
        if progress is not None:
            progress("variable", variable_id, index - 1, len(payload["variables"]))
        try:
            records, warnings, mean_raster, raster_bounds = _collect_chunked(
                variable_id,
                aoi,
                start_date,
                end_date,
                output_dir,
                progress=progress,
                progress_label=variable_id,
            )
            analysis = run_series_analysis(
                records,
                methods,
                aoi,
                product=spec.dataset,
                unit=spec.unit,
                label=spec.value_label,
                mean_raster=mean_raster,
                raster_bounds=raster_bounds,
                include_visualizations=include_visualizations,
            )
            variables[variable_id] = {
                "metadata": {
                    "variable_id": spec.variable_id,
                    "display_name": spec.display_name,
                    "dataset": spec.dataset,
                    "unit": spec.unit,
                    "native_resolution": spec.native_resolution,
                },
                "series": records,
                "data_quality": analysis.get("data_quality", {}),
                "analysis_results": analysis.get("analysis_results", {}),
                "visualizations": analysis.get("visualizations", {}),
                "status": analysis.get("status", "calculated"),
                "warnings": sorted(set(warnings + analysis.get("warnings", []))),
            }
            series_by_label[spec.display_name] = records
            analysis_by_label[spec.display_name] = analysis
            metadata_by_label[spec.display_name] = {"unit": spec.unit}
            all_warnings.extend(variables[variable_id]["warnings"])
        except Exception as exc:
            log(f"comparison: variable '{variable_id}' failed: {type(exc).__name__}: {exc}")
            variables[variable_id] = {
                "metadata": {
                    "variable_id": spec.variable_id,
                    "display_name": spec.display_name,
                    "dataset": spec.dataset,
                    "unit": spec.unit,
                    "native_resolution": spec.native_resolution,
                },
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "series": [],
                "analysis_results": {},
                "visualizations": {},
                "warnings": [],
            }
            all_warnings.append(f"{spec.display_name}: {exc}")

    comparison_visualizations = {}
    if include_visualizations and series_by_label:
        comparison_visualizations = generate_comparison_visualizations(
            series_by_label, analysis_by_label, metadata_by_label
        )

    return {
        "success": True,
        "job_id": job_id,
        "metadata": {
            "mode": "variable_comparison",
            "start_date": start_date,
            "end_date": end_date,
            "temporal_resolution": payload.get("temporal_resolution", "daily"),
            "aoi": aoi,
            "variables": payload["variables"],
            "analysis_methods": methods,
            "max_days_per_chunk": MAX_DAYS_PER_DAILY_JOB,
        },
        "variables": variables,
        "comparison": {"visualizations": comparison_visualizations},
        "warnings": sorted(set(all_warnings)),
    }


def execute_region_comparison(
    payload: dict[str, Any],
    job_id: str,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    variable_id = payload["variable"]
    spec = get_variable_spec(variable_id)
    start_date = payload["start_date"]
    end_date = payload["end_date"]
    methods = payload["analysis_methods"]
    include_visualizations = bool(payload.get("include_visualizations", True))

    regions: dict[str, Any] = {}
    series_by_label: dict[str, list[dict[str, Any]]] = {}
    analysis_by_label: dict[str, dict[str, Any]] = {}
    metadata_by_label: dict[str, dict[str, Any]] = {}
    all_warnings: list[str] = []

    for index, aoi in enumerate(payload["regions"], start=1):
        label = f"region_{index}"
        output_dir = JOBS_DIR / job_id / variable_id / label
        output_dir.mkdir(parents=True, exist_ok=True)
        if progress is not None:
            progress("region", label, index - 1, len(payload["regions"]))
        try:
            records, warnings, mean_raster, raster_bounds = _collect_chunked(
                variable_id,
                aoi,
                start_date,
                end_date,
                output_dir,
                progress=progress,
                progress_label=label,
            )
            analysis = run_series_analysis(
                records,
                methods,
                aoi,
                product=spec.dataset,
                unit=spec.unit,
                label=spec.value_label,
                mean_raster=mean_raster,
                raster_bounds=raster_bounds,
                include_visualizations=include_visualizations,
            )
            regions[label] = {
                "aoi": aoi,
                "metadata": {
                    "variable_id": spec.variable_id,
                    "dataset": spec.dataset,
                    "unit": spec.unit,
                    "native_resolution": spec.native_resolution,
                },
                "series": records,
                "data_quality": analysis.get("data_quality", {}),
                "analysis_results": analysis.get("analysis_results", {}),
                "visualizations": analysis.get("visualizations", {}),
                "status": analysis.get("status", "calculated"),
                "warnings": sorted(set(warnings + analysis.get("warnings", []))),
            }
            series_by_label[label] = records
            analysis_by_label[label] = analysis
            metadata_by_label[label] = {"unit": spec.unit}
            all_warnings.extend(regions[label]["warnings"])
        except Exception as exc:
            log(f"comparison: region '{label}' failed: {type(exc).__name__}: {exc}")
            regions[label] = {
                "aoi": aoi,
                "metadata": {"variable_id": spec.variable_id, "dataset": spec.dataset, "unit": spec.unit},
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "series": [],
                "analysis_results": {},
                "visualizations": {},
                "warnings": [],
            }
            all_warnings.append(f"{label}: {exc}")

    comparison_visualizations = {}
    if include_visualizations and series_by_label:
        comparison_visualizations = generate_comparison_visualizations(
            series_by_label, analysis_by_label, metadata_by_label
        )

    return {
        "success": True,
        "job_id": job_id,
        "metadata": {
            "mode": "region_comparison",
            "variable": variable_id,
            "dataset": spec.dataset,
            "start_date": start_date,
            "end_date": end_date,
            "temporal_resolution": payload.get("temporal_resolution", "daily"),
            "analysis_methods": methods,
            "max_days_per_chunk": MAX_DAYS_PER_DAILY_JOB,
        },
        "regions": regions,
        "comparison": {"visualizations": comparison_visualizations},
        "warnings": sorted(set(all_warnings)),
    }
