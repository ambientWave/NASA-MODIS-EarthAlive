from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.jobs import create_job
from app.utils.logging import log

router = APIRouter(prefix="/analysis", tags=["Analysis"])

SUPPORTED_PRODUCTS = {"MOD11A1"}
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


class TrendAnalysisRequest(BaseModel):
    product: str = Field("MOD11A1", description="Satellite product short name.", examples=["MOD11A1"])
    aoi: AOIRequest = Field(..., description="Geographic bounding box for the AOI.")
    start_date: str = Field(..., description="Analysis start date in ISO format YYYY-MM-DD.", examples=["2024-01-01"])
    end_date: str = Field(..., description="Analysis end date in ISO format YYYY-MM-DD.", examples=["2024-12-31"])
    temporal_resolution: Literal["daily", "monthly", "yearly"] = Field(
        "monthly", description="Aggregation frequency for the MODIS time series."
    )
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


@router.post(
    "/trend",
    status_code=202,
    response_model=JobAcceptedResponse,
    summary="Queue a MODIS LST trend analysis for a user-defined AOI.",
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
    product = str(payload.product).upper()
    if product not in SUPPORTED_PRODUCTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported product: '{payload.product}'. Supported products: {sorted(SUPPORTED_PRODUCTS)}",
        )

    start_date = _parse_iso_date(payload.start_date, "start_date")
    end_date = _parse_iso_date(payload.end_date, "end_date")
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must not be after end_date.")
    if start_date < PRODUCT_START_DATE:
        raise HTTPException(
            status_code=400,
            detail=f"start_date must be on or after {PRODUCT_START_DATE.isoformat()} (MOD11A1 v061 data coverage start).",
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
        "product": product,
        "aoi": aoi,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "temporal_resolution": temporal_resolution,
        "analysis_methods": requested_methods,
    }

    log(
        "trend: request accepted "
        f"product={product} aoi={aoi} start={start_date} end={end_date} "
        f"resolution={temporal_resolution} methods={requested_methods}"
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
