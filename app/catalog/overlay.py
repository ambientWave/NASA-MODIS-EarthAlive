"""Hand-maintained product decisions for the five supported variables.

This is the only curated layer. It records *judgement* (role, priority) and
never facts (dates, resolution, granularity) — those are inherited from the
generated AppEEARS catalog in ``app/catalog/products.json``.

Roles
-----
``observation``  native repeat cycle from the provider (finest temporal unit)
``composite``    provider-composited multi-day product (e.g. MOD11A2 8-day)
``aggregate``    provider-aggregated monthly/yearly product (e.g. MOD13A3)

Priority is the deterministic tie-break when two products share the same role,
granularity and spatial band.
"""

from __future__ import annotations

from dataclasses import dataclass

# product -> (role, priority)
VARIABLE_PRODUCTS: dict[str, dict[str, tuple[str, int]]] = {
    "land_surface_temperature": {
        "MOD11A1": ("observation", 100),
        "MOD11A2": ("composite", 95),
        "MYD11A1": ("observation", 90),
        "L08_ST": ("composite", 60),
        "ECO_L2_LSTE": ("observation", 40),
    },
    "ndvi": {
        "MOD13A3": ("aggregate", 110),
        "MOD13Q1": ("composite", 100),
        "MOD13A2": ("composite", 95),
        "MOD13A1": ("composite", 90),
        "HLSS30_VI": ("observation", 50),
    },
    "soil_moisture": {
        "SPL3SMP": ("observation", 100),
        "SPL3SMP_E": ("observation", 95),
        "SPL4SMGP": ("observation", 90),
    },
    # DAYMET is the AppEEARS-only target for these two; the collectors are
    # still on GPM/MERRA (see ``legacy_extent``) until that migration lands.
    "precipitation": {"DAYMET": ("observation", 100)},
    "air_temperature": {"DAYMET": ("observation", 100)},
}


@dataclass(frozen=True)
class VariableOverlay:
    variable_id: str
    label: str
    unit: str
    products: dict[str, tuple[str, int]]
    # Product whose extent governs clamping when selection is not catalog-driven.
    clamp_product: str | None = None
    # Provider composite used to derive monthly/yearly values, if any.
    aggregate_product: str | None = None
    # Extent of the product the collector actually reads, for legacy paths whose
    # product is not in the AppEEARS catalog (GPM, MERRA-2).
    legacy_extent: tuple[str | None, str | None] | None = None
    # True when the collector fills the selected catalog product end-to-end.
    catalog_selection: bool = False
    # Products the collector can actually read today. Selection never proposes
    # a product the collection path cannot honour.
    implemented_products: frozenset[str] = frozenset()


VARIABLE_OVERLAY: dict[str, VariableOverlay] = {
    "land_surface_temperature": VariableOverlay(
        variable_id="land_surface_temperature",
        label="Land Surface Temperature",
        unit="Celsius",
        products=VARIABLE_PRODUCTS["land_surface_temperature"],
        aggregate_product="MOD11A2",
        catalog_selection=True,
        implemented_products=frozenset({"MOD11A1", "MOD11A2"}),
    ),
    "ndvi": VariableOverlay(
        variable_id="ndvi",
        label="Vegetation Index (NDVI)",
        unit="NDVI",
        products=VARIABLE_PRODUCTS["ndvi"],
        clamp_product="MOD13A1",
    ),
    "soil_moisture": VariableOverlay(
        variable_id="soil_moisture",
        label="Soil Moisture",
        unit="m3/m3",
        products=VARIABLE_PRODUCTS["soil_moisture"],
        clamp_product="SPL3SMP_E",
    ),
    "precipitation": VariableOverlay(
        variable_id="precipitation",
        label="Precipitation",
        unit="mm/day",
        products=VARIABLE_PRODUCTS["precipitation"],
        legacy_extent=("2000-06-01", None),
    ),
    "air_temperature": VariableOverlay(
        variable_id="air_temperature",
        label="Air Temperature",
        unit="Celsius",
        products=VARIABLE_PRODUCTS["air_temperature"],
        legacy_extent=("1980-01-01", None),
    ),
}

def overlay_for(variable_id: str) -> VariableOverlay:
    try:
        return VARIABLE_OVERLAY[variable_id]
    except KeyError as exc:
        raise ValueError(f"No product overlay for variable '{variable_id}'.") from exc


def implemented_products(variable_id: str) -> frozenset[str]:
    """Products the collector for ``variable_id`` can read today."""
    overlay = overlay_for(variable_id)
    return overlay.implemented_products or frozenset(overlay.products)
