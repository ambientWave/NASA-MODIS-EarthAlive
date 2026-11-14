from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.catalog.selector import resolve_selection
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

MAX_TOTAL_DAYS = 366 * 30
# Earliest coverage start used by the comparison routes (MOD11A1 v061). The
# trend route clamps per product instead of rejecting early dates.
PRODUCT_START_DATE = date(2000, 2, 24)

class AOIRequest(BaseModel):
    west: float = Field(..., ge=-180.0, le=180.0, description="Western longitude boundary in decimal degrees.")
    south: float = Field(..., ge=-90.0, le=90.0, description="Southern latitude boundary in decimal degrees.")
    east: float = Field(..., ge=-180.0, le=180.0, description="Eastern longitude boundary in decimal degrees.")
    north: float = Field(..., ge=-90.0, le=90.0, description="Northern latitude boundary in decimal degrees.")


class SamplingRequest(BaseModel):
    """Observation sampling settings for monthly or yearly aggregations.

    Frontend Rules:
    - Only relevant when `temporal_resolution` is `monthly` or `yearly`.
    - When `mode` is `full_period`: `samples_per_month`, `seed`, and `missing` are ignored.
    - When `mode` is `random_sample`: `samples_per_month` (1..15) defines how many days are sampled.
    """

    mode: Literal["full_period", "random_sample"] = Field(
        "full_period",
        description=(
            "Observation selection mode:\n"
            "- `full_period` (Default): Uses every observation in the period, or the provider's "
            "coarser product when one exists. `samples_per_month` is ignored.\n"
            "- `random_sample`: Selects N real observations per calendar month, at the granularity "
            "the selected product supports (individual days, or one composite window per month). "
            "Requires `samples_per_month` (1-15)."
        ),
    )
    samples_per_month: int = Field(
        1,
        ge=1,
        le=15,
        description="Number of random days selected per calendar month (1 to 15). Only active when mode is 'random_sample'.",
    )
    seed: int | None = Field(
        None,
        description="Optional seed for reproducible random sampling. Derived from request parameters if omitted.",
    )
    missing: Literal["keep", "redraw"] = Field(
        "keep",
        description="Policy for missing sampled days: `keep` retains the gap, `redraw` draws an alternate day in the month.",
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
            "Spatial resolution strategy for monthly/yearly requests:\n"
            "- `coarse` (Default, Fast): Picks the coarsest valid product in the coarsest resolution band, "
            "so monthly/yearly requests never download daily granules to average them.\n"
            "- `native` (Detailed, Heavy): Picks the finest resolution band first, then the coarsest valid "
            "granularity inside it.\n"
            "Dates that fall outside the selected product's coverage are clamped and reported in "
            "`metadata.date_range`.\n"
            "UI Rule: Only applicable when `temporal_resolution` is `monthly` or `yearly`."
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


def validate_sampling_and_strategy(
    temporal_resolution: str,
    spatial_strategy: str,
    sampling: SamplingRequest,
) -> tuple[str, str, dict[str, Any]]:
    """Validate and normalize temporal resolution, spatial strategy, and sampling.

    Core Rules for Frontend Integration:
    -----------------------------------
    1. Daily Resolution (`temporal_resolution="daily"`):
       - Uses full daily native observations.
       - `spatial_strategy` must be 'coarse' (the default) or omitted.
       - `sampling.mode` must be 'full_period' (the default) or omitted.
       - `samples_per_month` is not applicable.

    2. Monthly / Yearly Resolution (`temporal_resolution="monthly" | "yearly"`):
       - Strategy `coarse`: picks the coarsest valid product (a provider aggregate
         if one exists, otherwise a provider composite such as MOD11A2).
       - Strategy `native`: picks the finest resolution band first, then the
         coarsest valid granularity inside it.
       - Mode `full_period`: uses every observation the selected product offers.
       - Mode `random_sample`: selects `samples_per_month` (1..15) real
         observations per month. The granularity is capped by the sample count
         (1-2 -> 16-day, 3-4 -> 8-day, 5+ -> daily); when no product at that
         granularity exists, sampling happens at composite granularity and the
         response declares ``sampling_applied="composite_window"``.
    """
    resolution = str(temporal_resolution).lower()
    if resolution not in SUPPORTED_TEMPORAL_RESOLUTION:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported temporal resolution: '{temporal_resolution}'. Supported values: {sorted(SUPPORTED_TEMPORAL_RESOLUTION)}",
        )

    strategy = str(spatial_strategy).lower()
    if resolution == "daily":
        if sampling.mode != "full_period":
            raise HTTPException(
                status_code=400,
                detail="Random sampling requires a monthly or yearly temporal_resolution.",
            )
        if strategy != "coarse":
            raise HTTPException(
                status_code=400,
                detail="spatial_strategy only applies to monthly or yearly requests.",
            )

    sampling_dict = sampling.model_dump()
    return resolution, strategy, sampling_dict


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

    requested_start = _parse_iso_date(payload.start_date, "start_date")
    requested_end = _parse_iso_date(payload.end_date, "end_date")
    if requested_start > requested_end:
        raise HTTPException(status_code=400, detail="start_date must not be after end_date.")

    temporal_resolution, spatial_strategy, sampling = validate_sampling_and_strategy(
        payload.temporal_resolution,
        payload.spatial_strategy,
        payload.sampling,
    )

    aoi = payload.aoi.model_dump()
    if aoi["west"] >= aoi["east"]:
        raise HTTPException(status_code=400, detail="AOI west must be less than east.")
    if aoi["south"] >= aoi["north"]:
        raise HTTPException(status_code=400, detail="AOI south must be less than north.")

    # Resolve the request into one product + effective range before anything
    # derives from the dates (seed, sampling plan, chunking, cap).
    try:
        selection = resolve_selection(
            variable_id,
            requested_start,
            requested_end,
            temporal_resolution,
            spatial_strategy,
            sampling["mode"],
            sampling["samples_per_month"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    start_date = selection.date_range.start
    end_date = selection.date_range.end
    if (end_date - start_date).days + 1 > MAX_TOTAL_DAYS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Effective date range exceeds the {MAX_TOTAL_DAYS}-day maximum. "
                "Reduce the range or choose a coarser temporal resolution."
            ),
        )

    if sampling["mode"] == "random_sample" and sampling["seed"] is None:
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
        "collection_product": selection.product_name,
        "selection": selection.metadata(),
        "selection_warnings": list(selection.warnings),
    }

    log(
        "trend: request accepted "
        f"variable={variable_id} aoi={aoi} start={start_date} end={end_date} "
        f"resolution={temporal_resolution} strategy={spatial_strategy} "
        f"sampling={sampling['mode']} product={selection.product_name} methods={requested_methods}"
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
