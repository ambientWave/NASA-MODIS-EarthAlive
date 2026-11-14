"""PDF Report Generator for EarthAlive Scientific Analysis and Comparisons.

Renders modern, publication-quality executive reports for:
- Single-variable trend analyses
- Multi-variable comparative analyses (side-by-side)
- Multi-region spatial comparative analyses (side-by-side)
Followed by cross-comparison synthesis charts and diagnostics.
"""

from __future__ import annotations

import base64
import io
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.gridspec import GridSpec
import numpy as np
from PIL import Image

# Modern Corporate / Scientific Palette
COLOR_PRIMARY = "#1E293B"      # Deep Slate / Navy
COLOR_ACCENT = "#4338CA"       # Indigo
COLOR_SECONDARY = "#64748B"    # Muted Slate
COLOR_BG_CARD = "#F8FAFC"      # Clean Light Gray
COLOR_BORDER = "#E2E8F0"       # Subtle divider
COLOR_TEXT_DARK = "#0F172A"    # High contrast text
COLOR_SUCCESS = "#059669"      # Emerald Green
COLOR_WARNING = "#D97706"      # Amber

# Matplotlib's pyplot state is process-global and not thread-safe. The PDF
# route is a sync endpoint, so FastAPI runs it in a threadpool; serialise
# rendering so two concurrent exports cannot corrupt each other's figures.
_PDF_LOCK = threading.Lock()


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce a payload entry to a dict so rendering never assumes a shape."""
    return value if isinstance(value, dict) else {}


def _text(value: Any, default: str = "N/A") -> str:
    """Render a nullable payload field without ever calling ``.title()`` on None."""
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _number(value: Any, fmt: str = "{:.1f}", default: str = "N/A") -> str:
    """Format a possibly-missing numeric value (``None`` and NaN included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(float(value)):
        return default
    return fmt.format(float(value))


def _title(value: Any, default: str = "N/A") -> str:
    return _text(value, default).replace("_", " ").title()


def _trend_direction(stats: dict[str, Any]) -> str:
    """Read the Mann-Kendall direction across both current and legacy keys."""
    mk = stats.get("mann_kendall") or {}
    return _title(mk.get("trend_direction") or mk.get("trend"))


def _slope_value(stats: dict[str, Any]) -> float | None:
    lr = stats.get("linear_regression") or {}
    for key in ("slope_per_year", "slope_celsius_per_year", "slope"):
        value = lr.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _decode_b64_image(image_payload: Any) -> Image.Image | None:
    """Extract and decode a PIL Image from a base64 string or dict envelope."""
    if not image_payload:
        return None
    raw_b64 = None
    if isinstance(image_payload, dict):
        raw_b64 = (
            image_payload.get("image_base64")
            or image_payload.get("base64")
            or image_payload.get("image")
        )
    elif isinstance(image_payload, str):
        raw_b64 = image_payload

    if not raw_b64 or not isinstance(raw_b64, str):
        return None

    if "base64," in raw_b64:
        raw_b64 = raw_b64.split("base64,")[1]

    try:
        data = base64.b64decode(raw_b64.strip())
        return Image.open(io.BytesIO(data))
    except Exception:
        return None


def _add_page_header(fig: plt.Figure, title: str, subtitle: str, job_id: str) -> None:
    """Draw a clean, modern header bar across the top of the page."""
    title = _text(title, "Analysis Report")
    subtitle = _text(subtitle, "")
    job_id = _text(job_id, "N/A")
    # Top banner rect
    fig.text(0.06, 0.955, title.upper(), fontsize=15, fontweight="bold", color=COLOR_PRIMARY, family="sans-serif")
    fig.text(0.06, 0.935, subtitle, fontsize=9.5, color=COLOR_SECONDARY, family="sans-serif")
    
    # Job ID & timestamp badge on right
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fig.text(0.94, 0.955, f"JOB: {job_id[:16]}...", fontsize=8.5, fontweight="bold", color=COLOR_ACCENT, ha="right", family="sans-serif")
    fig.text(0.94, 0.935, f"Generated: {timestamp}", fontsize=8, color=COLOR_SECONDARY, ha="right", family="sans-serif")

    # Divider line
    line_ax = fig.add_axes([0.06, 0.925, 0.88, 0.002])
    line_ax.axhline(0, color=COLOR_BORDER, linewidth=1.5)
    line_ax.axis("off")


def _add_page_footer(fig: plt.Figure, page_num: int, total_pages: int | None = None) -> None:
    """Draw a footer with pagination and platform branding."""
    page_str = f"Page {page_num}" + (f" of {total_pages}" if total_pages else "")
    fig.text(0.06, 0.03, "EarthAlive Analysis Platform • Powered by NASA AppEEARS & LP DAAC", fontsize=8, color=COLOR_SECONDARY, family="sans-serif")
    fig.text(0.94, 0.03, page_str, fontsize=8, color=COLOR_SECONDARY, ha="right", family="sans-serif")


def _render_trend_report(pdf: PdfPages, result: dict[str, Any]) -> None:
    """Render a single-variable trend analysis report."""
    meta = result.get("metadata") or {}
    quality = result.get("data_quality") or {}
    stats = result.get("analysis_results") or {}
    vis = result.get("visualizations") or {}
    warnings = result.get("warnings") or []
    job_id = _text(result.get("job_id") or meta.get("job_id"), "N/A")

    # PAGE 1: Overview, KPI cards, and Trend charts
    fig = plt.figure(figsize=(8.5, 11), dpi=150)
    fig.patch.set_facecolor("#FFFFFF")
    _add_page_header(
        fig,
        title="Environmental Trend Analysis Report",
        subtitle=f"Variable: {_text(meta.get('display_name') or meta.get('variable'))} ({_text(meta.get('dataset'))})",
        job_id=job_id,
    )

    # Metadata Grid Box
    aoi = meta.get("aoi") or {}
    meta_text = (
        f"• Date Range: {_text(meta.get('start_date'))} → {_text(meta.get('end_date'))}  |  "
        f"Resolution: {_title(meta.get('temporal_resolution'), 'Daily')}  |  "
        f"Unit: {_text(meta.get('unit'))}\n"
        f"• AOI Bounds: West {aoi.get('west')}, South {aoi.get('south')}, "
        f"East {aoi.get('east')}, North {aoi.get('north')}\n"
        f"• Provider: NASA AppEEARS ARD  |  Spatial Strategy: {_title(meta.get('spatial_strategy'), 'Not applicable (daily)')}"
    )
    fig.text(
        0.06, 0.885, meta_text,
        fontsize=8.5, color=COLOR_TEXT_DARK, family="sans-serif",
        bbox=dict(boxstyle="round,pad=0.6", facecolor=COLOR_BG_CARD, edgecolor=COLOR_BORDER, linewidth=1),
    )

    # Data Quality & Statistical Summary Cards
    obs_total = quality.get("observation_count", "N/A")
    obs_valid = quality.get("valid_observation_count", "N/A")
    coverage_pct = _number(quality.get("data_coverage_percentage"), "{:.1f}", "0.0")

    # Extract Primary Stats
    lr = stats.get("linear_regression") or {}
    slope = _slope_value(stats)
    slope_str = f"{slope:+.4f}" if slope is not None else "N/A"
    r2_str = _number(lr.get("r_squared"), "{:.3f}")
    p_str = _number(lr.get("p_value"), "{:.4f}")
    trend_dir = _trend_direction(stats)

    kpi_text = (
        f"Data Coverage: {coverage_pct}% ({obs_valid}/{obs_total} valid days)    |    "
        f"Trend Direction: {trend_dir}    |    "
        f"Slope: {slope_str} {_text(meta.get('unit'), '')}/period    |    "
        f"R²: {r2_str} (p = {p_str})"
    )
    fig.text(
        0.06, 0.835, kpi_text,
        fontsize=9, fontweight="bold", color=COLOR_ACCENT, family="sans-serif",
        bbox=dict(boxstyle="round,pad=0.5", facecolor="#EEF2FF", edgecolor="#C7D2FE", linewidth=1),
    )

    # Visualizations (Time Series Trend & Mean Raster). The analysis layer emits
    # method-named charts (``linear_regression``, ``mann_kendall``) plus
    # ``heatmap``; the legacy keys are kept as fallbacks.
    img_trend = _decode_b64_image(
        vis.get("trend_line")
        or vis.get("time_series")
        or vis.get("linear_regression")
        or vis.get("mann_kendall")
        or vis.get("modified_mann_kendall")
        or vis.get("seasonal_mann_kendall")
        or vis.get("t_test")
    )
    img_map = _decode_b64_image(
        vis.get("mean_raster") or vis.get("spatial_distribution") or vis.get("heatmap")
    )

    if img_trend and img_map:
        # Stacked display: Trend on top, Map on bottom
        ax_top = fig.add_axes([0.06, 0.44, 0.88, 0.36])
        ax_top.imshow(img_trend)
        ax_top.axis("off")
        ax_top.set_title("Time Series Trend Analysis", fontsize=10, fontweight="bold", color=COLOR_PRIMARY, pad=6)

        ax_bottom = fig.add_axes([0.06, 0.08, 0.88, 0.33])
        ax_bottom.imshow(img_map)
        ax_bottom.axis("off")
        ax_bottom.set_title("Spatial Distribution (Mean Surface Raster)", fontsize=10, fontweight="bold", color=COLOR_PRIMARY, pad=6)
    elif img_trend:
        ax = fig.add_axes([0.06, 0.15, 0.88, 0.65])
        ax.imshow(img_trend)
        ax.axis("off")
    elif img_map:
        ax = fig.add_axes([0.06, 0.15, 0.88, 0.65])
        ax.imshow(img_map)
        ax.axis("off")
    else:
        fig.text(0.5, 0.5, "No visualization charts generated for this run.", ha="center", color=COLOR_SECONDARY)

    _add_page_footer(fig, page_num=1)
    pdf.savefig(fig)
    plt.close(fig)

    # PAGE 2: Detailed Statistics & Diagnostics (if warnings or multiple methods)
    if len(stats) > 1 or warnings:
        fig2 = plt.figure(figsize=(8.5, 11), dpi=150)
        fig2.patch.set_facecolor("#FFFFFF")
        _add_page_header(
            fig2,
            title="Statistical Summary & Data Diagnostics",
            subtitle=f"{meta.get('display_name', 'Analysis')} Statistical Methods Output",
            job_id=job_id,
        )

        # Statistical Methods Table
        rows = []
        for method_name, result_data in stats.items():
            if not isinstance(result_data, dict):
                continue
            name_clean = method_name.replace("_", " ").title()
            p_val_str = _number(result_data.get("p_value"), "{:.4e}")
            stat_summary = ", ".join(
                f"{k}: {v:.3f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else f"{k}: {v}"
                for k, v in list(result_data.items())[:3]
                if k not in ("warnings", "status")
            )
            rows.append([name_clean, _title(result_data.get("status"), "Calculated"), p_val_str, stat_summary])

        if rows:
            ax_tbl = fig2.add_axes([0.06, 0.60, 0.88, 0.28])
            ax_tbl.axis("off")
            tbl = ax_tbl.table(
                cellText=rows,
                colLabels=["Method", "Status", "P-Value", "Key Statistics"],
                cellLoc="left",
                colWidths=[0.22, 0.14, 0.16, 0.48],
                loc="upper center",
            )
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(8.5)
            tbl.scale(1.0, 1.8)
            # Header styling
            for col in range(4):
                cell = tbl[(0, col)]
                cell.set_facecolor(COLOR_PRIMARY)
                cell.set_text_props(color="white", weight="bold")

        # Warnings & Data Quality Remarks
        y_pos = 0.52
        fig2.text(0.06, y_pos, "DATA INTEGRITY & OBSERVATION NOTES:", fontsize=9.5, fontweight="bold", color=COLOR_PRIMARY)
        y_pos -= 0.03
        if warnings:
            for w in warnings[:8]:
                fig2.text(0.08, y_pos, f"• {w}", fontsize=8.5, color=COLOR_WARNING, family="sans-serif")
                y_pos -= 0.025
        else:
            fig2.text(0.08, y_pos, "• No data quality anomalies or observation gaps reported.", fontsize=8.5, color=COLOR_SUCCESS)

        _add_page_footer(fig2, page_num=2)
        pdf.savefig(fig2)
        plt.close(fig2)


def _render_comparison_report(pdf: PdfPages, result: dict[str, Any]) -> None:
    """Render a multi-variable or multi-region comparison with side-by-side data and images."""
    meta = result.get("metadata") or {}
    mode = _text(meta.get("mode"), "")
    job_id = _text(result.get("job_id") or meta.get("job_id"), "N/A")

    is_variable_comp = "variables" in result or mode == "variable_comparison"
    items_dict = result.get("variables") if is_variable_comp else result.get("regions")
    if not isinstance(items_dict, dict):
        items_dict = {}
    comparison = result.get("comparison") or {}
    comparison_vis = comparison.get("visualizations") or {}
    if not isinstance(comparison_vis, dict):
        comparison_vis = {}

    item_keys = list(items_dict.keys())

    # -------------------------------------------------------------
    # PAGE 1: Comparison Overview & Setup
    # -------------------------------------------------------------
    fig = plt.figure(figsize=(8.5, 11), dpi=150)
    fig.patch.set_facecolor("#FFFFFF")
    report_title = "Variable Comparison Report" if is_variable_comp else "Regional Spatial Comparison Report"
    subtitle_desc = f"Comparing {len(item_keys)} {'variables' if is_variable_comp else 'regions'}"
    _add_page_header(fig, title=report_title, subtitle=subtitle_desc, job_id=job_id)

    # Global Parameters Card
    aoi = meta.get("aoi") or {}
    aoi_desc = (
        f"West {aoi.get('west')}, South {aoi.get('south')}, East {aoi.get('east')}, North {aoi.get('north')}"
        if isinstance(aoi, dict) and aoi else "Multiple custom AOI polygons"
    )
    meta_box = (
        f"• Comparison Period: {_text(meta.get('start_date'))} → {_text(meta.get('end_date'))}\n"
        f"• Shared Resolution: {_title(meta.get('temporal_resolution'), 'Monthly')}  |  "
        f"Strategy: {_title(meta.get('spatial_strategy'), 'Not applicable (daily)')}\n"
        f"• Region/AOI: {aoi_desc}\n"
        f"• NASA Engine: AppEEARS Cloud ARD Pipeline"
    )
    fig.text(
        0.06, 0.88, meta_box,
        fontsize=9, color=COLOR_TEXT_DARK, family="sans-serif",
        bbox=dict(boxstyle="round,pad=0.7", facecolor=COLOR_BG_CARD, edgecolor=COLOR_BORDER, linewidth=1),
    )

    # Comparative Metric Table across all items
    tbl_rows = []
    for k in item_keys:
        item = items_dict[k] if isinstance(items_dict[k], dict) else {}
        m = item.get("metadata") or {}
        label = _text(m.get("variable_id") or m.get("display_name"), str(k))
        dataset = _text(m.get("dataset"))
        unit = _text(m.get("unit"), "")
        q = item.get("data_quality") or {}
        cov = _number(q.get("data_coverage_percentage"), "{:.1f}", "0.0")
        valid_cnt = q.get("valid_observation_count", "N/A")

        # Trend & Slope
        results = item.get("analysis_results") or {}
        slope = _slope_value(results)
        slope_str = f"{slope:+.3e}" if slope is not None else "N/A"
        trend = _trend_direction(results)

        tbl_rows.append([str(label).upper(), dataset, f"{cov}% ({valid_cnt})", slope_str, trend, unit])

    if tbl_rows:
        ax_overview = fig.add_axes([0.06, 0.56, 0.88, 0.28])
        ax_overview.axis("off")
        overview_tbl = ax_overview.table(
            cellText=tbl_rows,
            colLabels=["Item", "Dataset", "Valid Data", "Slope", "Trend", "Unit"],
            cellLoc="left",
            colWidths=[0.24, 0.16, 0.18, 0.16, 0.14, 0.12],
            loc="upper center",
        )
        overview_tbl.auto_set_font_size(False)
        overview_tbl.set_fontsize(8.5)
        overview_tbl.scale(1.0, 1.8)
        for col in range(6):
            cell = overview_tbl[(0, col)]
            cell.set_facecolor(COLOR_PRIMARY)
            cell.set_text_props(color="white", weight="bold")

    # If comparison synthesis chart is available on Page 1
    multi_chart = _decode_b64_image(
        comparison_vis.get("multi_variable_series")
        or comparison_vis.get("multi_region_series")
        or comparison_vis.get("overview")
        # ``comparison_charts`` emits this key for both comparison modes.
        or comparison_vis.get("timeseries_comparison")
    )
    if multi_chart:
        ax_chart1 = fig.add_axes([0.06, 0.10, 0.88, 0.42])
        ax_chart1.imshow(multi_chart)
        ax_chart1.axis("off")
        ax_chart1.set_title("Cross-Series Normalized Comparison Overlay", fontsize=10, fontweight="bold", color=COLOR_PRIMARY, pad=6)

    _add_page_footer(fig, page_num=1)
    pdf.savefig(fig)
    plt.close(fig)

    # -------------------------------------------------------------
    # PAGES 2+: Side-by-Side Data & Images for Pairs of Entities
    # -------------------------------------------------------------
    page_counter = 2
    # Pair items: [ (0, 1), (2, 3), ... ]
    for i in range(0, len(item_keys), 2):
        fig_pair = plt.figure(figsize=(8.5, 11), dpi=150)
        fig_pair.patch.set_facecolor("#FFFFFF")

        k1 = item_keys[i]
        k2 = item_keys[i + 1] if i + 1 < len(item_keys) else None

        subtitle_pair = f"Side-by-Side Analysis: {str(k1).upper()}" + (
            f" vs {str(k2).upper()}" if k2 else ""
        )
        _add_page_header(fig_pair, title="Comparative Entity Diagnostics", subtitle=subtitle_pair, job_id=job_id)

        # Left Column: Item 1
        _render_entity_column(fig_pair, _as_dict(items_dict.get(k1)), col_x=0.06, col_w=0.42, label=str(k1))

        # Right Column: Item 2 (if present)
        if k2:
            _render_entity_column(fig_pair, _as_dict(items_dict.get(k2)), col_x=0.52, col_w=0.42, label=str(k2))
        else:
            fig_pair.text(0.70, 0.50, "[ Single entity on this page ]", ha="center", color=COLOR_SECONDARY)

        _add_page_footer(fig_pair, page_num=page_counter)
        pdf.savefig(fig_pair)
        plt.close(fig_pair)
        page_counter += 1

    # -------------------------------------------------------------
    # FINAL PAGE: Comparison Synthesis Visualizations
    # -------------------------------------------------------------
    # Find all comparison synthesis charts (correlation matrix, scatter, etc.)
    comp_images = []
    for chart_key, chart_data in comparison_vis.items():
        if chart_key == "timeseries_comparison":
            # Already rendered on the overview page.
            continue
        img = _decode_b64_image(chart_data)
        if img:
            comp_images.append((chart_key.replace("_", " ").title(), img))

    if comp_images:
        fig_synth = plt.figure(figsize=(8.5, 11), dpi=150)
        fig_synth.patch.set_facecolor("#FFFFFF")
        _add_page_header(
            fig_synth,
            title="Comparison Synthesis & Cross-Correlation",
            subtitle="Cross-entity statistical relationship and correlation matrices",
            job_id=job_id,
        )

        # Render 1 or 2 synthesis images
        if len(comp_images) == 1:
            title, img = comp_images[0]
            ax = fig_synth.add_axes([0.08, 0.20, 0.84, 0.65])
            ax.imshow(img)
            ax.axis("off")
            ax.set_title(title, fontsize=11, fontweight="bold", color=COLOR_PRIMARY, pad=8)
        else:
            # Top chart
            title1, img1 = comp_images[0]
            ax1 = fig_synth.add_axes([0.08, 0.52, 0.84, 0.36])
            ax1.imshow(img1)
            ax1.axis("off")
            ax1.set_title(title1, fontsize=10, fontweight="bold", color=COLOR_PRIMARY, pad=6)

            # Bottom chart
            title2, img2 = comp_images[1]
            ax2 = fig_synth.add_axes([0.08, 0.10, 0.84, 0.36])
            ax2.imshow(img2)
            ax2.axis("off")
            ax2.set_title(title2, fontsize=10, fontweight="bold", color=COLOR_PRIMARY, pad=6)

        _add_page_footer(fig_synth, page_num=page_counter)
        pdf.savefig(fig_synth)
        plt.close(fig_synth)


def _render_entity_column(
    fig: plt.Figure,
    entity_data: dict[str, Any],
    col_x: float,
    col_w: float,
    label: str,
) -> None:
    """Render one entity's statistics, metadata, and charts in a side-by-side column."""
    meta = entity_data.get("metadata") or {}
    quality = entity_data.get("data_quality") or {}
    stats = entity_data.get("analysis_results") or {}
    vis = entity_data.get("visualizations") or {}

    disp_name = _text(meta.get("display_name"), label.upper())
    unit = _text(meta.get("unit"), "")
    dataset = _text(meta.get("dataset"))

    # Column Title Card
    fig.text(col_x, 0.885, disp_name, fontsize=11, fontweight="bold", color=COLOR_PRIMARY)
    fig.text(col_x, 0.865, f"Dataset: {dataset}  |  Unit: {unit}", fontsize=8, color=COLOR_SECONDARY)

    # Key Statistics & Quality Box
    cov = _number(quality.get("data_coverage_percentage"), "{:.1f}", "0.0")
    v_obs = quality.get("valid_observation_count", "N/A")
    t_obs = quality.get("observation_count", "N/A")
    slope = _slope_value(stats)
    slope_str = f"{slope:+.4f}" if slope is not None else "N/A"
    p_str = _number((stats.get("linear_regression") or {}).get("p_value"), "{:.4f}")
    trend = _trend_direction(stats)

    stats_card = (
        f"Valid Obs: {v_obs}/{t_obs} ({cov}%)\n"
        f"Trend: {trend}\n"
        f"Slope: {slope_str} {unit}\n"
        f"Significance: p = {p_str}"
    )
    fig.text(
        col_x, 0.775, stats_card,
        fontsize=8, color=COLOR_TEXT_DARK, family="sans-serif",
        bbox=dict(boxstyle="round,pad=0.5", facecolor=COLOR_BG_CARD, edgecolor=COLOR_BORDER, linewidth=1),
    )

    # Entity Images: Time series chart and Raster Map
    img_time = _decode_b64_image(
        vis.get("trend_line")
        or vis.get("time_series")
        or vis.get("linear_regression")
        or vis.get("mann_kendall")
    )
    img_map = _decode_b64_image(
        vis.get("mean_raster") or vis.get("spatial_distribution") or vis.get("heatmap")
    )

    if img_time:
        ax_t = fig.add_axes([col_x, 0.44, col_w, 0.30])
        ax_t.imshow(img_time)
        ax_t.axis("off")
        ax_t.set_title("Time Series Trend", fontsize=8.5, fontweight="bold", color=COLOR_PRIMARY, pad=4)

    if img_map:
        ax_m = fig.add_axes([col_x, 0.10, col_w, 0.30])
        ax_m.imshow(img_map)
        ax_m.axis("off")
        ax_m.set_title("Spatial Distribution Map", fontsize=8.5, fontweight="bold", color=COLOR_PRIMARY, pad=4)


def build_analysis_pdf(result: dict[str, Any]) -> bytes:
    """Generate a high-resolution, multi-page PDF report from an analysis result.

    Supports:
    - Single-variable trend analyses (`trend_analysis`)
    - Multi-variable comparisons (`variable_comparison`) with side-by-side layout
    - Multi-region comparisons (`region_comparison`) with side-by-side layout
    """
    buffer = io.BytesIO()
    with _PDF_LOCK:
        with PdfPages(buffer) as pdf:
            meta = result.get("metadata") or {}
            mode = _text(meta.get("mode"), "")

            if "variables" in result or "regions" in result or "comparison" in mode:
                _render_comparison_report(pdf, result)
            else:
                _render_trend_report(pdf, result)

        pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes

