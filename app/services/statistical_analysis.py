from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

from app.utils.encoding import encode_bytes_base64
from app.utils.logging import log


DEFAULT_ANALYSIS_DIR = Path(__file__).resolve().parents[3] / "data" / "processed" / "analysis"


@dataclass
class AnalysisConfig:
    aoi: dict[str, float]
    product: str = "MOD11A1"
    start_date: str | None = None
    end_date: str | None = None
    seasonal_adjustment: bool = False
    analysis_frequency: str = "daily"
    output_dir: str | Path | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        self.aoi = {
            "west": float(self.aoi["west"]),
            "south": float(self.aoi["south"]),
            "east": float(self.aoi["east"]),
            "north": float(self.aoi["north"]),
        }
        if self.aoi["west"] >= self.aoi["east"]:
            raise ValueError("AOI west must be less than east.")
        if self.aoi["south"] >= self.aoi["north"]:
            raise ValueError("AOI south must be less than north.")
        self.product = str(self.product).upper()
        self.analysis_frequency = str(self.analysis_frequency).lower()
        self.output_dir = Path(self.output_dir) if self.output_dir is not None else DEFAULT_ANALYSIS_DIR
        self.label = str(self.label).strip().lower() if self.label else "analysis"


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_missing(value: Any) -> bool:
    if value is None or value == "":
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "y", "1"}:
            return True
        if text in {"false", "no", "n", "0"}:
            return False
        return text == "nan"
    return False


