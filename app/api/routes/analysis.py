from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.jobs import create_job
from app.services.temporal import resolve_seed
from app.services.variables import resolve_variable_id
from app.utils.logging import log

router = APIRouter(prefix="/analysis", tags=["Analysis"])

SUPPORTED_TEMPORAL_RESOLUTION = {"daily", "monthly", "yearly"}
SUPPORTED_ANALYSIS_METHODS = {
    "linear_regression",
    "mann_kendall",
    "modified_mann_kendall",
    "seasonal_mann_kendall",
    "t_test",
}

PRODUCT_START_DATE = date(2000, 2, 24)  # MOD11A1 v061 coverage start
MAX_TOTAL_DAYS = 366 * 30


class AOIRequest(BaseModel):
    west: float = Field(..., ge=-180.0, le=180.0, description="Western longitude boundary in decimal degrees.")
    south: float = Field(..., ge=-90.0, le=90.0, description="Southern latitude boundary in decimal degrees.")
    east: float = Field(..., ge=-180.0, le=180.0, description="Eastern longitude boundary in decimal degrees.")
    north: float = Field(..., ge=-90.0, le=90.0, description="Northern latitude boundary in decimal degrees.")


class SamplingRequest(BaseModel):
    """Random observation sampling, used instead of averaging where needed."""

    mode: Literal["all", "random_day_per_month"] = Field(
        "all",
        description=(
            "`all` uses every day or the provider monthly product. "
            "`random_day_per_month` selects uniformly random real observations, "
            "which is the fallback for variables without a monthly product."
        ),
    )
    samples_per_month: int = Field(1, ge=1, le=15, description="Random days selected per calendar month.")
    seed: int | None = Field(None, description="Seed for reproducible sampling; derived from the request when omitted.")
    missing: Literal["keep", "redraw"] = Field(
        "keep",
        description="`keep` leaves a missing sampled day missing; `redraw` replaces it with another candidate day.",
    )


class TrendAnalysisRequest(BaseModel):
    variable: str | None = Field(
        None,
        description=(
            "Variable to analyze: land_surface_temperature (LST), ndvi, precipitation, "
            "soil_moisture, or air_temperature. Defaults to land_surface_temperature."
        ),
        examples=["ndvi"],
    )
    product: str | None = Field(
        None,
        description=(
            "Deprecated alias for `variable`. Accepts a dataset short name such as MOD11A1, "
            "MOD13Q1, GPM_3IMERGD, SPL3SMP_E, or M2SDNXSLV."
        ),
        examples=["MOD11A1"],
    )
    aoi: AOIRequest = Field(..., description="Geographic bounding box for the AOI.")
    start_date: str = Field(..., description="Analysis start date in ISO format YYYY-MM-DD.", examples=["2024-01-01"])
    end_date: str = Field(..., description="Analysis end date in ISO format YYYY-MM-DD.", examples=["2024-12-31"])
    temporal_resolution: Literal["daily", "monthly", "yearly"] = Field(
        "monthly", description="Aggregation frequency for the MODIS time series."
    )
    spatial_strategy: Literal["coarse", "native"] = Field(
        "coarse",
        description=(
            "`coarse` reads the provider's monthly product (12 files/year, 0.05° for "
            "MODIS). `native` aggregates the daily/native product (more files, finer pixels)."
        ),
    )
    sampling: SamplingRequest = Field(default_factory=SamplingRequest)
    analysis_methods: list[Literal[
        "linear_regression",
        "mann_kendall",
        "modified_mann_kendall",
        "seasonal_mann_kendall",
        "t_test",
    ]] = Field(
        default_factory=lambda: ["linear_regression", "mann_kendall", "t_test"],
        description="Analysis methods to run for the prepared time series.",
        examples=[["linear_regression", "mann_kendall", "t_test"]],
    )


class JobAcceptedResponse(BaseModel):
    job_id: str
    status: str
    status_url: str
    result_url: str


def _parse_iso_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name} format: '{value}'. Expected YYYY-MM-DD.",
        ) from exc


def _resolve_variable(payload: TrendAnalysisRequest) -> str:
    """Resolve `variable` / deprecated `product` into one canonical variable id."""
    try:
        if payload.variable is not None:
            from_variable = resolve_variable_id(payload.variable)
        else:
            from_variable = None

        if payload.product is not None:
            from_product = resolve_variable_id(payload.product)
        else:
            from_product = None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if from_variable and from_product and from_variable != from_product:
        raise HTTPException(
            status_code=400,
            detail=(
                f"`variable` ('{payload.variable}') and `product` ('{payload.product}') "
                "refer to different variables; send only one."
            ),
        )
    return from_variable or from_product or "land_surface_temperature"


