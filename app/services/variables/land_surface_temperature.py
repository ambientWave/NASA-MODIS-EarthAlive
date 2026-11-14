"""Land-surface temperature collector backed by MODIS LST via AppEEARS.

Selection (``app/catalog/selector.py``) decides the product:

* daily requests read MOD11A1 (1 km, daily);
* monthly/yearly requests read MOD11A2 (1 km, 8-day composites) and derive the
  requested period from the provider's own composites.

No monthly LST product exists in AppEEARS, so there is no monthly source to
read and no daily-to-monthly averaging of raw granules.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.services.modis_timeseries import TimeSeriesConfig, collect_mod11a1_timeseries
from app.services.variables.base import CollectionRequest, VariableSeries, VariableSpec, build_record
from app.utils.logging import log

DEFAULT_PRODUCT = "MOD11A1"
COMPOSITE_PRODUCT = "MOD11A2"
COMPOSITE_STEP_DAYS = 8

# Verified AppEEARS layer names, one tuple per readable product. This is the
# only place the layers live; the selector never touches them.
DEFAULT_LAYERS: dict[str, tuple[str, ...]] = {
    "MOD11A1": ("LST_Day_1km", "QC_Day"),
    "MOD11A2": ("LST_Day_1km", "QC_Day"),
    "MYD11A1": ("LST_Day_1km", "QC_Day"),
}

SPEC = VariableSpec(
    variable_id="land_surface_temperature",
    display_name="Land Surface Temperature",
    dataset=DEFAULT_PRODUCT,
    unit="Celsius",
    native_resolution="daily",
    value_label="LST",
    composite_product=COMPOSITE_PRODUCT,
    composite_step_days=COMPOSITE_STEP_DAYS,
)


def _window_ranges(
    sample_days: tuple[str, ...],
    end_date: str,
    step_days: int = COMPOSITE_STEP_DAYS,
) -> list[tuple[str, str]]:
    """Return one AppEEARS range per sampled observation start.

    For 8-day composites the range is the provider's full window; for daily
    observations it is a single-day range, so AppEEARS never packages days the
    caller did not ask for.
    """
    final = date.fromisoformat(end_date)
    ranges: list[tuple[str, str]] = []
    for day in sorted(sample_days):
        start = date.fromisoformat(day)
        stop = min(start + timedelta(days=step_days - 1), final)
        ranges.append((start.isoformat(), stop.isoformat()))
    return ranges


def collect(request: CollectionRequest) -> VariableSeries:
    product = str(request.collection_product or DEFAULT_PRODUCT).upper()
    step_days = COMPOSITE_STEP_DAYS if product == COMPOSITE_PRODUCT else 1
    log(
        "variables: land_surface_temperature: collecting "
        f"{request.start_date} -> {request.end_date} for {request.aoi} ({product})"
    )

    date_ranges = None
    if request.sample_days:
        date_ranges = _window_ranges(request.sample_days, request.end_date, step_days)

    config = TimeSeriesConfig(
        aoi=request.aoi,
        start_date=request.start_date,
        end_date=request.end_date,
        product_short_name=product,
        temporal_aggregation="daily",
        output_dir=request.output_dir,
        region_name=f"lst_{product.lower()}",
        should_cancel=request.should_cancel,
        sample_days=request.sample_days,
        sampling_unit_days=step_days,
        date_ranges=date_ranges,
        layers=DEFAULT_LAYERS.get(product, DEFAULT_LAYERS[DEFAULT_PRODUCT]),
    )
    payload = collect_mod11a1_timeseries(config)

    source_frequency = "8-day" if step_days == COMPOSITE_STEP_DAYS else "daily"
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
                source_frequency=source_frequency,
            )
        )

    return VariableSeries(
        spec=SPEC,
        records=records,
        warnings=list(payload.get("warnings", [])),
        mean_raster=payload.get("mean_lst_raster"),
        raster_bounds=payload.get("raster_bounds"),
        source_products=[product],
        source_resolution="1000m",
        collection_strategy="composite" if step_days > 1 else "daily",
    )
