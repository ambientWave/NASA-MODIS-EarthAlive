"""Air-temperature collector backed by MERRA-2 daily statistics."""

from __future__ import annotations

from app.services.variables.base import CollectionRequest, PeriodSource, VariableSeries, VariableSpec
from app.services.variables.remote_grid import collect_daily_grid

SPEC = VariableSpec(
    variable_id="air_temperature",
    display_name="Air Temperature",
    dataset="M2SDNXSLV",
    unit="Celsius",
    native_resolution="daily",
    value_label="Air temperature",
    monthly_source=PeriodSource(
        short_name="M2TMNXSLV",
        version="5.12.4",
        variable_names=("T2MMEAN", "T2M"),
        reader="xarray",
        native_resolution="0.5° × 0.625°",
        offset=-273.15,
        valid_min=150.0,
        valid_max=350.0,
    ),
)


def collect(request: CollectionRequest) -> VariableSeries:
    return collect_daily_grid(
        request,
        SPEC,
        short_name="M2SDNXSLV",
        version="5.12.4",
        variable_names=("T2M", "T2MDEAD", "T2MMAX", "T2MMIN"),
        offset=-273.15,
        valid_min=150.0,
        valid_max=350.0,
    )
