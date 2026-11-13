"""Execution layer for multi-variable and multi-region comparison jobs.

A comparison is built from independent *parts*: one variable in a variable
comparison, one region in a region comparison. Each part is collected and
analysed on its own, so the parts can run in parallel on different RQ workers.
:func:`assemble_variable_comparison` and :func:`assemble_region_comparison` put
the finished parts back together and render the comparison charts.

The sequential executors below use the same builders and assemblers, which keeps
the inline (``ANALYSIS_QUEUE_MODE=inline``) and single-part paths byte-identical
to the distributed path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from app.jobs.errors import JobCanceled
from app.services.analysis_engine import run_series_analysis
from app.services.comparison_charts import generate_comparison_visualizations
from app.services.variables import collect_resolved_variable, get_variable_spec
from app.services.variables.collection import collect_chunked_variable
from app.utils.logging import log

PROJECT_ROOT = Path(__file__).resolve().parents[3]
JOBS_DIR = PROJECT_ROOT / "data" / "jobs"
MAX_DAYS_PER_DAILY_JOB = int(os.environ.get("MAX_DAYS_PER_DAILY_JOB", "1826"))

ProgressCallback = Callable[[str, str, int, int], None]


def _collection_options(payload: dict[str, Any]) -> dict[str, Any]:
    sampling = dict(payload.get("sampling") or {})
    return {
        "temporal_resolution": str(payload.get("temporal_resolution") or "daily").lower(),
        "spatial_strategy": str(payload.get("spatial_strategy") or "coarse").lower(),
        "sampling_mode": str(sampling.get("mode") or "all").lower(),
        "samples_per_month": int(sampling.get("samples_per_month") or 1),
        "sampling_seed": sampling.get("seed"),
        "sampling_missing": str(sampling.get("missing") or "keep").lower(),
    }


def _collect_chunked(
    variable_id: str,
    aoi: dict[str, float],
    start_date: str,
    end_date: str,
    output_dir: Path,
    progress: ProgressCallback | None = None,
    progress_label: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
    options: dict[str, Any] | None = None,
):
    return collect_chunked_variable(
        variable_id,
        aoi,
        start_date,
        end_date,
        output_dir,
        max_days=MAX_DAYS_PER_DAILY_JOB,
        progress=progress,
        progress_label=progress_label,
        should_cancel=should_cancel,
        collector=collect_resolved_variable,
        **(options or {}),
    )


def _variable_metadata(spec: Any) -> dict[str, Any]:
    return {
        "variable_id": spec.variable_id,
        "display_name": spec.display_name,
        "dataset": spec.dataset,
        "unit": spec.unit,
        "native_resolution": spec.native_resolution,
    }


def variable_part(
    payload: dict[str, Any],
    variable_id: str,
    job_id: str,
    progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
    position: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Collect and analyse one variable; return a self-contained part.

    A part is JSON-serialisable so a worker can persist it and another worker
    can assemble the comparison without sharing memory.
    """
    spec = get_variable_spec(variable_id)
    include_visualizations = bool(payload.get("include_visualizations", True))
    output_dir = JOBS_DIR / job_id / variable_id
    output_dir.mkdir(parents=True, exist_ok=True)

    if progress is not None and position is not None:
        progress("variable", variable_id, position[0] - 1, position[1])

    try:
        series = _collect_chunked(
            variable_id,
            payload["aoi"],
            payload["start_date"],
            payload["end_date"],
            output_dir,
            progress=progress,
            progress_label=variable_id,
            should_cancel=should_cancel,
            options=_collection_options(payload),
        )
        records = series.records
        warnings = series.warnings
        analysis = run_series_analysis(
            records,
            payload["analysis_methods"],
            payload["aoi"],
            product=spec.dataset,
            unit=spec.unit,
            label=spec.value_label,
            mean_raster=series.mean_raster,
            raster_bounds=series.raster_bounds,
            include_visualizations=include_visualizations,
        )
        entry = {
            "metadata": {
                **_variable_metadata(spec),
                "native_resolution": series.source_resolution or spec.native_resolution,
            },
            "series": records,
            "data_quality": analysis.get("data_quality", {}),
            "analysis_results": analysis.get("analysis_results", {}),
            "visualizations": analysis.get("visualizations", {}),
            "status": analysis.get("status", "calculated"),
            "warnings": sorted(set(warnings + analysis.get("warnings", []))),
        }
        return {
            "key": variable_id,
            "label": spec.display_name,
            "unit": spec.unit,
            "status": entry["status"],
            "entry": entry,
            "series": records,
            "analysis": analysis,
            "warnings": entry["warnings"],
            "collection_strategy": series.collection_strategy,
            "source_products": series.source_products or [spec.dataset],
            "source_resolution": series.source_resolution or spec.native_resolution,
            "sampling": series.sampling or {"mode": "all"},
        }
    except JobCanceled:
        raise
    except Exception as exc:
        log(f"comparison: variable '{variable_id}' failed: {type(exc).__name__}: {exc}")
        entry = {
            "metadata": _variable_metadata(spec),
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "series": [],
            "analysis_results": {},
            "visualizations": {},
            "warnings": [],
        }
        return {
            "key": variable_id,
            "label": spec.display_name,
            "unit": spec.unit,
            "status": "failed",
            "entry": entry,
            "series": [],
            "analysis": {},
            "warnings": [f"{spec.display_name}: {exc}"],
            "collection_strategy": "failed",
            "source_products": [],
            "source_resolution": None,
            "sampling": {"mode": "all"},
        }


