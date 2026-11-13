"""Registry of supported environmental variables and their collectors."""

from __future__ import annotations

from app.services.variables import air_temperature, land_surface_temperature, ndvi, precipitation, soil_moisture
from app.services.variables.base import CollectionRequest, VariableSeries, VariableSpec
from app.services.temporal import (
    aggregate_records,
    planned_days,
    resolve_seed,
    sample_day_plan,
    select_sampled_records,
)
from app.services.variables.period_grid import collect_period_variable
from app.utils.logging import log

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


def _daily_collect(variable_id: str, request: CollectionRequest) -> VariableSeries:
    series = collect_variable(variable_id, request)
    series.collection_strategy = "daily"
    series.frequency = "daily"
    return series


def collect_resolved_variable(variable_id: str, request: CollectionRequest) -> VariableSeries:
    """Collect at the requested temporal resolution and spatial strategy.

    * ``daily`` — the registered daily/16-day collector.
    * ``monthly``/``yearly`` + ``coarse`` — the provider's monthly product.
    * ``monthly``/``yearly`` + ``native`` — the daily product, then aggregated.
    * ``random_day_per_month`` — real observations on randomly selected days.

    This is the single place that decides whether a monthly request downloads
    12 files or 365, so trend and comparison jobs behave identically.
    """
    spec = get_variable_spec(variable_id)
    resolution = str(request.temporal_resolution or "daily").lower()
    if resolution not in {"daily", "monthly", "yearly"}:
        raise ValueError(f"Unsupported temporal resolution: '{request.temporal_resolution}'")
    strategy = str(request.spatial_strategy or "coarse").lower()
    sampling = str(request.sampling_mode or "all").lower()

    if (
        sampling == "all"
        and resolution != "daily"
        and strategy == "coarse"
        and spec.monthly_source is None
    ):
        # No provider monthly product (e.g. soil moisture): fall back to random
        # real observations instead of silently downloading every daily file.
        sampling = "random_day_per_month"
        log(f"sampling: {variable_id}: no monthly source, using random monthly sampling")

    if sampling == "random_day_per_month":
        seed = resolve_seed(
            request.sampling_seed,
            fallback_key=f"{variable_id}|{request.start_date}|{request.end_date}|{request.aoi}",
        )
        plan = sample_day_plan(
            request.start_date,
            request.end_date,
            samples_per_month=request.samples_per_month,
            seed=seed,
            key=variable_id,
            extra_candidates=3 if request.sampling_missing == "redraw" else 0,
        )
        request.sample_days = planned_days(plan)
        log(
            f"sampling: {variable_id}: {len(request.sample_days)} candidate day(s) "
            f"({request.samples_per_month}/month, missing={request.sampling_missing}, seed={seed})"
        )
        series = _daily_collect(variable_id, request)
        series.records = select_sampled_records(
            series.records,
            plan,
            samples_per_month=request.samples_per_month,
            missing_policy=request.sampling_missing,
            spec=spec,
        )
        series.collection_strategy = "sampled"
        series.sampling = {
            "mode": sampling,
            "samples_per_month": request.samples_per_month,
            "seed": seed,
            "missing": request.sampling_missing,
        }
        if resolution == "yearly":
            series.records = aggregate_records(series.records, spec, "yearly")
            series.frequency = "yearly"
        elif resolution == "monthly":
            series.frequency = "monthly"
        return series

    if resolution == "daily":
        series = _daily_collect(variable_id, request)
        series.sampling = {"mode": "all", "samples_per_month": request.samples_per_month}
        return series

    if strategy == "coarse" and spec.monthly_source is not None:
        series = collect_period_variable(request, spec, spec.monthly_source)
        series.sampling = {"mode": "all", "samples_per_month": request.samples_per_month}
        if resolution == "yearly":
            series.records = aggregate_records(series.records, spec, "yearly")
            series.frequency = "yearly"
        log(
            f"collection: {variable_id}: {series.collection_strategy} "
            f"({len(series.records)} {series.frequency} record(s), source={series.source_products})"
        )
        return series

    log(f"collection: {variable_id}: native aggregation to {resolution}")
    series = _daily_collect(variable_id, request)
    series.records = aggregate_records(series.records, spec, resolution)
    series.frequency = resolution
    series.collection_strategy = "native_aggregated"
    series.sampling = {"mode": "all", "samples_per_month": request.samples_per_month}
    return series
