"""Background comparison-analysis endpoints."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.routes.analysis import (
    AOIRequest,
    MAX_TOTAL_DAYS,
    PRODUCT_START_DATE,
    SamplingRequest,
    validate_sampling_and_strategy,
)
from app.jobs import create_job
from app.services.analysis_engine import SUPPORTED_METHODS
from app.services.temporal import resolve_seed
from app.services.variables import SUPPORTED_VARIABLES

router = APIRouter(prefix="/analysis", tags=["Analysis"])

AnalysisMethod = Literal[
    "linear_regression",
    "mann_kendall",
    "modified_mann_kendall",
    "seasonal_mann_kendall",
    "t_test",
]

MAX_REGIONS = 8


class VariableComparisonRequest(BaseModel):
    aoi: AOIRequest = Field(..., description="Geographic bounding box shared by every variable.")
    start_date: str = Field(..., description="Start date in ISO format YYYY-MM-DD.")
    end_date: str = Field(..., description="End date in ISO format YYYY-MM-DD.")
    temporal_resolution: Literal["daily", "monthly", "yearly"] = Field(
        "daily",
        description="Aggregation frequency: `daily`, `monthly`, or `yearly`.",
    )
    spatial_strategy: Literal["coarse", "native"] = Field(
        "coarse",
        description="Spatial resolution strategy (`coarse` or `native`). Only applicable for `monthly` or `yearly`.",
    )
    sampling: SamplingRequest = Field(default_factory=SamplingRequest)
    variables: list[str] = Field(
        ...,
        description="Variable ids to compare.",
        examples=[["land_surface_temperature", "ndvi", "precipitation", "soil_moisture", "air_temperature"]],
    )
    analysis_methods: list[AnalysisMethod] = Field(
        default_factory=lambda: ["linear_regression", "mann_kendall", "t_test"]
    )
    include_visualizations: bool = Field(True, description="Render base64 PNG charts.")


class RegionComparisonRequest(BaseModel):
    variable: str = Field(..., description="One variable id to compare across regions.")
    regions: list[AOIRequest] = Field(..., description="List of bounding boxes.")
    start_date: str = Field(..., description="Start date in ISO format YYYY-MM-DD.")
    end_date: str = Field(..., description="End date in ISO format YYYY-MM-DD.")
    temporal_resolution: Literal["daily", "monthly", "yearly"] = Field(
        "daily",
        description="Aggregation frequency: `daily`, `monthly`, or `yearly`.",
    )
    spatial_strategy: Literal["coarse", "native"] = Field(
        "coarse",
        description="Spatial resolution strategy (`coarse` or `native`). Only applicable for `monthly` or `yearly`.",
    )
    sampling: SamplingRequest = Field(default_factory=SamplingRequest)
    analysis_methods: list[AnalysisMethod] = Field(
        default_factory=lambda: ["linear_regression", "mann_kendall", "t_test"]
    )
    include_visualizations: bool = Field(True, description="Render base64 PNG charts.")


def _collection_options(payload: Any, seed_key: str) -> dict[str, Any]:
    """Validate and normalise resolution/strategy/sampling for a comparison."""
    resolution, strategy, sampling = validate_sampling_and_strategy(
        payload.temporal_resolution,
        payload.spatial_strategy,
        payload.sampling,
    )
    if sampling["mode"] == "random_sample" and sampling["seed"] is None:
        sampling["seed"] = resolve_seed(None, fallback_key=seed_key)
    return {
        "temporal_resolution": resolution,
        "spatial_strategy": strategy,
        "sampling": sampling,
    }


def _parse_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: '{value}'. Expected YYYY-MM-DD.") from exc


def _validate_dates(start_date: str, end_date: str) -> tuple[date, date]:
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must not be after end_date.")
    if start < PRODUCT_START_DATE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"start_date must be on or after {PRODUCT_START_DATE.isoformat()} "
                "(MOD11A1 v061 data coverage start)."
            ),
        )
    if (end - start).days + 1 > MAX_TOTAL_DAYS:
        raise HTTPException(status_code=400, detail=f"Date range exceeds the {MAX_TOTAL_DAYS}-day maximum.")
    return start, end


def _validate_aoi(aoi: AOIRequest) -> dict[str, float]:
    bounds = aoi.model_dump()
    if bounds["west"] >= bounds["east"]:
        raise HTTPException(status_code=400, detail="AOI west must be less than east.")
    if bounds["south"] >= bounds["north"]:
        raise HTTPException(status_code=400, detail="AOI south must be less than north.")
    return bounds


def _validate_methods(methods: list[str]) -> list[str]:
    lower = [str(method).lower() for method in methods]
    unsupported = sorted(set(lower) - SUPPORTED_METHODS)
    if unsupported:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported analysis methods: {unsupported}. Supported methods: {sorted(SUPPORTED_METHODS)}",
        )
    if not lower:
        raise HTTPException(status_code=400, detail="At least one analysis method is required.")
    return lower


def _validate_variables(variables: list[str]) -> list[str]:
    if not variables:
        raise HTTPException(status_code=400, detail="At least one variable is required.")
    unsupported = sorted(set(variables) - SUPPORTED_VARIABLES)
    if unsupported:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported variables: {unsupported}. Supported variables: {sorted(SUPPORTED_VARIABLES)}",
        )
    # Preserve request order while dropping duplicates.
    return list(dict.fromkeys(variables))


def _enqueue(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        job_id = create_job(kind, payload)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not enqueue analysis job: {exc}") from exc
    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/api/v1/analysis/jobs/{job_id}",
        "result_url": f"/api/v1/analysis/jobs/{job_id}/result",
    }


@router.post("/compare/variables", status_code=202)
def compare_variables(payload: VariableComparisonRequest) -> dict[str, Any]:
    variables = _validate_variables(payload.variables)
    methods = _validate_methods(payload.analysis_methods)
    _validate_dates(payload.start_date, payload.end_date)
    options = _collection_options(
        payload,
        seed_key=f"variables|{payload.start_date}|{payload.end_date}|{','.join(variables)}",
    )
    return _enqueue(
        "variable_comparison",
        {
            "aoi": _validate_aoi(payload.aoi),
            "start_date": payload.start_date,
            "end_date": payload.end_date,
            **options,
            "variables": variables,
            "analysis_methods": methods,
            "include_visualizations": payload.include_visualizations,
        },
    )


@router.post("/compare/regions", status_code=202)
def compare_regions(payload: RegionComparisonRequest) -> dict[str, Any]:
    if not payload.regions:
        raise HTTPException(status_code=400, detail="At least one region is required.")
    if len(payload.regions) > MAX_REGIONS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_REGIONS} regions are allowed per job.")
    methods = _validate_methods(payload.analysis_methods)
    _validate_dates(payload.start_date, payload.end_date)
    if payload.variable not in SUPPORTED_VARIABLES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported variable: '{payload.variable}'. Supported variables: {sorted(SUPPORTED_VARIABLES)}",
        )

    regions = [_validate_aoi(aoi) for aoi in payload.regions]
    seen: set[tuple[float, float, float, float]] = set()
    for region in regions:
        key = (region["west"], region["south"], region["east"], region["north"])
        if key in seen:
            raise HTTPException(status_code=400, detail="Duplicate AOI regions are not allowed.")
        seen.add(key)

    options = _collection_options(
        payload,
        seed_key=f"regions|{payload.variable}|{payload.start_date}|{payload.end_date}|{regions}",
    )
    return _enqueue(
        "region_comparison",
        {
            "variable": payload.variable,
            "regions": regions,
            "start_date": payload.start_date,
            "end_date": payload.end_date,
            **options,
            "analysis_methods": methods,
            "include_visualizations": payload.include_visualizations,
        },
    )

