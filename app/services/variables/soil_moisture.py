"""Soil-moisture collector backed by SMAP L3 daily."""

from __future__ import annotations

from app.services.variables.base import CollectionRequest, VariableSeries, VariableSpec
from app.services.variables.remote_grid import collect_daily_grid

SPEC = VariableSpec(
    variable_id="soil_moisture",
    display_name="Soil Moisture",
    dataset="SPL3SMP_E",
    unit="m3/m3",
    native_resolution="daily",
    value_label="Soil moisture",
)


def collect(request: CollectionRequest) -> VariableSeries:
    return collect_daily_grid(
        request,
        SPEC,
        short_name="SPL3SMP_E",
        version="006",
        variable_names=("soil_moisture", "soil_moisture_am", "soil_moisture_pm"),
        valid_min=0.0,
        valid_max=0.8,
        quality_variable="retrieval_qual_flag",
        quality_value=0,
    )
