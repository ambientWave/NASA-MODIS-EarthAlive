"""Registry of supported environmental variables and their collectors."""

from __future__ import annotations

from app.services.variables import air_temperature, land_surface_temperature, ndvi, precipitation, soil_moisture
from app.services.variables.base import CollectionRequest, VariableSeries, VariableSpec

_REGISTRY = {
    spec.variable_id: (spec, collector)
    for spec, collector in [
        (land_surface_temperature.SPEC, land_surface_temperature.collect),
        (ndvi.SPEC, ndvi.collect),
        (precipitation.SPEC, precipitation.collect),
        (soil_moisture.SPEC, soil_moisture.collect),
        (air_temperature.SPEC, air_temperature.collect),
    ]
}

SUPPORTED_VARIABLES = frozenset(_REGISTRY)


def get_variable_spec(variable_id: str) -> VariableSpec:
    try:
        return _REGISTRY[variable_id][0]
    except KeyError as exc:
        raise ValueError(f"Unsupported variable: '{variable_id}'. Supported variables: {sorted(_REGISTRY)}") from exc


def collect_variable(variable_id: str, request: CollectionRequest) -> VariableSeries:
    try:
        collector = _REGISTRY[variable_id][1]
    except KeyError as exc:
        raise ValueError(f"Unsupported variable: '{variable_id}'. Supported variables: {sorted(_REGISTRY)}") from exc
    return collector(request)
