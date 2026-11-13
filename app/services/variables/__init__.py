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

# Convenience aliases accepted by the trend endpoint. Values are canonical
# variable ids; dataset short names are included because callers often know
# the product rather than the internal id.
_ALIASES = {
    "lst": "land_surface_temperature",
    "land_surface_temperature": "land_surface_temperature",
    "mod11a1": "land_surface_temperature",
    "ndvi": "ndvi",
    "mod13q1": "ndvi",
    "mod13a1": "ndvi",
    "precipitation": "precipitation",
    "gpm": "precipitation",
    "gpm_3imergd": "precipitation",
    "soil_moisture": "soil_moisture",
    "soil moisture": "soil_moisture",
    "smap": "soil_moisture",
    "spl3smp_e": "soil_moisture",
    "air_temperature": "air_temperature",
    "air temperature": "air_temperature",
    "merra": "air_temperature",
    "m2sdnxslv": "air_temperature",
}


def resolve_variable_id(value: str) -> str:
    """Map an id, display name, or dataset short name to a canonical id."""
    key = str(value).strip().lower()
    if key in _REGISTRY:
        return key
    if key in _ALIASES:
        return _ALIASES[key]
    raise ValueError(f"Unsupported variable: '{value}'. Supported variables: {sorted(_REGISTRY)}")


def get_variable_spec(variable_id: str) -> VariableSpec:
    canonical = resolve_variable_id(variable_id)
    return _REGISTRY[canonical][0]


def collect_variable(variable_id: str, request: CollectionRequest) -> VariableSeries:
    canonical = resolve_variable_id(variable_id)
    return _REGISTRY[canonical][1](request)
