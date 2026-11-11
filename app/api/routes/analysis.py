from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.modis_timeseries import TimeSeriesConfig, collect_mod11a1_timeseries
from app.services.statistical_analysis import (
        linear_regression_analysis,
        mann_kendall_analysis,
        modified_mann_kendall_analysis,
        paired_period_t_test,
        seasonal_mann_kendall_analysis,
        validate_time_series,
    )
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


class AOIRequest(BaseModel):
    west: float = Field(..., ge=-180.0, le=180.0, description="Western longitude boundary in decimal degrees.")
    south: float = Field(..., ge=-90.0, le=90.0, description="Southern latitude boundary in decimal degrees.")
    east: float = Field(..., ge=-180.0, le=180.0, description="Eastern longitude boundary in decimal degrees.")
    north: float = Field(..., ge=-90.0, le=90.0, description="Northern latitude boundary in decimal degrees.")


class TrendAnalysisRequest(BaseModel):
    product: str = Field("MOD11A1", description="Satellite product short name.", example="MOD11A1")
    aoi: AOIRequest = Field(..., description="Geographic bounding box for the AOI.")
    start_date: str = Field(..., description="Analysis start date in ISO format YYYY-MM-DD.", example="2024-01-01")
    end_date: str = Field(..., description="Analysis end date in ISO format YYYY-MM-DD.", example="2024-12-31")
    temporal_resolution: Literal["daily", "monthly", "yearly"] = Field("monthly", description="Aggregation frequency for the MODIS time series.")
    analysis_methods: list[Literal[
        "linear_regression",
        "mann_kendall",
        "modified_mann_kendall",
        "seasonal_mann_kendall",
        "t_test",
    ]] = Field(
        default_factory=lambda: ["linear_regression", "mann_kendall", "t_test"],
        description="Analysis methods to run for the prepared time series.",
        example=["linear_regression", "mann_kendall", "t_test"],
    )


class TrendAnalysisResponse(BaseModel):
    success: bool
    metadata: dict[str, Any]
    data_quality: dict[str, Any]
    analysis_results: dict[str, Any]
    visualizations: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def _parse_iso_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name} format: '{value}'. Expected YYYY-MM-DD.",
        ) from exc


def _as_stats_records(time_series_payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = time_series_payload.get("records", [])
    if not records:
        return []

    transformed: list[dict[str, Any]] = []
    for record in records:
        if record.get("missing_observation") or record.get("mean_temperature_celsius") is None:
            mean_lst = None
            missing = True
            valid_pixels = 0
        else:
            mean_lst = float(record["mean_temperature_celsius"])
            missing = False
            valid_pixels = int(record.get("valid_pixels", record.get("valid_pixel_count", 0)))

        transformed.append(
            {
                "observation_date": record.get("observation_date"),
                "mean_lst": mean_lst,
                "valid_pixel_count": int(record.get("valid_pixels", record.get("valid_pixel_count", 0))),
                "total_pixel_count": int(record.get("total_pixels", record.get("total_pixel_count", 0))),
                "valid_data_percentage": float(record.get("valid_data_percentage", 0.0)),
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


@router.post(
    "/trend",
    summary="Run MODIS LST trend analysis for a user-defined AOI.",
    response_model=TrendAnalysisResponse,
    responses={
        200: {
            "description": "Trend analysis completed successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "success": True,
                        "metadata": {
                            "product": "MOD11A1",
                            "temporal_resolution": "monthly",
                            "start_date": "2024-01-01",
                            "end_date": "2024-12-31",
                            "aoi": {"west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7},
                        },
                        "data_quality": {
                            "valid_observation_count": 12,
                            "missing_observation_percentage": 0.0,
                            "data_coverage_percentage": 98.3,
                            "warnings": [],
                        },
                        "analysis_results": {
                            "linear_regression": {"status": "calculated", "slope_celsius_per_year": 0.41},
                            "mann_kendall": {"status": "calculated", "trend_direction": "increasing"},
                        },
                        "visualizations": {},
                        "warnings": [],
                    }
                }
            },
        },
        400: {"description": "Invalid AOI, date range, or unsupported inputs."},
        404: {"description": "No NASA data or no valid observations were found."},
        503: {"description": "Earthdata connection or remote access failure."},
        504: {"description": "Processing timeout while downloading or preparing time series."},
    },
)
def run_trend_analysis(payload: TrendAnalysisRequest) -> dict[str, Any]:
    try:
        product = str(payload.product).upper()
        if product not in SUPPORTED_PRODUCTS:
            raise HTTPException(status_code=400, detail=f"Unsupported product: '{payload.product}'. Supported products: {sorted(SUPPORTED_PRODUCTS)}")

        start_date = _parse_iso_date(payload.start_date, "start_date")
        end_date = _parse_iso_date(payload.end_date, "end_date")
        if start_date > end_date:
            raise HTTPException(status_code=400, detail="start_date must not be after end_date.")

        aoi = payload.aoi.model_dump()
        if not (-180.0 <= aoi["west"] <= 180.0 and -180.0 <= aoi["east"] <= 180.0):
            raise HTTPException(status_code=400, detail="AOI longitude bounds must lie within [-180, 180].")
        if not (-90.0 <= aoi["south"] <= 90.0 and -90.0 <= aoi["north"] <= 90.0):
            raise HTTPException(status_code=400, detail="AOI latitude bounds must lie within [-90, 90].")
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

        config = TimeSeriesConfig(
            aoi=aoi,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            product_short_name=product,
            temporal_aggregation=temporal_resolution,
            region_name="trend_api",
        )

        try:
            time_series_payload = collect_mod11a1_timeseries(config)
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=f"Processing timeout: {exc}") from exc
        except (ConnectionError, OSError) as exc:
            raise HTTPException(status_code=503, detail=f"NASA Earthdata connection failure: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=f"No NASA data available: {exc}") from exc

        records = _as_stats_records(time_series_payload)
        if not records:
            raise HTTPException(status_code=404, detail="No valid time series records were returned for the requested AOI and date range.")

        try:
            validation = validate_time_series(records)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=f"No valid observations: {exc}") from exc

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
            raise HTTPException(status_code=404, detail="Insufficient observations for statistical inference.")

        analysis_results: dict[str, Any] = {}
        detected_warnings: list[str] = list(data_quality["warnings"])

        for method in requested_methods:
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
                        detected_warnings.append(result.get("insufficient_data_reason", result.get("reason", "Seasonal Mann-Kendall was not calculated.")))
            elif method == "t_test":
                result = _calculate_t_test(records)
                analysis_results[method] = result
                detected_warnings.extend(result.get("warnings", []))

        response = {
            "success": True,
            "metadata": {
                "product": product,
                "temporal_resolution": temporal_resolution,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "aoi": aoi,
            },
            "data_quality": data_quality,
            "analysis_results": analysis_results,
            "visualizations": {},
            "warnings": sorted(set(detected_warnings)),
        }
        return response

    except HTTPException:
        raise
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=f"Processing timeout: {exc}") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=f"Earthdata connection failure: {exc}") from exc
    except Exception as exc:  # pragma: no cover - defensive fallback
        raise HTTPException(status_code=500, detail=f"Unexpected analysis error: {exc}") from exc


@router.get("/health")
def analysis_health() -> dict[str, Any]:
    return {"status": "ok", "message": "Trend analysis service is available."}
