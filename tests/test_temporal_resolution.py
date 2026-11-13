"""Temporal resolution, provider monthly products, and random sampling."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import variables as variable_registry
from app.services.temporal import (
    aggregate_records,
    monthly_periods,
    period_chunks,
    sample_day_plan,
    select_sampled_records,
)
from app.services.variables import collect_resolved_variable, get_variable_spec
from app.services.variables.base import CollectionRequest, VariableSeries


def _daily_records(start: str, end: str, value: float = 10.0) -> list[dict]:
    current = date.fromisoformat(start)
    finish = date.fromisoformat(end)
    records = []
    while current <= finish:
        records.append(
            {
                "observation_date": current.isoformat(),
                "value": value,
                "unit": "Celsius",
                "valid_pixel_count": 100,
                "total_pixel_count": 100,
                "valid_data_percentage": 100.0,
                "missing": False,
                "missing_reason": None,
                "granules_used": [],
                "coverage_status": "complete",
                "source_frequency": "daily",
            }
        )
        current += timedelta(days=1)
    return records


def _request(**overrides) -> CollectionRequest:
    payload = dict(
        aoi={"west": 31.0, "south": 29.0, "east": 32.0, "north": 30.0},
        start_date="2024-01-01",
        end_date="2024-12-31",
        output_dir="unused",
    )
    payload.update(overrides)
    return CollectionRequest(**payload)


def test_monthly_periods_cover_partial_first_and_last_month():
    assert monthly_periods("2024-01-15", "2024-03-10") == ["2024-01", "2024-02", "2024-03"]


def test_period_chunks_never_split_a_month():
    chunks = period_chunks("2020-06-15", "2022-02-10", max_years=1)
    assert chunks[0] == ("2020-06-15", "2020-12-31")
    assert chunks[1] == ("2021-01-01", "2021-12-31")
    assert chunks[-1] == ("2022-01-01", "2022-02-10")


def test_aggregate_daily_to_monthly_and_yearly():
    records = _daily_records("2024-01-01", "2024-02-29", value=10.0)
    spec = get_variable_spec("land_surface_temperature")

    monthly = aggregate_records(records, spec, "monthly")
    assert [record["observation_date"] for record in monthly] == ["2024-01-01", "2024-02-01"]
    assert monthly[0]["value"] == pytest.approx(10.0)
    assert monthly[0]["source_frequency"] == "monthly"

    yearly = aggregate_records(monthly, spec, "yearly")
    assert len(yearly) == 1
    assert yearly[0]["observation_date"] == "2024-01-01"
    assert yearly[0]["value"] == pytest.approx(10.0)


def test_provider_monthly_to_yearly_is_weighted_by_month_length():
    spec = get_variable_spec("land_surface_temperature")
    monthly = [
        {
            "observation_date": "2024-01-01",
            "value": 10.0,
            "valid_pixel_count": 100,
            "total_pixel_count": 100,
            "missing": False,
            "source_frequency": "monthly",
        },
        {
            "observation_date": "2024-02-01",
            "value": 20.0,
            "valid_pixel_count": 100,
            "total_pixel_count": 100,
            "missing": False,
            "source_frequency": "monthly",
        },
    ]
    yearly = aggregate_records(monthly, spec, "yearly")
    expected = (10.0 * 31 + 20.0 * 29) / 60  # 2024 is a leap year: February has 29 days
    assert yearly[0]["value"] == pytest.approx(expected)
    assert yearly[0]["source_frequency"] == "yearly"


def test_sample_plan_is_deterministic_and_stays_inside_each_month():
    first = sample_day_plan("2024-01-01", "2024-03-31", samples_per_month=2, seed=7)
    second = sample_day_plan("2024-01-01", "2024-03-31", samples_per_month=2, seed=7)
    other = sample_day_plan("2024-01-01", "2024-03-31", samples_per_month=2, seed=8)

    assert first == second
    assert first != other
    for period, days in first.items():
        assert len(days) == 2
        assert all(day.startswith(period) for day in days)
        assert len(set(days)) == len(days)


def test_select_sampled_records_keep_and_redraw():
    plan = {"2024-01": ["2024-01-05", "2024-01-20", "2024-01-11"]}
    records = [
        {"observation_date": "2024-01-05", "value": None, "missing": True},
        {"observation_date": "2024-01-20", "value": None, "missing": True},
        {"observation_date": "2024-01-11", "value": 7.0, "missing": False},
    ]
    kept = select_sampled_records(records, plan, samples_per_month=1, missing_policy="keep")
    assert kept[0]["observation_date"] == "2024-01-05"
    assert kept[0]["missing"] is True

    redrawn = select_sampled_records(records, plan, samples_per_month=1, missing_policy="redraw")
    assert redrawn[0]["observation_date"] == "2024-01-11"
    assert redrawn[0]["value"] == 7.0


def test_resolver_uses_provider_monthly_product_for_coarse(monkeypatch):
    spec = get_variable_spec("land_surface_temperature")
    captured = {}

    def fake_period(request, spec_arg, source):
        captured["source"] = source.short_name
        return VariableSeries(
            spec=spec_arg,
            records=[
                {
                    "observation_date": "2024-01-01",
                    "value": 15.0,
                    "valid_pixel_count": 10,
                    "total_pixel_count": 10,
                    "missing": False,
                    "source_frequency": "monthly",
                }
            ],
            frequency="monthly",
            collection_strategy="provider_monthly",
            source_products=[source.short_name],
            source_resolution=source.native_resolution,
        )

    monkeypatch.setattr(variable_registry, "collect_period_variable", fake_period)
    series = collect_resolved_variable(
        "land_surface_temperature",
        _request(temporal_resolution="monthly", spatial_strategy="coarse"),
    )

    assert captured["source"] == "MOD11C3"
    assert series.collection_strategy == "provider_monthly"
    assert series.frequency == "monthly"
    assert series.source_products == ["MOD11C3"]
    assert spec is not None


def test_resolver_falls_back_to_random_sampling_without_monthly_product(monkeypatch):
    spec, _ = variable_registry._REGISTRY["soil_moisture"]
    monkeypatch.setitem(
        variable_registry._REGISTRY,
        "soil_moisture",
        (spec, lambda request: VariableSeries(spec=spec, records=_daily_records(request.start_date, request.end_date))),
    )

    series = collect_resolved_variable(
        "soil_moisture",
        _request(temporal_resolution="monthly", spatial_strategy="coarse", sampling_seed=3),
    )

    assert series.collection_strategy == "sampled"
    assert series.frequency == "monthly"
    assert len(series.records) == 12
    assert series.sampling["seed"] == 3


def test_resolver_native_aggregates_daily_records(monkeypatch):
    spec, _ = variable_registry._REGISTRY["air_temperature"]
    monkeypatch.setitem(
        variable_registry._REGISTRY,
        "air_temperature",
        (spec, lambda request: VariableSeries(spec=spec, records=_daily_records(request.start_date, request.end_date))),
    )

    series = collect_resolved_variable(
        "air_temperature",
        _request(temporal_resolution="monthly", spatial_strategy="native"),
    )

    assert series.collection_strategy == "native_aggregated"
    assert [record["observation_date"] for record in series.records] == [
        "2024-01-01",
        "2024-02-01",
        "2024-03-01",
        "2024-04-01",
        "2024-05-01",
        "2024-06-01",
        "2024-07-01",
        "2024-08-01",
        "2024-09-01",
        "2024-10-01",
        "2024-11-01",
        "2024-12-01",
    ]


def _payload(**overrides):
    payload = {
        "aoi": {"west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7},
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "temporal_resolution": "monthly",
        "analysis_methods": ["linear_regression"],
    }
    payload.update(overrides)
    return payload


def test_api_rejects_sampling_for_daily_requests():
    client = TestClient(app)
    response = client.post(
        "/api/v1/analysis/trend",
        json=_payload(temporal_resolution="daily", sampling={"mode": "random_day_per_month"}),
    )
    assert response.status_code == 400
    assert "monthly or yearly" in response.json()["detail"]


def test_api_rejects_native_strategy_for_daily_requests():
    client = TestClient(app)
    response = client.post(
        "/api/v1/analysis/trend",
        json=_payload(temporal_resolution="daily", spatial_strategy="native"),
    )
    assert response.status_code == 400
    assert "spatial_strategy" in response.json()["detail"]


def test_api_derives_a_reproducible_sampling_seed(monkeypatch, tmp_path):
    from app.services import trend_service

    captured = {}

    def fake_collect(*args, **kwargs):
        captured.update(kwargs)
        spec = get_variable_spec("soil_moisture")
        return VariableSeries(
            spec=spec,
            records=_daily_records("2024-01-01", "2024-12-31"),
            frequency="monthly",
            collection_strategy="sampled",
            sampling={"mode": "random_day_per_month", "seed": kwargs.get("sampling_seed")},
        )

    monkeypatch.setattr("app.jobs.queue.QUEUE_MODE", "inline")
    monkeypatch.setattr("app.jobs.store.JOBS_DIR", tmp_path)
    monkeypatch.setattr(trend_service, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(trend_service, "collect_chunked_variable", fake_collect)

    client = TestClient(app)
    response = client.post(
        "/api/v1/analysis/trend",
        json=_payload(
            variable="soil_moisture",
            temporal_resolution="monthly",
            sampling={"mode": "random_day_per_month"},
        ),
    )
    assert response.status_code == 202, response.text
    client.get(response.json()["status_url"])
    assert isinstance(captured.get("sampling_seed"), int)

    body = client.get(response.json()["result_url"]).json()
    assert body["metadata"]["sampling"]["mode"] == "random_day_per_month"
    assert body["metadata"]["sampling"]["seed"] == captured["sampling_seed"]

    # The same request must produce the same seed.
    assert body["metadata"]["sampling"]["seed"] == captured["sampling_seed"]


def test_comparison_accepts_monthly_resolution(monkeypatch, tmp_path):
    from app.services import comparison_service

    monkeypatch.setattr("app.jobs.queue.QUEUE_MODE", "inline")
    monkeypatch.setattr("app.jobs.store.JOBS_DIR", tmp_path)
    monkeypatch.setattr(comparison_service, "JOBS_DIR", tmp_path)

    def fake_collect(variable_id, aoi, start_date, end_date, output_dir, **kwargs):
        spec = get_variable_spec(variable_id)
        return VariableSeries(spec=spec, records=_daily_records(start_date, end_date), frequency="monthly")

    monkeypatch.setattr(comparison_service, "collect_chunked_variable", fake_collect)

    client = TestClient(app)
    response = client.post(
        "/api/v1/analysis/compare/variables",
        json={
            "aoi": {"west": 31.0, "south": 29.0, "east": 32.0, "north": 30.0},
            "start_date": "2024-01-01",
            "end_date": "2024-03-31",
            "temporal_resolution": "monthly",
            "variables": ["ndvi", "air_temperature"],
            "analysis_methods": ["linear_regression"],
            "include_visualizations": False,
        },
    )
    assert response.status_code == 202, response.text
    body = client.get(response.json()["result_url"]).json()
    assert body["metadata"]["temporal_resolution"] == "monthly"
    assert body["metadata"]["spatial_strategy"] == "coarse"
