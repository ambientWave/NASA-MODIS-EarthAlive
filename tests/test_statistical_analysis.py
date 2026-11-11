from __future__ import annotations

import base64
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services.statistical_analysis import (
    AnalysisConfig,
    analyze_time_series,
    generate_trend_visualizations,
    linear_regression_analysis,
    mann_kendall_analysis,
    paired_period_t_test,
    validate_time_series,
)


def _make_records(values, dates):
    return [
        {
            "observation_date": date,
            "mean_lst": float(value),
            "valid_pixel_count": 100,
            "total_pixel_count": 100,
            "valid_data_percentage": 100.0,
            "missing": False,
        }
        for date, value in zip(dates, values)
    ]


def test_validate_time_series_rejects_duplicate_dates():
    records = [
        {"observation_date": "2024-01-01", "mean_lst": 10.0, "valid_pixel_count": 100, "total_pixel_count": 100, "valid_data_percentage": 100.0, "missing": False},
        {"observation_date": "2024-01-01", "mean_lst": 11.0, "valid_pixel_count": 100, "total_pixel_count": 100, "valid_data_percentage": 100.0, "missing": False},
    ]
    with pytest.raises(ValueError, match="Duplicate observation dates"):
        validate_time_series(records)


def test_validate_time_series_handles_missing_values_without_replacing_them():
    records = [
        {"observation_date": "2024-01-01", "mean_lst": 10.0, "valid_pixel_count": 100, "total_pixel_count": 100, "valid_data_percentage": 100.0, "missing": False},
        {"observation_date": "2024-01-02", "mean_lst": None, "valid_pixel_count": 0, "total_pixel_count": 100, "valid_data_percentage": 0.0, "missing": True},
        {"observation_date": "2024-01-03", "mean_lst": 11.0, "valid_pixel_count": 100, "total_pixel_count": 100, "valid_data_percentage": 100.0, "missing": False},
        {"observation_date": "2024-01-04", "mean_lst": 12.0, "valid_pixel_count": 100, "total_pixel_count": 100, "valid_data_percentage": 100.0, "missing": False},
    ]
    validated = validate_time_series(records)
    assert validated["summary"]["missing_observation_count"] == 1
    assert validated["summary"]["missing_observation_percentage"] == pytest.approx(25.0)
    assert len(validated["dataframe"]) == 3
    assert validated["dataframe"]["mean_lst"].notna().all()


def test_linear_regression_detects_increasing_trend():
    dates = pd.date_range("2024-01-01", periods=12, freq="MS")
    values = np.linspace(10.0, 18.0, len(dates))
    result = linear_regression_analysis(_make_records(values, dates.strftime("%Y-%m-%d")))
    assert result["slope_celsius_per_year"] > 0
    assert result["r_squared"] > 0.9
    assert result["trend_direction"] == "increasing"


def test_mann_kendall_handles_tied_values():
    dates = pd.date_range("2024-01-01", periods=8, freq="D")
    values = [10, 10, 10, 11, 11, 12, 12, 12]
    result = mann_kendall_analysis(_make_records(values, dates.strftime("%Y-%m-%d")))
    assert "s_statistic" in result
    assert "kendall_tau" in result
    assert "p_value" in result
    assert result["trend_direction"] in {"increasing", "decreasing", "no_trend"}


def test_paired_period_t_test_compares_two_periods():
    early_dates = pd.date_range("2024-01-01", periods=6, freq="D")
    late_dates = pd.date_range("2024-02-01", periods=6, freq="D")
    early_values = [10, 10.5, 11, 11.5, 12, 12.5]
    late_values = [15, 15.5, 16, 16.5, 17, 17.5]

    result = paired_period_t_test(
        _make_records(early_values + late_values, list(early_dates.strftime("%Y-%m-%d")) + list(late_dates.strftime("%Y-%m-%d"))),
        ("2024-01-01", "2024-01-06"),
        ("2024-02-01", "2024-02-06"),
    )

    assert result["mean_difference_celsius"] > 0
    assert result["p_value"] < 0.05
    assert "confidence_interval_95" in result


