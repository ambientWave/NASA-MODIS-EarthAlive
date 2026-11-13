"""Base64 comparison charts shared by the variable/region comparison jobs."""

from __future__ import annotations

from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from app.services.statistical_analysis import _figure_to_visualization
from app.utils.logging import log


def _frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for record in records:
        value = record.get("value")
        if value is None or record.get("missing"):
            continue
        rows.append({"date": pd.to_datetime(record.get("observation_date")), "value": float(value)})
    if not rows:
        return pd.DataFrame(columns=["date", "value"])
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def generate_comparison_visualizations(
    series_by_label: dict[str, list[dict[str, Any]]],
    analysis_by_label: dict[str, dict[str, Any]],
    metadata_by_label: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Render overview charts for a multi-variable or multi-region result."""
    visualizations: dict[str, dict[str, str]] = {}

    frames = {label: _frame(records) for label, records in series_by_label.items()}
    labels = [label for label in frame_order(series_by_label) if not frames[label].empty]

    if labels:
        try:
            fig, axes = plt.subplots(len(labels), 1, figsize=(11, max(3.0, 2.6 * len(labels))), sharex=True)
            if len(labels) == 1:
                axes = [axes]
            for axis, label in zip(axes, labels):
                frame = frames[label]
                unit = metadata_by_label.get(label, {}).get("unit", "")
                axis.plot(frame["date"], frame["value"], marker="o", markersize=2.5, linewidth=1.3, color="tab:blue")
                axis.set_ylabel(f"{label}\n({unit})" if unit else label)
                axis.grid(alpha=0.25)
            axes[0].set_title("Time-series comparison")
            axes[-1].set_xlabel("Date")
            fig.autofmt_xdate()
            fig.tight_layout()
            visualizations["timeseries_comparison"] = _figure_to_visualization(fig)
        except Exception as exc:  # pragma: no cover - rendering guard
            log(f"comparison charts: timeseries failed: {type(exc).__name__}: {exc}")

    try:
        slope_labels: list[str] = []
        slopes: list[float] = []
        for label in series_by_label:
            analysis = analysis_by_label.get(label, {})
            results = analysis.get("analysis_results", analysis)
            result = results.get("linear_regression", {})
            slope = result.get("slope_per_year", result.get("slope_celsius_per_year"))
            if slope is not None:
                unit = metadata_by_label.get(label, {}).get("unit", "")
                slope_labels.append(f"{label}\n({unit}/year)" if unit else f"{label}\n(per year)")
                slopes.append(float(slope))
        if slope_labels:
            fig, ax = plt.subplots(figsize=(10, 4.5))
            colors = ["tab:red" if slope < 0 else "tab:green" for slope in slopes]
            ax.bar(slope_labels, slopes, color=colors)
            ax.axhline(0.0, color="black", linewidth=0.8)
            ax.set_title("Linear-regression slope comparison (native units; bar heights are not directly comparable)")
            ax.set_ylabel("Slope per year")
            fig.tight_layout()
            visualizations["trend_summary"] = _figure_to_visualization(fig)
    except Exception as exc:  # pragma: no cover - rendering guard
        log(f"comparison charts: trend summary failed: {type(exc).__name__}: {exc}")

    try:
        coverage_labels: list[str] = []
        coverage_values: list[float] = []
        for label, result in analysis_by_label.items():
            coverage = result.get("data_quality", {}).get("data_coverage_percentage")
            if coverage is None:
                continue
            coverage_labels.append(label)
            coverage_values.append(float(coverage))
        if coverage_labels:
            fig, ax = plt.subplots(figsize=(10, 4.5))
            ax.bar(coverage_labels, coverage_values, color="tab:blue")
            ax.set_ylim(0, 100)
            ax.set_title("Data coverage comparison")
            ax.set_ylabel("Valid pixels (%)")
            fig.tight_layout()
            visualizations["coverage_comparison"] = _figure_to_visualization(fig)
    except Exception as exc:  # pragma: no cover - rendering guard
        log(f"comparison charts: coverage failed: {type(exc).__name__}: {exc}")

    return visualizations


def frame_order(series_by_label: dict[str, Any]) -> list[str]:
    return list(series_by_label)
