"""Catalog, selector, and frozen wire-contract tests.

The catalog is generated from a verbatim AppEEARS ``GET /api/product`` dump;
these tests guard the two things that can silently drift:

1. ``products.json`` no longer matching ``data/appeears_products.json``.
2. The curated overlay pointing at a product AppEEARS does not offer.

The API test posts the exact ``yearly + coarse + random_sample`` payload the
frontend sends and asserts that clamping, product choice, composite sampling and
the derived-period warning all survive the round trip.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.catalog import load_catalog
from app.catalog.overlay import VARIABLE_PRODUCTS, implemented_products
from app.catalog.selector import resolve_selection
from app.main import app
from app.services.variables import get_variable_spec
from app.services.variables.base import VariableSeries
from tools.build_catalog import DEFAULT_SOURCE, DEFAULT_TARGET, build

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXACT_PAYLOAD = {
    "variable": "land_surface_temperature",
    "aoi": {"west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7},
    "start_date": "2000-01-01",
    "end_date": "2024-12-31",
    "temporal_resolution": "yearly",
    "spatial_strategy": "coarse",
    "sampling": {"mode": "random_sample", "samples_per_month": 1, "seed": 42, "missing": "keep"},
    "analysis_methods": ["linear_regression", "mann_kendall", "seasonal_mann_kendall"],
}


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #


def test_catalog_matches_the_appeears_snapshot():
    """Regenerating from the snapshot must reproduce the committed catalog."""
    committed = json.loads(DEFAULT_TARGET.read_text(encoding="utf-8"))
    rebuilt = build(DEFAULT_SOURCE)

    assert rebuilt["count"] == committed["count"] == 193
    assert rebuilt["source_sha256"] == committed["source_sha256"]
    assert rebuilt["products"] == committed["products"]


def test_every_curated_product_exists_in_the_catalog():
    catalog = load_catalog()
    for variable, products in VARIABLE_PRODUCTS.items():
        assert products, f"{variable} has no curated products"
        missing = sorted(set(products) - set(catalog))
        assert not missing, f"{variable} references products not in AppEEARS: {missing}"


def test_implemented_products_are_a_subset_of_curated_products():
    for variable, products in VARIABLE_PRODUCTS.items():
        extra = implemented_products(variable) - set(products)
        assert not extra, f"{variable} claims unimplemented products: {sorted(extra)}"


def test_every_implemented_product_has_verified_layers():
    from app.services.variables.land_surface_temperature import DEFAULT_LAYERS

    for product in implemented_products("land_surface_temperature"):
        assert DEFAULT_LAYERS.get(product), f"{product} has no AppEEARS layers"


# --------------------------------------------------------------------------- #
# Selector
# --------------------------------------------------------------------------- #


def _selection(**overrides):
    kwargs = {
        "variable": "land_surface_temperature",
        "start": date(2000, 1, 1),
        "end": date(2024, 12, 31),
        "period": "yearly",
        "strategy": "coarse",
        "sampling_mode": "random_sample",
        "samples_per_month": 1,
    }
    kwargs.update(overrides)
    return resolve_selection(**kwargs)


def test_exact_payload_selects_the_coarsest_composite():
    selection = _selection()

    assert selection.product_name == "MOD11A2"
    assert selection.product_id == "MOD11A2.061"
    meta = selection.metadata()
    assert meta["resolution"]["basis"] == "derived"
    assert meta["resolution"]["source_granularity"] == "8_day"
    assert meta["collection"] == "composite"
    assert meta["sampling_applied"] == "composite_window"
    # Clamped start, then trimmed to whole years so the trend is not skewed.
    assert meta["date_range"]["requested"] == {"start": "2000-01-01", "end": "2024-12-31"}
    assert meta["date_range"]["effective"] == {"start": "2001-01-01", "end": "2024-12-31"}
    assert meta["date_range"]["clamped"] is True
    assert meta["date_range"]["truncated_partial_periods"] is True


def test_every_decision_that_changes_data_is_declared():
    selection = _selection()
    warnings = "\n".join(selection.warnings)

    assert "clipped to 2000-02-18" in warnings
    assert "Partial yearly periods" in warnings
    assert "derived from MOD11A2" in warnings
    assert "composite granularity" in warnings


def test_daily_requests_use_the_daily_product():
    selection = _selection(period="daily", end=date(2024, 1, 31))
    assert selection.product_name == "MOD11A1"
    assert selection.basis == "provider"
    assert selection.metadata()["sampling_applied"] == "random_sample"


def test_sampling_cap_forces_finer_products():
    # 1-2 samples/month can ride on a 16-day composite, 3-4 on 8-day, >4 daily.
    assert _selection(samples_per_month=2).product_name == "MOD11A2"
    assert _selection(samples_per_month=5).product_name == "MOD11A1"


def test_native_strategy_prefers_the_finest_band():
    # ``spatial_strategy`` only chooses the spatial band; granularity is still
    # the coarsest valid one inside that band. Both MODIS LST products are
    # 1000 m, so a yearly native request still reads the 8-day composite.
    selection = _selection(strategy="native")
    assert selection.metadata()["resolution"]["source_resolution"] == "1000m"
    assert selection.product_name == "MOD11A2"

    daily = _selection(strategy="native", period="daily", end=date(2024, 1, 31))
    assert daily.product_name == "MOD11A1"


def test_full_period_monthly_uses_the_composite_without_sampling():
    selection = _selection(
        end=date(2024, 12, 31),
        period="monthly",
        sampling_mode="full_period",
    )
    assert selection.product_name == "MOD11A2"
    assert selection.metadata()["collection"] == "composite"
    assert selection.metadata()["sampling_applied"] == "full_period"


def test_range_without_overlap_is_rejected():
    with pytest.raises(ValueError, match="no overlap"):
        _selection(start=date(1990, 1, 1), end=date(1999, 12, 31))


def test_lst_collector_submits_one_composite_range_per_sample(monkeypatch, tmp_path):
    from app.services.variables import land_surface_temperature as lst
    from app.services.variables.base import CollectionRequest

    captured = {}

    def fake_collect(config):
        captured["config"] = config
        return {"records": [], "warnings": []}

    monkeypatch.setattr(lst, "collect_mod11a1_timeseries", fake_collect)
    days = tuple(f"2024-{month:02d}-01" for month in range(1, 13))

    lst.collect(
        CollectionRequest(
            aoi={"west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7},
            start_date="2024-01-01",
            end_date="2024-12-31",
            output_dir=tmp_path,
            temporal_resolution="yearly",
            sampling_mode="random_sample",
            collection_product="MOD11A2",
            sample_days=days,
        )
    )

    config = captured["config"]
    assert config.product_short_name == "MOD11A2"
    assert config.sampling_unit_days == 8
    assert config.layers == ("LST_Day_1km", "QC_Day")
    # One aligned 8-day AppEEARS range per sampled composite start.
    assert len(config.date_ranges) == 12
    assert config.date_ranges[0] == ("2024-01-01", "2024-01-08")


def test_legacy_variables_still_clamp_against_a_real_product():
    selection = _selection(variable="ndvi", period="yearly", sampling_mode="full_period")
    assert selection.product_name is None
    assert selection.metadata()["date_range"]["effective"]["start"] == "2001-01-01"


# --------------------------------------------------------------------------- #
# Wire contract (inline queue, no network)
# --------------------------------------------------------------------------- #


def _yearly_records() -> list[dict]:
    return [
        {
            "observation_date": f"{year}-01-01",
            "value": 20.0 + (year - 2001) * 0.05,
            "valid_pixel_count": 100,
            "total_pixel_count": 100,
            "valid_data_percentage": 100.0,
            "missing": False,
            "missing_reason": None,
            "granules_used": [f"{year}-01"],
            "coverage_status": "complete",
            "source_frequency": "8-day",
        }
        for year in range(2001, 2025)
    ]


def _fake_collect(records, captured: dict | None = None):
    def collect(variable_id, *args, **kwargs):
        if captured is not None:
            captured.update(kwargs)
            captured["variable_id"] = variable_id
            for name, value in zip(("aoi", "start_date", "end_date", "output_dir"), args):
                captured[name] = value
        spec = get_variable_spec(variable_id)
        return VariableSeries(
            spec=spec,
            records=records,
            source_products=["MOD11A2.061"],
            source_resolution="1000m",
            collection_strategy="composite",
        )

    return collect


def test_exact_frontend_payload_round_trip(monkeypatch, tmp_path):
    captured: dict = {}
    monkeypatch.setattr("app.jobs.queue.QUEUE_MODE", "inline")
    monkeypatch.setattr("app.jobs.store.JOBS_DIR", tmp_path)
    monkeypatch.setattr("app.services.trend_service.JOBS_DIR", tmp_path)
    monkeypatch.setattr(
        "app.services.trend_service.collect_chunked_variable",
        _fake_collect(_yearly_records(), captured),
    )

    client = TestClient(app)
    response = client.post("/api/v1/analysis/trend", json=EXACT_PAYLOAD)
    assert response.status_code == 202, response.text

    # The selector's product and clamped range must reach the collector.
    assert captured["collection_product"] == "MOD11A2"
    assert captured["start_date"] == "2001-01-01"
    assert captured["end_date"] == "2024-12-31"

    result = client.get(response.json()["result_url"])
    assert result.status_code == 200, result.text
    body = result.json()

    assert body["success"] is True
    meta = body["metadata"]
    assert meta["start_date"] == "2001-01-01"
    assert meta["end_date"] == "2024-12-31"
    assert meta["product"] == "MOD11A2"
    assert meta["resolution"]["source_granularity"] == "8_day"
    assert meta["sampling_applied"] == "composite_window"
    assert meta["date_range"]["clamped"] is True

    # Seasonal Mann-Kendall cannot run on one observation per year; the
    # unsupported status must be declared, never silently dropped.
    assert body["analysis_results"]["seasonal_mann_kendall"]["status"] == "not_supported"
    assert any("Seasonal Mann-Kendall" in warning for warning in body["warnings"])
    assert any("Partial yearly periods" in warning for warning in body["warnings"])
