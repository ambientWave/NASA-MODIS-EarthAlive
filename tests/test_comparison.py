from __future__ import annotations

import base64
from datetime import date, timedelta

import pytest

from app.services import comparison_service
from app.services.analysis_engine import run_series_analysis, to_stats_records
from app.services.variables import SUPPORTED_VARIABLES, get_variable_spec
from app.services.variables.base import VariableSeries
from app.services.variables.remote_grid import date_chunks


def _fake_series(variable_id: str, start_date: str, end_date: str) -> VariableSeries:
    spec = get_variable_spec(variable_id)
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    records = []
    current = start
    index = 0
    while current <= end:
        value = 10.0 + index * 0.05
        records.append(
            {
                "observation_date": current.isoformat(),
                "value": value,
                "unit": spec.unit,
                "valid_pixel_count": 100,
                "total_pixel_count": 100,
                "valid_data_percentage": 100.0,
                "missing": False,
                "missing_reason": None,
                "granules_used": [],
                "coverage_status": "complete",
                "source_frequency": spec.native_resolution,
            }
        )
        current += timedelta(days=1)
        index += 1
    return VariableSeries(spec=spec, records=records)


def test_registry_contains_requested_variables():
    assert SUPPORTED_VARIABLES == {
        "land_surface_temperature",
        "ndvi",
        "precipitation",
        "soil_moisture",
        "air_temperature",
    }
    assert get_variable_spec("ndvi").native_resolution == "16-day"


def test_to_stats_records_accepts_generic_value_field():
    records = [
        {"observation_date": "2024-01-01", "value": 1.2, "valid_pixel_count": 10, "total_pixel_count": 10, "missing": False},
        {"observation_date": "2024-01-02", "value": None, "valid_pixel_count": 0, "total_pixel_count": 10, "missing": True},
    ]
    stats = to_stats_records(records)
    assert stats[0]["mean_lst"] == pytest.approx(1.2)
    assert stats[1]["mean_lst"] is None
    assert stats[1]["missing"] is True


def test_run_series_analysis_renders_base64_visualizations():
    records = _fake_series("air_temperature", "2024-01-01", "2024-03-01").records
    result = run_series_analysis(
        records,
        ["linear_regression", "mann_kendall"],
        {"west": 31.0, "south": 29.0, "east": 32.0, "north": 30.0},
        product="M2SDNXSLV",
        unit="Celsius",
        label="Air temperature",
    )
    assert result["status"] == "calculated"
    assert set(result["visualizations"]) == {"linear_regression", "mann_kendall"}
    for chart in result["visualizations"].values():
        assert chart["content_type"] == "image/png"
        assert base64.b64decode(chart["base64"]).startswith(b"\x89PNG")


def test_date_chunks_are_inclusive_and_bounded():
    chunks = date_chunks("2020-01-01", "2020-01-10", 4)
    assert chunks == [
        ("2020-01-01", "2020-01-04"),
        ("2020-01-05", "2020-01-08"),
        ("2020-01-09", "2020-01-10"),
    ]


def test_execute_variable_comparison_merges_and_analyses(monkeypatch, tmp_path):
    monkeypatch.setattr(comparison_service, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(comparison_service, "collect_variable", lambda variable_id, request: _fake_series(variable_id, request.start_date, request.end_date))
    monkeypatch.setattr(comparison_service, "MAX_DAYS_PER_DAILY_JOB", 10)

    result = comparison_service.execute_variable_comparison(
        {
            "aoi": {"west": 31.0, "south": 29.0, "east": 32.0, "north": 30.0},
            "start_date": "2024-01-01",
            "end_date": "2024-01-20",
            "temporal_resolution": "daily",
            "variables": ["land_surface_temperature", "ndvi"],
            "analysis_methods": ["linear_regression", "mann_kendall"],
            "include_visualizations": True,
        },
        "job1",
    )

    assert set(result["variables"]) == {"land_surface_temperature", "ndvi"}
    assert len(result["variables"]["land_surface_temperature"]["series"]) == 20
    assert "linear_regression" in result["variables"]["land_surface_temperature"]["visualizations"]
    assert set(result["comparison"]["visualizations"]) >= {"timeseries_comparison", "trend_summary", "coverage_comparison"}
    for chart in result["comparison"]["visualizations"].values():
        assert base64.b64decode(chart["base64"]).startswith(b"\x89PNG")


def test_execute_region_comparison_uses_region_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(comparison_service, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(comparison_service, "collect_variable", lambda variable_id, request: _fake_series(variable_id, request.start_date, request.end_date))

    result = comparison_service.execute_region_comparison(
        {
            "variable": "land_surface_temperature",
            "regions": [
                {"west": 31.0, "south": 29.0, "east": 31.5, "north": 29.5},
                {"west": 31.5, "south": 29.0, "east": 32.0, "north": 29.5},
            ],
            "start_date": "2024-01-01",
            "end_date": "2024-02-01",
            "temporal_resolution": "daily",
            "analysis_methods": ["linear_regression"],
            "include_visualizations": False,
        },
        "job2",
    )

    assert set(result["regions"]) == {"region_1", "region_2"}
    assert result["regions"]["region_1"]["aoi"]["west"] == 31.0
    assert result["comparison"]["visualizations"] == {}
