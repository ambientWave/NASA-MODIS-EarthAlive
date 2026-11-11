from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from app.services.modis_timeseries import (
    GranuleCache,
    TimeSeriesConfig,
    aggregate_period_records,
    build_missing_observation_record,
    collect_mod11a1_timeseries,
    date_range,
    save_timeseries_outputs,
    summarize_temperature_array,
)


def test_date_range_is_inclusive_and_sorted():
    dates = date_range("2024-01-01", "2024-01-03")
    assert dates == ["2024-01-01", "2024-01-02", "2024-01-03"]


def test_missing_observation_record_keeps_date_and_nulls():
    record = build_missing_observation_record("2024-01-02", "No granules found")
    assert record["observation_date"] == "2024-01-02"
    assert record["mean_temperature_celsius"] is None
    assert record["minimum_temperature_celsius"] is None
    assert record["valid_pixels"] == 0
    assert record["missing_observation"] is True
    assert record["missing_reason"] == "No granules found"


def test_summarize_temperature_array_counts_valid_pixels_only():
    array = np.array(
        [
            [20.0, np.nan, 25.0],
            [np.nan, 30.0, 35.0],
        ],
        dtype=np.float32,
    )
    summary = summarize_temperature_array(array)

    assert summary["valid_pixels"] == 4
    assert summary["total_pixels"] == 6
    assert summary["mean_temperature_celsius"] == pytest.approx(27.5)
    assert summary["minimum_temperature_celsius"] == pytest.approx(20.0)
    assert summary["maximum_temperature_celsius"] == pytest.approx(35.0)
    assert summary["valid_data_percentage"] == pytest.approx((4 / 6) * 100.0)


def test_granule_cache_reuses_identical_granules(tmp_path):
    cache = GranuleCache(tmp_path / "granule_cache.json")
    granule = {"umm": {"GranuleUR": "MOD11A1.A2024001.h20v05.061.2024005033647"}}

    assert cache.is_cached(granule) is False

    dummy_path = tmp_path / "dummy.hdf"
    dummy_path.write_text("placeholder", encoding="utf-8")
    cache.mark_downloaded(granule, dummy_path)

    assert cache.is_cached(granule) is True

    duplicate = {"umm": {"GranuleUR": "MOD11A1.A2024001.h20v05.061.2024005033647"}}
    assert cache.is_cached(duplicate) is True


def test_aggregate_period_records_groups_monthly_data():
    records = [
        {"observation_date": "2024-01-01", "mean_temperature_celsius": 10.0, "minimum_temperature_celsius": 8.0, "maximum_temperature_celsius": 12.0, "valid_pixels": 80, "total_pixels": 100, "missing_observation": False, "granules_used": ["g1"]},
        {"observation_date": "2024-01-03", "mean_temperature_celsius": 12.0, "minimum_temperature_celsius": 10.0, "maximum_temperature_celsius": 14.0, "valid_pixels": 20, "total_pixels": 100, "missing_observation": False, "granules_used": ["g2"]},
    ]

    aggregated = aggregate_period_records(records, "monthly")
    assert len(aggregated) == 1
    assert aggregated[0]["observation_date"] == "2024-01"
    assert aggregated[0]["valid_pixels"] == 100
    assert aggregated[0]["mean_temperature_celsius"] == pytest.approx(10.4)


def test_save_timeseries_outputs_creates_csv_and_json(tmp_path):
    records = [
        {"observation_date": "2024-01-01", "mean_temperature_celsius": 12.5, "minimum_temperature_celsius": 10.0, "maximum_temperature_celsius": 15.0, "valid_pixels": 100, "total_pixels": 200, "valid_data_percentage": 50.0, "missing_observation": False, "missing_reason": None, "granules_used": ["g1"], "coverage_status": "complete"},
        {"observation_date": "2024-01-02", "mean_temperature_celsius": None, "minimum_temperature_celsius": None, "maximum_temperature_celsius": None, "valid_pixels": 0, "total_pixels": 0, "valid_data_percentage": 0.0, "missing_observation": True, "missing_reason": "No granules found", "granules_used": [], "coverage_status": "missing"},
    ]

    output_files = save_timeseries_outputs(records, tmp_path, "egypt", "daily")
    assert Path(output_files["csv"]).exists()
    assert Path(output_files["json"]).exists()

    with open(output_files["csv"], newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["observation_date"] == "2024-01-01"
    assert rows[1]["missing_observation"] == "True"

    with open(output_files["json"], encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload[1]["missing_observation"] is True


@pytest.mark.integration
def test_egypt_short_date_timeseries_integration(tmp_path):
    config = TimeSeriesConfig(
        aoi={"west": 29.5, "south": 29.5, "east": 31.5, "north": 31.5},
        start_date="2024-01-01",
        end_date="2024-01-03",
        product_short_name="MOD11A1",
        temporal_aggregation="daily",
        output_dir=tmp_path / "egypt_timeseries",
        cache_path=tmp_path / "cache" / "modis_granule_cache.json",
        region_name="egypt",
    )

    result = collect_mod11a1_timeseries(config)

    assert result["product_short_name"] == "MOD11A1"
    assert result["temporal_aggregation"] == "daily"
    assert len(result["records"]) == 3
    assert result["output_files"]["csv"].endswith("egypt_modis_timeseries_daily.csv")
    assert Path(result["output_files"]["csv"]).exists()
    assert Path(result["output_files"]["json"]).exists()

    records = result["records"]
    assert records[0]["observation_date"] == "2024-01-01"
    assert all("observation_date" in record for record in records)
    assert any(record.get("missing_observation") is False for record in records)