def region_part(
    payload: dict[str, Any],
    aoi: dict[str, float],
    label: str,
    job_id: str,
    progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Collect and analyse one region of a region comparison."""
    variable_id = payload["variable"]
    spec = get_variable_spec(variable_id)
    include_visualizations = bool(payload.get("include_visualizations", True))
    output_dir = JOBS_DIR / job_id / variable_id / label
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        series = _collect_chunked(
            variable_id,
            aoi,
            payload["start_date"],
            payload["end_date"],
            output_dir,
            progress=progress,
            progress_label=label,
            should_cancel=should_cancel,
            options=_collection_options(payload),
        )
        records = series.records
        warnings = series.warnings
        analysis = run_series_analysis(
            records,
            payload["analysis_methods"],
            aoi,
            product=spec.dataset,
            unit=spec.unit,
            label=spec.value_label,
            mean_raster=series.mean_raster,
            raster_bounds=series.raster_bounds,
            include_visualizations=include_visualizations,
        )
        entry = {
            "aoi": aoi,
            "metadata": {
                "variable_id": spec.variable_id,
                "dataset": spec.dataset,
                "unit": spec.unit,
                "native_resolution": series.source_resolution or spec.native_resolution,
            },
            "series": records,
            "data_quality": analysis.get("data_quality", {}),
            "analysis_results": analysis.get("analysis_results", {}),
            "visualizations": analysis.get("visualizations", {}),
            "status": analysis.get("status", "calculated"),
            "warnings": sorted(set(warnings + analysis.get("warnings", []))),
        }
        return {
            "key": label,
            "label": label,
            "unit": spec.unit,
            "status": entry["status"],
            "entry": entry,
            "series": records,
            "analysis": analysis,
            "warnings": entry["warnings"],
            "collection_strategy": series.collection_strategy,
            "source_products": series.source_products or [spec.dataset],
            "source_resolution": series.source_resolution or spec.native_resolution,
            "sampling": series.sampling or {"mode": "all"},
        }
    except JobCanceled:
        raise
    except Exception as exc:
        log(f"comparison: region '{label}' failed: {type(exc).__name__}: {exc}")
        entry = {
            "aoi": aoi,
            "metadata": {"variable_id": spec.variable_id, "dataset": spec.dataset, "unit": spec.unit},
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "series": [],
            "analysis_results": {},
            "visualizations": {},
            "warnings": [],
        }
        return {
            "key": label,
            "label": label,
            "unit": spec.unit,
            "status": "failed",
            "entry": entry,
            "series": [],
            "analysis": {},
            "warnings": [f"{label}: {exc}"],
            "collection_strategy": "failed",
            "source_products": [],
            "source_resolution": None,
            "sampling": {"mode": "all"},
        }


def failed_part(key: str, label: str, exc: BaseException, unit: str = "") -> dict[str, Any]:
    """Build an ``entry`` for a part that died outside its own error handling."""
    error = f"{type(exc).__name__}: {exc}"
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "status": "failed",
        "entry": {
            "metadata": {"unit": unit},
            "status": "failed",
            "error": error,
            "series": [],
            "analysis_results": {},
            "visualizations": {},
            "warnings": [],
        },
        "series": [],
        "analysis": {},
        "warnings": [f"{label}: {exc}"],
        "collection_strategy": "failed",
        "source_products": [],
        "source_resolution": None,
        "sampling": {"mode": "all"},
    }


def _chart_inputs(parts: list[dict[str, Any]]) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    series_by_label: dict[str, list[dict[str, Any]]] = {}
    analysis_by_label: dict[str, dict[str, Any]] = {}
    metadata_by_label: dict[str, dict[str, Any]] = {}
    for part in parts:
        if part.get("status") == "failed":
            continue
        series_by_label[part["label"]] = part["series"]
        analysis_by_label[part["label"]] = part["analysis"]
        metadata_by_label[part["label"]] = {"unit": part["unit"]}
    return series_by_label, analysis_by_label, metadata_by_label


def _comparison_visualizations(payload: dict[str, Any], parts: list[dict[str, Any]]) -> dict[str, Any]:
    if not payload.get("include_visualizations", True):
        return {}
    series_by_label, analysis_by_label, metadata_by_label = _chart_inputs(parts)
    if not series_by_label:
        return {}
    return generate_comparison_visualizations(series_by_label, analysis_by_label, metadata_by_label)


def _all_warnings(parts: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for part in parts:
        warnings.extend(part.get("warnings", []))
    return sorted(set(warnings))


def _collection_metadata(payload: dict[str, Any], parts: list[dict[str, Any]]) -> dict[str, Any]:
    resolution = payload.get("temporal_resolution", "daily")
    first = next((part for part in parts if part.get("status") != "failed"), {})
    return {
        "temporal_resolution": resolution,
        "spatial_strategy": payload.get("spatial_strategy", "coarse") if resolution != "daily" else None,
        "collection_strategy": first.get("collection_strategy"),
        "source_products": first.get("source_products", []),
        "native_resolution": first.get("source_resolution"),
        "sampling": first.get("sampling", {"mode": "all"}),
    }


def assemble_variable_comparison(
    payload: dict[str, Any], job_id: str, parts: list[dict[str, Any]]
) -> dict[str, Any]:
    variables = {part["key"]: part["entry"] for part in parts}
    return {
        "success": True,
        "job_id": job_id,
        "metadata": {
            "mode": "variable_comparison",
            "start_date": payload["start_date"],
            "end_date": payload["end_date"],
            **_collection_metadata(payload, parts),
            "aoi": payload["aoi"],
            "variables": payload["variables"],
            "analysis_methods": payload["analysis_methods"],
            "max_days_per_chunk": MAX_DAYS_PER_DAILY_JOB,
        },
        "variables": variables,
        "comparison": {"visualizations": _comparison_visualizations(payload, parts)},
        "warnings": _all_warnings(parts),
    }


def assemble_region_comparison(
    payload: dict[str, Any], job_id: str, parts: list[dict[str, Any]]
) -> dict[str, Any]:
    spec = get_variable_spec(payload["variable"])
    regions = {part["key"]: part["entry"] for part in parts}
    return {
        "success": True,
        "job_id": job_id,
        "metadata": {
            "mode": "region_comparison",
            "variable": payload["variable"],
            "dataset": spec.dataset,
            "start_date": payload["start_date"],
            "end_date": payload["end_date"],
            **_collection_metadata(payload, parts),
            "analysis_methods": payload["analysis_methods"],
            "max_days_per_chunk": MAX_DAYS_PER_DAILY_JOB,
        },
        "regions": regions,
        "comparison": {"visualizations": _comparison_visualizations(payload, parts)},
        "warnings": _all_warnings(parts),
    }


def execute_variable_comparison(
    payload: dict[str, Any],
    job_id: str,
    progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    variables = payload["variables"]
    parts = [
        variable_part(
            payload,
            variable_id,
            job_id,
            progress=progress,
            should_cancel=should_cancel,
            position=(index, len(variables)),
        )
        for index, variable_id in enumerate(variables, start=1)
    ]
    return assemble_variable_comparison(payload, job_id, parts)


def execute_region_comparison(
    payload: dict[str, Any],
    job_id: str,
    progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    parts = []
    for index, aoi in enumerate(payload["regions"], start=1):
        if should_cancel is not None and should_cancel():
            raise JobCanceled()
        label = f"region_{index}"
        if progress is not None:
            progress("region", label, index - 1, len(payload["regions"]))
        parts.append(
            region_part(
                payload,
                aoi,
                label,
                job_id,
                progress=progress,
                should_cancel=should_cancel,
            )
        )
    return assemble_region_comparison(payload, job_id, parts)