def _lag1_autocorrelation(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    if values.size < 2:
        return 0.0
    if np.allclose(values, values[0]):
        return 0.0
    left = values[:-1]
    right = values[1:]
    if left.size == 0:
        return 0.0
    corr = np.corrcoef(left, right)[0, 1]
    return float(np.nan_to_num(corr, nan=0.0, posinf=0.0, neginf=0.0))


def _mann_kendall_core(values: np.ndarray) -> dict[str, float]:
    x = np.asarray(values, dtype=float)
    if x.size == 0:
        return {"s": 0.0, "tau": 0.0, "var_s": 0.0, "z": 0.0, "p_value": 1.0}
    if x.size == 1:
        return {"s": 0.0, "tau": 0.0, "var_s": 0.0, "z": 0.0, "p_value": 1.0}

    s = 0.0
    for i in range(x.size):
        for j in range(i + 1, x.size):
            s += np.sign(x[j] - x[i])

    unique, counts = np.unique(x, return_counts=True)
    tie_corr = 0.0
    for count in counts:
        tie_corr += count * (count - 1) * (2 * count + 5)

    n = x.size
    var_s = (n * (n - 1) * (2 * n + 5) - tie_corr) / 18.0
    if var_s <= 0:
        z = 0.0
        p_value = 1.0
    else:
        if s > 0:
            z = (s - 1) / np.sqrt(var_s)
        elif s < 0:
            z = (s + 1) / np.sqrt(var_s)
        else:
            z = 0.0
        p_value = float(2 * stats.norm.sf(abs(z)))

    denominator = np.sqrt((n * (n - 1) / 2.0) * (n * (n - 1) / 2.0))
    tau = float(s / denominator) if denominator > 0 else 0.0
    return {"s": float(s), "tau": float(tau), "var_s": float(var_s), "z": float(z), "p_value": float(p_value)}


def _modified_mk_hemad_rao(values: np.ndarray) -> dict[str, Any]:
    x = np.asarray(values, dtype=float)
    n = x.size
    if n < 10:
        return {
            "status": "insufficient_data",
            "reason": "Modified Mann-Kendall requires at least 10 valid observations.",
            "autocorrelation_coefficient": 0.0,
        }

    base = _mann_kendall_core(x)
    base_var = float(base["var_s"])
    rho = _lag1_autocorrelation(x)
    if abs(rho) < 0.1:
        return {
            "status": "not_applicable",
            "reason": "Estimated lag-1 autocorrelation is weak; the standard Mann-Kendall variance is retained.",
            "autocorrelation_coefficient": float(rho),
            "base_result": base,
        }

    if base_var <= 0:
        return {
            "status": "not_applicable",
            "reason": "The variance of the S statistic is zero or undefined; the standard result is retained.",
            "autocorrelation_coefficient": float(rho),
            "base_result": base,
        }

    max_lag = min(10, max(1, n // 3))
    rho_estimates = []
    for lag in range(1, max_lag + 1):
        if lag >= n:
            break
        left = x[:-lag]
        right = x[lag:]
        if left.size < 2:
            continue
        if np.allclose(left, left[0]) and np.allclose(right, right[0]):
            continue
        corr = np.corrcoef(left, right)[0, 1]
        corr = float(np.nan_to_num(corr, nan=0.0, posinf=0.0, neginf=0.0))
        rho_estimates.append(corr)

    if not rho_estimates:
        return {
            "status": "not_applicable",
            "reason": "Autocorrelation cannot be estimated credibly from the available observations.",
            "autocorrelation_coefficient": float(rho),
            "base_result": base,
        }

    rho_terms = np.asarray(rho_estimates, dtype=float)
    correction_factor = 1.0 + 2.0 * np.sum((1.0 - np.arange(1, len(rho_terms) + 1) / n) * rho_terms)
    if not np.isfinite(correction_factor) or correction_factor <= 0:
        return {
            "status": "not_applicable",
            "reason": "The Hamed-Rao variance correction is non-positive; the standard variance is retained.",
            "autocorrelation_coefficient": float(rho),
            "base_result": base,
        }

    adjusted_var = max(base_var * correction_factor, 1e-12)
    adjusted_z = base["s"] / np.sqrt(adjusted_var)
    adjusted_p = float(2 * stats.norm.sf(abs(adjusted_z)))
    return {
        "status": "calculated",
        "method": "Hamed-Rao modified Mann-Kendall",
        "autocorrelation_coefficient": float(rho),
        "correction_factor": float(correction_factor),
        "adjusted_variance": float(adjusted_var),
        "s": float(base["s"]),
        "tau": float(base["tau"]),
        "z": float(adjusted_z),
        "p_value": float(adjusted_p),
        "trend_direction": "increasing" if base["s"] > 0 else "decreasing" if base["s"] < 0 else "no_trend",
        "base_result": base,
        "assumptions": [
            "The original Mann-Kendall S and tau statistics are preserved.",
            "Only the variance used for inference is corrected to account for positive lag-1 dependence and the serial-correlation structure implied by the observed lags.",
            "No automatic prewhitening is applied; the raw series remains the basis for the test and the correction is used only when the data conditions support it.",
        ],
    }


def validate_time_series(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    raw = list(records)
    if not raw:
        raise ValueError("Time series is empty.")

    required = [
        "observation_date",
        "mean_lst",
        "valid_pixel_count",
        "total_pixel_count",
        "valid_data_percentage",
        "missing",
    ]
    missing_columns = [column for column in required if column not in raw[0]]
    if missing_columns:
        raise ValueError(f"Series is missing required columns: {missing_columns}")

    df = pd.DataFrame(raw)
    df["observation_date"] = pd.to_datetime(df["observation_date"], errors="coerce")
    df = df.dropna(subset=["observation_date"]).copy()

    df["mean_lst"] = df["mean_lst"].map(_coerce_float)
    df["valid_pixel_count"] = pd.to_numeric(df["valid_pixel_count"], errors="coerce").fillna(0)
    df["total_pixel_count"] = pd.to_numeric(df["total_pixel_count"], errors="coerce").fillna(0)
    df["valid_data_percentage"] = pd.to_numeric(df["valid_data_percentage"], errors="coerce")
    df["missing"] = df["missing"].map(_coerce_missing)

    if df.empty:
        raise ValueError("No valid observations remain after parsing dates.")

    duplicates = df["observation_date"].duplicated(keep=False)
    duplicate_dates = df.loc[duplicates, "observation_date"].dt.strftime("%Y-%m-%d").unique().tolist()
    if duplicate_dates:
        raise ValueError(f"Duplicate observation dates detected: {duplicate_dates}")

    df = df.sort_values("observation_date").reset_index(drop=True)
    valid_mask = df["mean_lst"].notna() & ~df["missing"]
    valid_obs = df.loc[valid_mask].copy()
    missing_obs = df.loc[~valid_mask].copy()

    if valid_obs.empty:
        raise ValueError("No valid non-missing observations remain for statistical analysis.")
    if len(valid_obs) < 3:
        raise ValueError("Insufficient valid observations for meaningful statistical analysis.")

    missing_pct = float(missing_obs.shape[0] / max(1, len(df))) * 100.0
    coverage = 0.0
    if valid_obs["total_pixel_count"].sum() > 0:
        coverage = float(valid_obs["valid_pixel_count"].sum() / valid_obs["total_pixel_count"].sum()) * 100.0

    summary = {
        "observation_count": int(len(df)),
        "valid_observation_count": int(len(valid_obs)),
        "missing_observation_count": int(len(missing_obs)),
        "missing_observation_percentage": missing_pct,
        "data_coverage_percentage": coverage,
        "date_range_start": valid_obs["observation_date"].min().strftime("%Y-%m-%d"),
        "date_range_end": valid_obs["observation_date"].max().strftime("%Y-%m-%d"),
        "duplicate_dates": duplicate_dates,
        "warnings": [],
    }

    if missing_pct > 20.0:
        summary["warnings"].append("Missing observations exceed 20% of the record; trend interpretation should be conservative.")
    if coverage < 50.0:
        summary["warnings"].append("Coverage is low; valid-pixel fraction is below 50% of the total possible pixels.")
    if valid_obs["valid_pixel_count"].sum() < 10:
        summary["warnings"].append("Very few valid pixels remain after filtering; conclusions should be considered provisional.")

    return {"dataframe": valid_obs, "summary": summary, "missing_records": missing_obs}


def linear_regression_analysis(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    validated = validate_time_series(records)
    df = validated["dataframe"].copy().sort_values("observation_date").reset_index(drop=True)
    x = (df["observation_date"] - df["observation_date"].min()).dt.total_seconds() / (365.25 * 86400.0)
    y = df["mean_lst"].to_numpy(dtype=float)

    slope, intercept, r_value, p_value, stderr = stats.linregress(x.to_numpy(dtype=float), y)
    r_squared = float(r_value ** 2)
    t_crit = float(stats.t.ppf(0.975, max(1, len(df) - 2)))
    ci_low = slope - t_crit * stderr
    ci_high = slope + t_crit * stderr
    lag1 = _lag1_autocorrelation(y)
    warnings: list[str] = []
    if abs(lag1) > 0.5:
        warnings.append("Ordinary OLS p-values and confidence intervals are optimistic when serial dependence is strong.")

    return {
        "status": "calculated",
        "n": int(len(df)),
        "slope_celsius_per_year": float(slope),
        "intercept_celsius": float(intercept),
        "r_squared": float(r_squared),
        "p_value": float(p_value),
        "standard_error": float(stderr),
        "confidence_interval_95": {"lower": float(ci_low), "upper": float(ci_high)},
        "significance": "significant" if p_value < 0.05 else "not_significant",
        "trend_direction": "increasing" if slope > 0 else "decreasing" if slope < 0 else "flat",
        "lag1_autocorrelation": float(lag1),
        "warnings": warnings,
        "observations_used": int(len(df)),
        "date_range_start": df["observation_date"].min().strftime("%Y-%m-%d"),
        "date_range_end": df["observation_date"].max().strftime("%Y-%m-%d"),
        "confidence_interval_type": "ordinary_ols_95",
        "assumptions": [
            "Ordinary least squares regression is used with actual elapsed time in years, not equal spacing assumptions.",
            "The reported confidence interval assumes approximately independent residuals.",
            "Strong serial correlation makes this inference optimistic and requires a robust or autocorrelation-aware alternative.",
        ],
    }


def mann_kendall_analysis(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    validated = validate_time_series(records)
    df = validated["dataframe"].copy().sort_values("observation_date").reset_index(drop=True)
    values = df["mean_lst"].to_numpy(dtype=float)
    stats_out = _mann_kendall_core(values)
    lag1 = _lag1_autocorrelation(values)
    warnings: list[str] = []
    if abs(lag1) > 0.5:
        warnings.append("Strong serial dependence is present; the standard Mann-Kendall p-value may overstate statistical significance.")
    return {
        "status": "calculated",
        "n": int(len(values)),
        "s_statistic": float(stats_out["s"]),
        "kendall_tau": float(stats_out["tau"]),
        "z_statistic": float(stats_out["z"]),
        "p_value": float(stats_out["p_value"]),
        "trend_direction": "increasing" if stats_out["s"] > 0 else "decreasing" if stats_out["s"] < 0 else "no_trend",
        "statistical_significance": "significant" if stats_out["p_value"] < 0.05 else "not_significant",
        "lag1_autocorrelation": float(lag1),
        "serial_correlation_warning": bool(abs(lag1) > 0.5),
        "warnings": warnings,
        "modified_mann_kendall_considered": bool(abs(lag1) > 0.5),
        "assumptions": [
            "Mann-Kendall is applied to the raw valid observations without interpolation.",
            "The p-value is valid under the standard independent-sample approximation.",
            "When strong serial dependence is present, the standard result should be interpreted cautiously and supplemented by a modified variance correction if justified.",
        ],
    }


def modified_mann_kendall_analysis(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    validated = validate_time_series(records)
    df = validated["dataframe"].copy().sort_values("observation_date").reset_index(drop=True)
    values = df["mean_lst"].to_numpy(dtype=float)
    standard = mann_kendall_analysis(df.to_dict("records"))
    correction = _modified_mk_hemad_rao(values)

    if correction["status"] == "insufficient_data":
        return {
            "status": "insufficient_data",
            "insufficient_data_reason": correction["reason"],
            "standard_mann_kendall": standard,
            "autocorrelation_coefficient": float(_lag1_autocorrelation(values)),
            "assumptions": [
                "The modified test is not calculated because the sample is too short or the variance correction is unsupported.",
                "The standard Mann-Kendall result remains reported for comparison.",
            ],
        }

    if correction["status"] == "not_applicable":
        return {
            "status": "not_applicable",
            "reason": correction["reason"],
            "standard_mann_kendall": standard,
            "autocorrelation_coefficient": float(correction.get("autocorrelation_coefficient", _lag1_autocorrelation(values))),
            "selected_method": "standard_mann_kendall",
            "assumptions": correction.get("assumptions", ["The correction is not appropriate under the observed conditions; the standard test is retained."]),
        }

    return {
        "status": "calculated",
        "selected_method": "Hamed-Rao modified Mann-Kendall",
        "standard_mann_kendall": standard,
        "modified_mann_kendall": {
            "s_statistic": float(correction["s"]),
            "kendall_tau": float(correction["tau"]),
            "z_statistic": float(correction["z"]),
            "p_value": float(correction["p_value"]),
            "trend_direction": correction["trend_direction"],
            "autocorrelation_coefficient": float(correction["autocorrelation_coefficient"]),
            "correction_factor": float(correction["correction_factor"]),
            "adjusted_variance": float(correction["adjusted_variance"]),
            "method": correction["method"],
        },
        "autocorrelation_coefficient": float(correction["autocorrelation_coefficient"]),
        "assumptions": correction["assumptions"],
    }


def seasonal_deseasonalize(records: Iterable[dict[str, Any]], frequency: str = "monthly") -> dict[str, Any]:
    validated = validate_time_series(records)
    df = validated["dataframe"].copy().sort_values("observation_date").reset_index(drop=True)
    if frequency == "monthly":
        df["month"] = df["observation_date"].dt.month
        month_means = df.groupby("month")["mean_lst"].mean()
        df["seasonally_adjusted_lst"] = df["mean_lst"] - df["month"].map(month_means) + month_means.mean()
    else:
        df["seasonally_adjusted_lst"] = df["mean_lst"]
    return {
        "dataframe": df,
        "monthly_means": {int(k): float(v) for k, v in month_means.items()} if frequency == "monthly" else {},
        "warnings": ["Seasonal decomposition is not the same as Seasonal Mann-Kendall and should not be interpreted as a long-term trend by itself."],
    }


def seasonal_mann_kendall_analysis(records: Iterable[dict[str, Any]], min_years: int = 2, min_per_month: int = 2) -> dict[str, Any]:
    validated = validate_time_series(records)
    df = validated["dataframe"].copy().sort_values("observation_date").reset_index(drop=True)

    if df["observation_date"].dt.month.nunique() < 2:
        return {
            "status": "not_supported",
            "reason": "Seasonal Mann-Kendall is implemented for monthly time series with at least two calendar months; daily observations are not treated as a valid seasonal sequence here.",
            "minimum_required_months": 2,
        }
    if df["observation_date"].dt.year.nunique() < min_years:
        return {
            "status": "insufficient_data",
            "insufficient_data_reason": f"Seasonal Mann-Kendall requires at least {min_years} years of monthly observations; only {int(df['observation_date'].dt.year.nunique())} are available.",
            "years_available": int(df["observation_date"].dt.year.nunique()),
        }

    monthly_results = {}
    for month in range(1, 13):
        month_df = df[df["observation_date"].dt.month == month]
        if len(month_df) < min_per_month:
            continue
        result = _mann_kendall_core(month_df["mean_lst"].to_numpy(dtype=float))
        monthly_results[month] = {
            "n": int(len(month_df)),
            "s_statistic": float(result["s"]),
            "kendall_tau": float(result["tau"]),
            "z_statistic": float(result["z"]),
            "p_value": float(result["p_value"]),
            "trend_direction": "increasing" if result["s"] > 0 else "decreasing" if result["s"] < 0 else "no_trend",
        }

    if not monthly_results:
        return {
            "status": "insufficient_data",
            "insufficient_data_reason": "No month had enough valid observations for a seasonal Mann-Kendall calculation.",
            "minimum_per_month": min_per_month,
        }

    s_total = float(sum(entry["s_statistic"] for entry in monthly_results.values()))
    var_total = float(sum(_mann_kendall_core(df[df["observation_date"].dt.month == month]["mean_lst"].to_numpy(dtype=float))["var_s"] for month in monthly_results.keys()))
    z = float(s_total / np.sqrt(var_total)) if var_total > 0 else 0.0
    p_value = float(2 * stats.norm.sf(abs(z)))
    direction = "increasing" if s_total > 0 else "decreasing" if s_total < 0 else "no_trend"

    return {
        "status": "calculated",
        "method": "Seasonal Mann-Kendall",
        "months_evaluated": sorted(monthly_results.keys()),
        "n_months": len(monthly_results),
        "combined_s": s_total,
        "combined_variance": var_total,
        "z_statistic": z,
        "p_value": p_value,
        "trend_direction": direction,
        "statistical_significance": "significant" if p_value < 0.05 else "not_significant",
        "assumptions": [
            "The same calendar month is compared across years, not single-day or multi-day sequences.",
            "Missing months and unequal seasonal sample sizes are allowed, but each month must still have enough valid observations.",
            "This is a seasonal trend check, not a decomposition-based trend estimate, and it should not be interpreted as a daily trend analysis.",
        ],
        "monthly_results": monthly_results,
    }


def _newey_west_hac(x: np.ndarray, y: np.ndarray, lag: int | None = None) -> tuple[float, float, dict[str, Any]]:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = x.size
    if n < 5:
        return 0.0, 0.0, {"status": "not_calculated", "reason": "Insufficient observations for HAC inference."}

    if lag is None:
        lag = max(1, min(int(np.sqrt(n)), n // 5))
    lag = min(lag, n - 1)
    X = np.column_stack([np.ones_like(x), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    residuals = y - X @ beta
    xtx_inv = np.linalg.inv(X.T @ X)
    meat = np.zeros((2, 2), dtype=float)

    for t in range(n):
        xt = X[t]
        meat += np.outer(xt, xt) * residuals[t] ** 2

    for h in range(1, lag + 1):
        weight = 1.0 - h / (lag + 1)
        for t in range(h, n):
            xt = X[t]
            xt_h = X[t - h]
            contrib = weight * (residuals[t] * residuals[t - h])
            meat += contrib * (np.outer(xt, xt_h) + np.outer(xt_h, xt))

    cov = xtx_inv @ meat @ xtx_inv
    slope = float(beta[1])
    slope_se = float(np.sqrt(max(cov[1, 1], 0.0)))
    t_stat = float(slope / slope_se) if slope_se > 0 else 0.0
    p_value = float(2 * stats.t.sf(abs(t_stat), df=max(1, n - 2)))
    t_crit = float(stats.t.ppf(0.975, max(1, n - 2)))
    ci_low = slope - t_crit * slope_se
    ci_high = slope + t_crit * slope_se

    return slope, slope_se, {
        "status": "calculated",
        "lag": int(lag),
        "selection_method": "automatic Newey-West lag = min(sqrt(n), n/5)",
        "slope_celsius_per_year": float(slope),
        "standard_error": float(slope_se),
        "p_value": float(p_value),
        "confidence_interval_95": {"lower": float(ci_low), "upper": float(ci_high)},
        "assumptions": [
            "A HAC covariance estimator is used for a weakly dependent error process.",
            "This inference is robust to heteroskedasticity and serial dependence under the usual HAC assumptions.",
            "A standard equally-spaced HAC estimator is not used blindly when observation spacing is irregular; irregular spacing triggers a not-calculated result.",
        ],
    }


def hac_regression_analysis(records: Iterable[dict[str, Any]], lag: int | None = None) -> dict[str, Any]:
    validated = validate_time_series(records)
    df = validated["dataframe"].copy().sort_values("observation_date").reset_index(drop=True)
    if len(df) < 5:
        return {"status": "not_calculated", "reason": "Insufficient observations for HAC inference.", "n": int(len(df))}

    times = (df["observation_date"] - df["observation_date"].min()).dt.total_seconds() / (365.25 * 86400.0)
    x = times.to_numpy(dtype=float)
    y = df["mean_lst"].to_numpy(dtype=float)

    if x.size > 1:
        spacing = np.diff(x)
        if spacing.size > 0:
            median_spacing = float(np.median(np.abs(spacing)))
            if median_spacing > 0:
                spacing_cv = float(np.std(np.abs(spacing), ddof=1) / median_spacing)
            else:
                spacing_cv = 0.0
        else:
            spacing_cv = 0.0
    else:
        spacing_cv = 0.0

    if spacing_cv > 0.25:
        return {
            "status": "not_calculated",
            "reason": "Observation spacing is irregular enough that a standard HAC estimator is not appropriate without a custom time-aware covariance design.",
            "irregular_spacing_coefficient_of_variation": float(spacing_cv),
            "n": int(len(df)),
            "assumptions": [
                "A standard equally-spaced HAC estimator is not applied blindly to an irregular series.",
                "The data are reported with ordinary OLS inference only, while robust inference is withheld because the date spacing is unsuitable.",
            ],
        }

    slope, slope_se, result = _newey_west_hac(x, y, lag=lag)
    return {
        "status": result["status"],
        "n": int(len(df)),
        "slope_celsius_per_year": float(slope),
        "standard_error": float(slope_se),
        "lag": result.get("lag"),
        "lag_selection_method": result.get("selection_method"),
        "p_value": result.get("p_value"),
        "confidence_interval_95": result.get("confidence_interval_95", {}),
        "assumptions": result.get("assumptions", []),
        "warnings": ["HAC inference was calculated only when the spacing structure was sufficiently regular for a robust covariance estimate."],
    }


def paired_period_t_test(
    records: Iterable[dict[str, Any]],
    early_period: tuple[str, str],
    late_period: tuple[str, str],
) -> dict[str, Any]:
    valid = validate_time_series(records)["dataframe"].copy()
    valid["observation_date"] = pd.to_datetime(valid["observation_date"])

    start_a, end_a = early_period
    start_b, end_b = late_period
    mask_a = (valid["observation_date"] >= pd.Timestamp(start_a)) & (valid["observation_date"] <= pd.Timestamp(end_a))
    mask_b = (valid["observation_date"] >= pd.Timestamp(start_b)) & (valid["observation_date"] <= pd.Timestamp(end_b))

    values_a = valid.loc[mask_a, "mean_lst"].dropna().to_numpy(dtype=float)
    values_b = valid.loc[mask_b, "mean_lst"].dropna().to_numpy(dtype=float)

    if len(values_a) < 2 or len(values_b) < 2:
        raise ValueError("Each comparison period must contain at least two valid observations.")

    mean_a = float(np.mean(values_a))
    mean_b = float(np.mean(values_b))
    diff = mean_b - mean_a
    statistic, p_value = stats.ttest_ind(values_b, values_a, equal_var=False, nan_policy="omit")
    se = float(np.sqrt(np.var(values_b, ddof=1) / len(values_b) + np.var(values_a, ddof=1) / len(values_a)))
    t_star = float(stats.t.ppf(0.975, max(1, min(len(values_b), len(values_a)) - 1)))
    ci_low = diff - t_star * se
    ci_high = diff + t_star * se
    pooled_sd = float(np.sqrt(((len(values_b) - 1) * np.var(values_b, ddof=1) + (len(values_a) - 1) * np.var(values_a, ddof=1)) / (len(values_b) + len(values_a) - 2)))
    effect_size = float(diff / pooled_sd) if pooled_sd > 0 else 0.0
    combined = np.concatenate([values_a, values_b])
    auto_corr = _lag1_autocorrelation(combined)
    warnings: list[str] = []
    if abs(auto_corr) > 0.5:
        warnings.append("The Welch comparison is being reported as descriptive only because strong serial dependence undermines the independence assumption for daily observations.")

    return {
        "period_a": {"start": start_a, "end": end_a, "mean": mean_a, "n": len(values_a)},
        "period_b": {"start": start_b, "end": end_b, "mean": mean_b, "n": len(values_b)},
        "mean_difference_celsius": float(diff),
        "test_statistic": float(statistic),
        "p_value": float(p_value),
        "confidence_interval_95": {"lower": float(ci_low), "upper": float(ci_high)},
        "effect_size_cohens_d": float(effect_size),
        "lag1_autocorrelation": float(auto_corr),
        "serial_correlation_warning": bool(abs(auto_corr) > 0.5),
        "warnings": warnings,
        "descriptive_only": bool(abs(auto_corr) > 0.5),
        "assumptions": [
            "Welch's two-sample t-test is used for two explicitly defined periods with unequal variance assumptions.",
            "This is a period comparison, not evidence of causation.",
            "The independence assumption is especially tenuous for daily autocorrelated observations, so the p-value should be treated as descriptive rather than inferential.",
        ],
    }


def save_analysis_json(results: dict[str, Any], output_path: str | Path) -> str:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)
    return str(output_path)


def generate_analysis_visualizations(
    records: Iterable[dict[str, Any]],
    output_dir: str | Path,
    aoi: dict[str, float],
    product: str,
    regression_result: dict[str, Any] | None = None,
) -> dict[str, str]:
    validated = validate_time_series(records)
    df = validated["dataframe"].copy().sort_values("observation_date").reset_index(drop=True)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    time_series_path = out_dir / "time_series.png"
    trend_path = out_dir / "linear_regression.png"
    missing_path = out_dir / "missing_observations.png"

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(df["observation_date"], df["mean_lst"], marker="o", linewidth=1.8)
    ax.set_title(f"{product} LST Time Series for AOI {aoi}")
    ax.set_xlabel("Date")
    ax.set_ylabel("Mean LST (°C)")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(time_series_path, dpi=150)
    plt.close(fig)

    if regression_result is not None:
        fig2, ax2 = plt.subplots(figsize=(10, 5))
        x = (df["observation_date"] - df["observation_date"].min()).dt.total_seconds() / (365.25 * 86400.0)
        fit_x = np.linspace(float(x.min()), float(x.max()), 100)
        fit_y = regression_result["slope_celsius_per_year"] * fit_x + regression_result["intercept_celsius"]
        ax2.plot(x, df["mean_lst"], marker="o", label="Observed", color="tab:blue")
        ax2.plot(fit_x, fit_y, color="tab:red", linewidth=2, label="Linear fit")
        ax2.set_title("Linear Regression Trend")
        ax2.set_xlabel("Years since start")
        ax2.set_ylabel("Mean LST (°C)")
        ax2.legend()
        fig2.tight_layout()
        fig2.savefig(trend_path, dpi=150)
        plt.close(fig2)

    fig3, ax3 = plt.subplots(figsize=(10, 4))
    missing = df["missing"].astype(bool)
    ax3.bar(df["observation_date"], missing.astype(int), width=0.8, color="tab:orange")
    ax3.set_title("Missing Observation Flags")
    ax3.set_xlabel("Date")
    ax3.set_ylabel("Missing (1 = yes)")
    fig3.autofmt_xdate()
    fig3.tight_layout()
    fig3.savefig(missing_path, dpi=150)
    plt.close(fig3)

    return {"time_series": str(time_series_path), "trend": str(trend_path), "missing": str(missing_path)}


def _figure_to_visualization(fig: Any) -> dict[str, str]:
    """Encode an open matplotlib figure as a base64 PNG object and close it."""
    buffer = io.BytesIO()
    try:
        fig.savefig(buffer, format="png", dpi=150, bbox_inches="tight")
        return {"content_type": "image/png", "base64": encode_bytes_base64(buffer.getvalue())}
    finally:
        plt.close(fig)


def _format_metric(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}g}"
    except (TypeError, ValueError):
        return str(value)


def _series_dataframe(records: Iterable[dict[str, Any]]) -> pd.DataFrame:
    validated = validate_time_series(records)
    return validated["dataframe"].copy().sort_values("observation_date").reset_index(drop=True)


def _render_series_with_annotation(
    df: pd.DataFrame,
    title: str,
    annotation_lines: Iterable[str],
    ylabel: str = "Mean LST (°C)",
) -> dict[str, str]:
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(df["observation_date"], df["mean_lst"], marker="o", linewidth=1.8, color="tab:blue")
    ax.set_title(title)
    ax.set_xlabel("Date")
    ax.set_ylabel(ylabel)
    fig.autofmt_xdate()
    ax.text(
        0.02,
        0.98,
        "\n".join(line for line in annotation_lines if line),
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=9,
        bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.85, "edgecolor": "0.7"},
    )
    fig.tight_layout()
    return _figure_to_visualization(fig)


def _render_linear_regression(
    df: pd.DataFrame,
    result: dict[str, Any],
    aoi: dict[str, float],
    product: str,
) -> dict[str, str]:
    fig, ax = plt.subplots(figsize=(10, 5))
    x = (df["observation_date"] - df["observation_date"].min()).dt.total_seconds() / (365.25 * 86400.0)
    slope = result.get("slope_celsius_per_year")
    intercept = result.get("intercept_celsius")
    ax.plot(df["observation_date"], df["mean_lst"], marker="o", label="Observed", color="tab:blue")
    if slope is not None and intercept is not None:
        fit_x = np.linspace(float(x.min()), float(x.max()), 100)
        fit_y = float(slope) * fit_x + float(intercept)
        fit_dates = df["observation_date"].min() + pd.to_timedelta(fit_x * 365.25, unit="D")
        ax.plot(fit_dates, fit_y, color="tab:red", linewidth=2, label="Linear fit")
    ax.set_title(f"{product} LST Linear Regression — AOI {aoi}")
    ax.set_xlabel("Date")
    ax.set_ylabel("Mean LST (°C)")
    ax.legend(loc="best")
    ax.text(
        0.02,
        0.98,
        "\n".join(
            [
                f"slope: {_format_metric(slope)} °C/year",
                f"p-value: {_format_metric(result.get('p_value'))}",
                f"R²: {_format_metric(result.get('r_squared'))}",
                f"n: {result.get('n', len(df))}",
            ]
        ),
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=9,
        bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.85, "edgecolor": "0.7"},
    )
    fig.autofmt_xdate()
    fig.tight_layout()
    return _figure_to_visualization(fig)


def _render_mann_kendall(
    df: pd.DataFrame,
    result: dict[str, Any],
    product: str,
    label: str,
) -> dict[str, str]:
    return _render_series_with_annotation(
        df,
        f"{product} LST {label} — AOI",
        [
            f"trend: {result.get('trend_direction', 'n/a')}",
            f"S: {_format_metric(result.get('s_statistic'))}",
            f"tau: {_format_metric(result.get('kendall_tau'))}",
            f"p-value: {_format_metric(result.get('p_value'))}",
            f"n: {result.get('n', len(df))}",
        ],
    )


def _render_modified_mann_kendall(
    df: pd.DataFrame,
    result: dict[str, Any],
    product: str,
) -> dict[str, str]:
    details = result.get("modified_mann_kendall") or result.get("standard_mann_kendall") or {}
    return _render_series_with_annotation(
        df,
        f"{product} LST Modified Mann-Kendall — AOI",
        [
            f"status: {result.get('status', 'n/a')}",
            f"method: {result.get('selected_method', details.get('method', 'n/a'))}",
            f"trend: {details.get('trend_direction', 'n/a')}",
            f"tau: {_format_metric(details.get('kendall_tau'))}",
            f"p-value: {_format_metric(details.get('p_value'))}",
            f"autocorrelation: {_format_metric(result.get('autocorrelation_coefficient'))}",
        ],
    )


def _render_seasonal_mann_kendall(
    df: pd.DataFrame,
    result: dict[str, Any],
    product: str,
) -> dict[str, str]:
    if result.get("status") == "calculated":
        monthly_means = df.groupby(df["observation_date"].dt.month)["mean_lst"].mean()
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.bar(monthly_means.index.astype(int), monthly_means.to_numpy(), color="tab:green")
        ax.set_title(f"{product} LST Seasonal Mann-Kendall — monthly means")
        ax.set_xlabel("Calendar month")
        ax.set_ylabel("Mean LST (°C)")
        ax.set_xticks(range(1, 13))
        ax.text(
            0.02,
            0.98,
            "\n".join(
                [
                    f"trend: {result.get('trend_direction', 'n/a')}",
                    f"combined S: {_format_metric(result.get('combined_s'))}",
                    f"p-value: {_format_metric(result.get('p_value'))}",
                    f"months: {result.get('n_months', len(monthly_means))}",
                ]
            ),
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=9,
            bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.85, "edgecolor": "0.7"},
        )
        fig.tight_layout()
        return _figure_to_visualization(fig)

    return _render_series_with_annotation(
        df,
        f"{product} LST Seasonal Mann-Kendall — AOI",
        [
            f"status: {result.get('status', 'n/a')}",
            str(result.get("reason") or result.get("insufficient_data_reason") or ""),
        ],
    )


def _render_t_test(
    df: pd.DataFrame,
    result: dict[str, Any],
    product: str,
) -> dict[str, str]:
    period_a = result.get("period_a")
    period_b = result.get("period_b")
    if not period_a or not period_b:
        return _render_series_with_annotation(
            df,
            f"{product} LST Period Comparison — AOI",
            [f"status: {result.get('status', 'insufficient_data')}", str(result.get("reason") or "")],
        )

    fig, ax = plt.subplots(figsize=(8, 5))
    labels = [
        f"Early\n{period_a.get('start')} to {period_a.get('end')}",
        f"Late\n{period_b.get('start')} to {period_b.get('end')}",
    ]
    means = [float(period_a.get("mean", 0.0)), float(period_b.get("mean", 0.0))]
    ax.bar(labels, means, color=["tab:blue", "tab:orange"])
    ax.set_title(f"{product} LST Period Comparison — AOI")
    ax.set_ylabel("Mean LST (°C)")
    ax.text(
        0.02,
        0.98,
        "\n".join(
            [
                f"difference: {_format_metric(result.get('mean_difference_celsius'))} °C",
                f"p-value: {_format_metric(result.get('p_value'))}",
                f"effect size: {_format_metric(result.get('effect_size_cohens_d'))}",
            ]
        ),
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=9,
        bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.85, "edgecolor": "0.7"},
    )
    fig.tight_layout()
    return _figure_to_visualization(fig)


def _render_heatmap(
    array: np.ndarray,
    bounds: tuple[float, float, float, float],
    product: str,
    aoi: dict[str, float],
) -> dict[str, str]:
    fig, ax = plt.subplots(figsize=(10, 8))
    masked = np.ma.masked_invalid(np.asarray(array, dtype=np.float32))
    image = ax.imshow(
        masked,
        cmap="inferno",
        origin="upper",
        extent=[bounds[0], bounds[2], bounds[1], bounds[3]],
    )
    fig.colorbar(image, ax=ax, label="Mean temperature (°C)")
    ax.set_title(f"{product} Mean LST Heatmap — AOI {aoi}")
    ax.set_xlabel("Longitude (°)")
    ax.set_ylabel("Latitude (°)")
    fig.tight_layout()
    return _figure_to_visualization(fig)


def generate_trend_visualizations(
    records: Iterable[dict[str, Any]],
    analysis_results: dict[str, Any],
    aoi: dict[str, float],
    product: str,
    mean_raster: np.ndarray | None = None,
    raster_bounds: tuple[float, float, float, float] | None = None,
) -> dict[str, dict[str, str]]:
    """Build the trend response ``visualizations`` mapping.

    Each key is the figure name (``heatmap`` or an analysis method name) and
    each value contains the PNG bytes encoded as base64. Figures that cannot be
    produced are omitted instead of failing the whole analysis request.
    """
    visualizations: dict[str, dict[str, str]] = {}

    if mean_raster is not None and raster_bounds is not None:
        try:
            visualizations["heatmap"] = _render_heatmap(mean_raster, raster_bounds, product, aoi)
        except Exception as exc:  # pragma: no cover - rendering guard
            log(f"visualizations: could not render heatmap: {type(exc).__name__}: {exc}")

    try:
        df = _series_dataframe(records)
    except ValueError as exc:
        log(f"visualizations: could not prepare series: {exc}")
        return visualizations

    renderers = {
        "linear_regression": lambda result: _render_linear_regression(df, result, aoi, product),
        "mann_kendall": lambda result: _render_mann_kendall(df, result, product, "Mann-Kendall"),
        "modified_mann_kendall": lambda result: _render_modified_mann_kendall(df, result, product),
        "seasonal_mann_kendall": lambda result: _render_seasonal_mann_kendall(df, result, product),
        "t_test": lambda result: _render_t_test(df, result, product),
    }

    for method, result in analysis_results.items():
        renderer = renderers.get(method)
        if renderer is None or not isinstance(result, dict):
            continue
        try:
            visualizations[method] = renderer(result)
        except Exception as exc:  # pragma: no cover - rendering guard
            log(f"visualizations: could not render '{method}': {type(exc).__name__}: {exc}")

    return visualizations


def analyze_time_series(
    records: Iterable[dict[str, Any]],
    config: AnalysisConfig,
    t_test_periods: tuple[tuple[str, str], tuple[str, str]] | None = None,
) -> dict[str, Any]:
    try:
        validated = validate_time_series(records)
    except ValueError as exc:
        return {
            "analysis_type": "time_series",
            "status": "insufficient_data",
            "insufficient_data_reason": str(exc),
            "selected_method": "none",
            "aoi": config.aoi,
            "product": config.product,
            "statistical_warnings": [str(exc)],
            "autocorrelation_status": "unknown",
        }

    df = validated["dataframe"].copy().sort_values("observation_date").reset_index(drop=True)
    standard_mk = mann_kendall_analysis(df.to_dict("records"))
    mk_res = _modified_mk_hemad_rao(df["mean_lst"].to_numpy(dtype=float))
    modified_mk = None
    if mk_res["status"] == "calculated":
        modified_mk = {
            "status": "calculated",
            "method": mk_res["method"],
            "s_statistic": float(mk_res["s"]),
            "kendall_tau": float(mk_res["tau"]),
            "z_statistic": float(mk_res["z"]),
            "p_value": float(mk_res["p_value"]),
            "trend_direction": mk_res["trend_direction"],
            "autocorrelation_coefficient": float(mk_res["autocorrelation_coefficient"]),
            "correction_factor": float(mk_res["correction_factor"]),
            "adjusted_variance": float(mk_res["adjusted_variance"]),
            "assumptions": mk_res["assumptions"],
        }
    elif mk_res["status"] in {"insufficient_data", "not_applicable"}:
        modified_mk = {
            "status": mk_res["status"],
            "reason": mk_res["reason"],
            "standard_mann_kendall": standard_mk,
            "autocorrelation_coefficient": float(mk_res.get("autocorrelation_coefficient", _lag1_autocorrelation(df["mean_lst"].to_numpy(dtype=float)))),
        }

    seasonal = seasonal_mann_kendall_analysis(df.to_dict("records")) if config.analysis_frequency == "monthly" else {
        "status": "not_supported",
        "reason": "Seasonal Mann-Kendall is implemented for monthly observations only; this series is not monthly.",
    }

    regression = linear_regression_analysis(df.to_dict("records"))
    hac = hac_regression_analysis(df.to_dict("records"))
    warnings = list(validated["summary"]["warnings"])
    if standard_mk["serial_correlation_warning"]:
        warnings.append("Standard Mann-Kendall and OLS p-values may be overly optimistic when strong serial dependency is present.")
    if seasonal.get("status") == "not_supported":
        warnings.append(seasonal["reason"])
    if hac.get("status") == "not_calculated":
        warnings.append(hac.get("reason", "HAC inference was not calculated."))

    selected_method = "standard_mann_kendall"
    if modified_mk and isinstance(modified_mk, dict) and modified_mk.get("status") == "calculated":
        selected_method = "modified_mann_kendall"

    auto_rho = float(standard_mk["lag1_autocorrelation"])
    if abs(auto_rho) > 0.5:
        autocorrelation_status = "strong"
    elif abs(auto_rho) > 0.1:
        autocorrelation_status = "moderate"
    else:
        autocorrelation_status = "weak_or_absent"

    result: dict[str, Any] = {
        "analysis_type": "time_series",
        "status": "calculated",
        "selected_method": selected_method,
        "aoi": config.aoi,
        "product": config.product,
        "date_range": {
            "start": df["observation_date"].min().strftime("%Y-%m-%d"),
            "end": df["observation_date"].max().strftime("%Y-%m-%d"),
        },
        "n_observations": int(len(df)),
        "missing_observation_percentage": float(validated["summary"]["missing_observation_percentage"]),
        "data_coverage_percentage": float(validated["summary"]["data_coverage_percentage"]),
        "analysis_frequency": config.analysis_frequency,
        "autocorrelation_status": autocorrelation_status,
        "autocorrelation_coefficient": auto_rho,
        "standard_mann_kendall": standard_mk,
        "modified_mann_kendall": modified_mk,
        "seasonal_mann_kendall": seasonal,
        "ols_regression": regression,
        "hac_regression": hac,
        "confidence_interval_type": "ordinary_ols_95" if hac.get("status") != "calculated" else "hac_newey_west_95",
        "statistical_warnings": warnings,
        "insufficient_data_reason": None,
        "linear_regression": regression,
        "mann_kendall": standard_mk,
        "scientific_limitations": [
            "Standard Mann-Kendall and OLS inference are only reliable under weak serial dependence.",
            "Seasonal Mann-Kendall is a separate analysis and does not replace raw trend analysis.",
            "Robust HAC inference is only calculated when the series is sufficiently regular in time.",
            "Welch comparisons are descriptive when the underlying observations are autocorrelated and cannot be interpreted as causal effects.",
        ],
    }

    if t_test_periods is not None:
        result["t_test"] = paired_period_t_test(df.to_dict("records"), t_test_periods[0], t_test_periods[1])
        result["confidence_interval_type"] = "ordinary_ols_95" if hac.get("status") != "calculated" else "hac_newey_west_95"

    if config.output_dir is not None:
        charts = generate_analysis_visualizations(df.to_dict("records"), config.output_dir, config.aoi, config.product, regression)
        result["visualizations"] = charts
        json_path = config.output_dir / f"{config.label}_analysis.json"
        save_analysis_json(result, json_path)

    return result