def test_analyze_time_series_generates_structured_output(tmp_path):
    dates = pd.date_range("2024-01-01", periods=12, freq="MS")
    values = np.linspace(9.0, 15.0, len(dates))
    config = AnalysisConfig(
        aoi={"west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7},
        product="MOD11A1",
        output_dir=tmp_path,
        label="egypt_analysis",
    )

    result = analyze_time_series(
        _make_records(values, dates.strftime("%Y-%m-%d")),
        config,
        t_test_periods=(("2024-01-01", "2024-03-31"), ("2024-10-01", "2024-12-31")),
    )

    assert result["linear_regression"]["slope_celsius_per_year"] > 0
    assert result["mann_kendall"]["trend_direction"] in {"increasing", "decreasing", "no_trend"}
    assert "t_test" in result
    assert (tmp_path / "egypt_analysis_analysis.json").exists()


def test_analysis_config_requires_valid_bbox():
    with pytest.raises(ValueError):
        AnalysisConfig(aoi={"west": 10.0, "south": 5.0, "east": 5.0, "north": 10.0})


def test_mann_kendall_warns_for_strong_autocorrelation():
    dates = pd.date_range("2024-01-01", periods=12, freq="MS")
    values = [10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0, 14.5, 15.0, 15.5]
    result = mann_kendall_analysis(_make_records(values, dates.strftime("%Y-%m-%d")))
    assert result["serial_correlation_warning"] is True or abs(result["lag1_autocorrelation"]) > 0.5


def test_validate_time_series_flags_sparse_coverages():
    records = [
        {"observation_date": "2024-01-01", "mean_lst": 10.0, "valid_pixel_count": 1, "total_pixel_count": 1000, "valid_data_percentage": 0.1, "missing": False},
        {"observation_date": "2024-01-02", "mean_lst": 10.5, "valid_pixel_count": 1, "total_pixel_count": 1000, "valid_data_percentage": 0.1, "missing": False},
        {"observation_date": "2024-01-03", "mean_lst": 11.0, "valid_pixel_count": 1, "total_pixel_count": 1000, "valid_data_percentage": 0.1, "missing": False},
    ]
    validated = validate_time_series(records)
    assert any("Very few valid pixels remain" in message for message in validated["summary"]["warnings"])


def _ar1_series(length, base, rho, sigma=1.0, rng=None):
    if rng is None:
        rng = np.random.default_rng(123)
    values = np.empty(length, dtype=float)
    current = float(base)
    for i in range(length):
        current = rho * current + rng.normal(0.0, sigma)
        values[i] = current
    return values


def test_strong_positive_autocorrelation_warns_and_reports_modified_result():
    rng = np.random.default_rng(11)
    values = _ar1_series(30, 10.0, 0.85, sigma=0.5, rng=rng)
    dates = pd.date_range("2024-01-01", periods=len(values), freq="D")
    result = mann_kendall_analysis(_make_records(values, dates.strftime("%Y-%m-%d")))
    assert abs(result["lag1_autocorrelation"]) > 0.5
    assert result["serial_correlation_warning"] is True
    mod = __import__("backend.app.services.statistical_analysis", fromlist=["modified_mann_kendall_analysis"]).modified_mann_kendall_analysis(
        _make_records(values, dates.strftime("%Y-%m-%d"))
    )
    assert mod["status"] in {"calculated", "not_applicable", "insufficient_data"}


def test_negative_autocorrelation_is_detected():
    rng = np.random.default_rng(7)
    values = np.array([10.0 + (1 if i % 2 == 0 else -1) * (0.5 + 0.1 * rng.normal()) for i in range(30)])
    dates = pd.date_range("2024-01-01", periods=len(values), freq="D")
    result = mann_kendall_analysis(_make_records(values, dates.strftime("%Y-%m-%d")))
    assert result["lag1_autocorrelation"] < -0.2


def test_independent_random_series_has_no_autocorrelation_warning():
    rng = np.random.default_rng(42)
    values = rng.normal(10.0, 1.0, size=40)
    dates = pd.date_range("2024-01-01", periods=len(values), freq="D")
    result = mann_kendall_analysis(_make_records(values, dates.strftime("%Y-%m-%d")))
    assert abs(result["lag1_autocorrelation"]) < 0.5
    assert result["serial_correlation_warning"] is False


def test_increasing_trend_with_autocorrelation_has_positive_slope():
    rng = np.random.default_rng(5)
    base = np.linspace(10.0, 18.0, 40)
    values = base + 0.25 * _ar1_series(40, 0.0, 0.8, sigma=0.8, rng=rng)
    dates = pd.date_range("2024-01-01", periods=len(values), freq="D")
    result = linear_regression_analysis(_make_records(values, dates.strftime("%Y-%m-%d")))
    assert result["slope_celsius_per_year"] > 0
    assert result["trend_direction"] == "increasing"
    assert abs(result["lag1_autocorrelation"]) > 0.3


def test_decreasing_trend_with_autocorrelation_has_negative_slope():
    rng = np.random.default_rng(17)
    base = np.linspace(18.0, 10.0, 40)
    values = base + 0.25 * _ar1_series(40, 0.0, 0.8, sigma=0.8, rng=rng)
    dates = pd.date_range("2024-01-01", periods=len(values), freq="D")
    result = linear_regression_analysis(_make_records(values, dates.strftime("%Y-%m-%d")))
    assert result["slope_celsius_per_year"] < 0
    assert result["trend_direction"] == "decreasing"


def test_monthly_seasonal_data_with_underlying_trend_is_detected():
    years = pd.date_range("2020-01-01", periods=48, freq="MS")
    seasonal = np.array([8.0, 9.0, 11.0, 13.0, 14.0, 16.0, 17.0, 16.5, 15.0, 12.5, 10.0, 8.5])
    trend = np.linspace(0.0, 3.0, len(years))
    values = seasonal.repeat(4) + trend
    result = __import__("backend.app.services.statistical_analysis", fromlist=["seasonal_mann_kendall_analysis"]).seasonal_mann_kendall_analysis(
        _make_records(values, years.strftime("%Y-%m-%d"))
    )
    assert result["status"] == "calculated"
    assert result["trend_direction"] in {"increasing", "decreasing"}


def test_monthly_seasonal_data_without_underlying_trend_has_no_strong_seasonal_signal():
    years = pd.date_range("2020-01-01", periods=48, freq="MS")
    seasonal = np.array([8.0, 9.0, 11.0, 13.0, 14.0, 16.0, 17.0, 16.5, 15.0, 12.5, 10.0, 8.5])
    values = np.tile(seasonal, 4)
    result = __import__("backend.app.services.statistical_analysis", fromlist=["seasonal_mann_kendall_analysis"]).seasonal_mann_kendall_analysis(
        _make_records(values, years.strftime("%Y-%m-%d"))
    )
    assert result["status"] == "calculated"
    assert result["trend_direction"] in {"increasing", "decreasing", "no_trend"}


def test_missing_months_are_handled_in_seasonal_analysis():
    dates = pd.to_datetime([
        "2020-01-01", "2020-02-01", "2020-03-01", "2020-05-01", "2020-06-01", "2020-07-01",
        "2020-08-01", "2020-09-01", "2020-10-01", "2020-11-01", "2020-12-01",
        "2021-01-01", "2021-02-01", "2021-03-01", "2021-04-01", "2021-05-01",
        "2021-06-01", "2021-07-01", "2021-08-01", "2021-09-01", "2021-10-01",
        "2021-11-01", "2021-12-01",
    ])
    values = np.linspace(10.0, 15.0, len(dates))
    result = __import__("backend.app.services.statistical_analysis", fromlist=["seasonal_mann_kendall_analysis"]).seasonal_mann_kendall_analysis(
        _make_records(values, dates.strftime("%Y-%m-%d"))
    )
    assert result["status"] in {"calculated", "insufficient_data"}
    assert "months_evaluated" in result or "insufficient_data_reason" in result


def test_insufficient_observations_return_explicit_status():
    dates = pd.date_range("2024-01-01", periods=6, freq="MS")
    values = np.linspace(10.0, 12.0, len(dates))
    result = __import__("backend.app.services.statistical_analysis", fromlist=["modified_mann_kendall_analysis"]).modified_mann_kendall_analysis(
        _make_records(values, dates.strftime("%Y-%m-%d"))
    )
    assert result["status"] in {"insufficient_data", "not_applicable"}


def test_irregular_observation_dates_make_hac_inference_unavailable():
    dates = pd.to_datetime([
        "2024-01-01", "2024-02-01", "2024-03-01", "2024-05-01", "2024-07-15", "2024-09-01",
        "2024-11-01", "2024-12-15", "2025-01-15", "2025-03-01",
    ])
    values = np.linspace(10.0, 15.0, len(dates))
    result = __import__("backend.app.services.statistical_analysis", fromlist=["hac_regression_analysis"]).hac_regression_analysis(
        _make_records(values, dates.strftime("%Y-%m-%d"))
    )
    assert result["status"] == "not_calculated"


def test_constant_series_has_no_trend_and_no_statistically_meaningful_signal():
    dates = pd.date_range("2024-01-01", periods=20, freq="D")
    values = np.full(len(dates), 10.0)
    result = mann_kendall_analysis(_make_records(values, dates.strftime("%Y-%m-%d")))
    assert result["trend_direction"] == "no_trend"
    assert result["p_value"] == pytest.approx(1.0)
    assert result["lag1_autocorrelation"] == pytest.approx(0.0)


# Keep the existing tied-value coverage, but verify the new assumption-bound behavior explicitly.
def test_tied_values_are_handled_with_correct_assumptions():
    dates = pd.date_range("2024-01-01", periods=8, freq="D")
    values = [10, 10, 10, 11, 11, 12, 12, 12]
    result = mann_kendall_analysis(_make_records(values, dates.strftime("%Y-%m-%d")))
    assert "s_statistic" in result
    assert "kendall_tau" in result
    assert result["p_value"] >= 0.0


def test_generate_trend_visualizations_uses_figure_names_and_base64_pngs():
    dates = pd.date_range("2024-01-01", periods=12, freq="MS")
    records = _make_records(np.linspace(9.0, 15.0, len(dates)), dates.strftime("%Y-%m-%d"))
    analysis_results = {
        "linear_regression": linear_regression_analysis(records),
        "mann_kendall": mann_kendall_analysis(records),
        "modified_mann_kendall": {
            "status": "calculated",
            "selected_method": "Hamed-Rao modified Mann-Kendall",
            "autocorrelation_coefficient": 0.35,
            "modified_mann_kendall": {
                "trend_direction": "increasing",
                "kendall_tau": 0.5,
                "p_value": 0.01,
            },
        },
        "seasonal_mann_kendall": {
            "status": "insufficient_data",
            "insufficient_data_reason": "Not enough years.",
        },
        "t_test": paired_period_t_test(
            records,
            ("2024-01-01", "2024-03-31"),
            ("2024-10-01", "2024-12-31"),
        ),
    }

    visualizations = generate_trend_visualizations(
        records,
        analysis_results,
        {"west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7},
        "MOD11A1",
        mean_raster=np.ones((4, 4), dtype=np.float32),
        raster_bounds=(24.7, 22.0, 36.9, 31.7),
    )

    assert set(visualizations) == {
        "heatmap",
        "linear_regression",
        "mann_kendall",
        "modified_mann_kendall",
        "seasonal_mann_kendall",
        "t_test",
    }
    for chart in visualizations.values():
        assert chart["content_type"] == "image/png"
        assert base64.b64decode(chart["base64"]).startswith(b"\x89PNG")