@router.post(
    "/trend",
    status_code=202,
    response_model=JobAcceptedResponse,
    summary="Queue a trend analysis for one environmental variable and AOI.",
    responses={
        202: {
            "description": "Trend analysis job accepted.",
            "content": {
                "application/json": {
                    "example": {
                        "job_id": "9c0f...",
                        "status": "queued",
                        "status_url": "/api/v1/analysis/jobs/9c0f...",
                        "result_url": "/api/v1/analysis/jobs/9c0f.../result",
                    }
                }
            },
        },
        400: {"description": "Invalid AOI, date range, or unsupported inputs."},
        503: {"description": "Could not enqueue the job (analysis queue unavailable)."},
    },
)
def run_trend_analysis(payload: TrendAnalysisRequest) -> dict[str, Any]:
    variable_id = _resolve_variable(payload)

    start_date = _parse_iso_date(payload.start_date, "start_date")
    end_date = _parse_iso_date(payload.end_date, "end_date")
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must not be after end_date.")
    if start_date < PRODUCT_START_DATE:
        raise HTTPException(
            status_code=400,
            detail=f"start_date must be on or after {PRODUCT_START_DATE.isoformat()} (earliest supported data coverage start).",
        )
    if (end_date - start_date).days + 1 > MAX_TOTAL_DAYS:
        raise HTTPException(status_code=400, detail=f"Date range exceeds the {MAX_TOTAL_DAYS}-day maximum.")

    aoi = payload.aoi.model_dump()
    if aoi["west"] >= aoi["east"]:
        raise HTTPException(status_code=400, detail="AOI west must be less than east.")
    if aoi["south"] >= aoi["north"]:
        raise HTTPException(status_code=400, detail="AOI south must be less than north.")

    temporal_resolution = str(payload.temporal_resolution).lower()
    if temporal_resolution not in SUPPORTED_TEMPORAL_RESOLUTION:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported temporal resolution: '{payload.temporal_resolution}'. Supported values: {sorted(SUPPORTED_TEMPORAL_RESOLUTION)}",
        )

    spatial_strategy = str(payload.spatial_strategy).lower()
    if temporal_resolution == "daily":
        if payload.sampling.mode != "all":
            raise HTTPException(status_code=400, detail="Random sampling requires a monthly or yearly temporal_resolution.")
        if spatial_strategy != "coarse":
            raise HTTPException(status_code=400, detail="spatial_strategy only applies to monthly or yearly requests.")

    sampling = payload.sampling.model_dump()
    if sampling["mode"] == "random_day_per_month" and sampling["seed"] is None:
        sampling["seed"] = resolve_seed(
            None,
            fallback_key=(
                f"{variable_id}|{start_date.isoformat()}|{end_date.isoformat()}|"
                f"{aoi['west']},{aoi['south']},{aoi['east']},{aoi['north']}"
            ),
        )

    requested_methods = [str(method).lower() for method in payload.analysis_methods]
    unsupported_methods = sorted(set(requested_methods) - SUPPORTED_ANALYSIS_METHODS)
    if unsupported_methods:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported analysis methods: {unsupported_methods}. Supported methods: {sorted(SUPPORTED_ANALYSIS_METHODS)}",
        )
    if not requested_methods:
        raise HTTPException(status_code=400, detail="At least one analysis method is required.")

    job_payload = {
        "variable": variable_id,
        "aoi": aoi,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "temporal_resolution": temporal_resolution,
        "spatial_strategy": spatial_strategy,
        "sampling": sampling,
        "analysis_methods": requested_methods,
    }

    log(
        "trend: request accepted "
        f"variable={variable_id} aoi={aoi} start={start_date} end={end_date} "
        f"resolution={temporal_resolution} strategy={spatial_strategy} "
        f"sampling={sampling['mode']} methods={requested_methods}"
    )
    try:
        job_id = create_job("trend_analysis", job_payload)
    except Exception as exc:
        log(f"trend: could not enqueue job: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=503, detail=f"Could not enqueue analysis job: {exc}") from exc

    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/api/v1/analysis/jobs/{job_id}",
        "result_url": f"/api/v1/analysis/jobs/{job_id}/result",
    }


@router.get("/health")
def analysis_health() -> dict[str, Any]:
    return {"status": "ok", "message": "Trend analysis service is available."}
