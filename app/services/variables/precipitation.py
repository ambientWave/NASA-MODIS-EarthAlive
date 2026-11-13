"""Precipitation collector backed by GPM IMERG daily."""

from __future__ import annotations

from app.services.variables.base import CollectionRequest, VariableSeries, VariableSpec
from app.services.variables.remote_grid import collect_daily_grid

SPEC = VariableSpec(
    variable_id="precipitation",
    display_name="Precipitation",
    dataset="GPM_3IMERGD",
    unit="mm/day",
    native_resolution="daily",
    value_label="Precipitation",
)


def collect(request: CollectionRequest) -> VariableSeries:
    return collect_daily_grid(
        request,
        SPEC,
        short_name="GPM_3IMERGD",
        version="07",
        variable_names=("precipitation", "precipitationCal"),
        valid_min=0.0,
        valid_max=1000.0,
    )
