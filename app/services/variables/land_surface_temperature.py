"""Land-surface temperature collector backed by the existing MOD11A1 pipeline."""

from __future__ import annotations

from app.services.modis_timeseries import TimeSeriesConfig, collect_mod11a1_timeseries
from app.services.variables.base import CollectionRequest, PeriodSource, VariableSeries, VariableSpec, build_record
from app.utils.logging import log

SPEC = VariableSpec(
    variable_id="land_surface_temperature",
    display_name="Land Surface Temperature",
    dataset="MOD11A1",
    unit="Celsius",
    native_resolution="daily",
    value_label="LST",
    monthly_source=PeriodSource(
        short_name="MOD11C3",
        version="061",
        variable_names=("LST_Day_CMG",),
        reader="gdal",
        native_resolution="0.05°",
        scale=0.02,
        offset=-273.15,
        valid_min=7500.0,
        valid_max=65535.0,
        gdal_grid="MODIS_Grid_Monthly_CMG_LST",
        gdal_subdataset="LST_Day_CMG",
        gdal_src_nodata=0.0,
        analysis_resolution=0.01,
    ),
)


def collect(request: CollectionRequest) -> VariableSeries:
    log(
        "variables: land_surface_temperature: collecting "
        f"{request.start_date} -> {request.end_date} for {request.aoi}"
    )
    config = TimeSeriesConfig(
        aoi=request.aoi,
        start_date=request.start_date,
        end_date=request.end_date,
        product_short_name="MOD11A1",
        temporal_aggregation="daily",
        output_dir=request.output_dir,
        region_name="land_surface_temperature",
        should_cancel=request.should_cancel,
    )
    payload = collect_mod11a1_timeseries(config)

    records = []
    for item in payload.get("records", []):
        value = item.get("mean_temperature_celsius")
        records.append(
            build_record(
                item.get("observation_date"),
                value,
                SPEC,
                valid_pixels=int(item.get("valid_pixels") or 0),
                total_pixels=int(item.get("total_pixels") or 0),
                missing_reason=item.get("missing_reason"),
                granules_used=item.get("granules_used"),
                coverage_status=item.get("coverage_status"),
            )
        )

    return VariableSeries(
        spec=SPEC,
        records=records,
        warnings=list(payload.get("warnings", [])),
        mean_raster=payload.get("mean_lst_raster"),
        raster_bounds=payload.get("raster_bounds"),
    )
