"""Standalone QA inspection and heatmap generation for MOD11A1 rasters.

This is a developer/CLI utility, not part of the HTTP API. Paths are arguments
with project-rooted defaults, and output directories are created on demand.

Usage:
    python -m app.services.raster_processor
    python -m app.services.raster_processor --lst path/to/LST.tif --qc path/to/QC.tif --output out/heatmap.png
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio


from app.services.modis_processor import PROCESSED_DATA_DIR
from app.utils.paths import ensure_parent_directory, require_file


DEFAULT_LST_PATH = PROCESSED_DATA_DIR / "lst_day_2024_01_01.tif"
DEFAULT_QC_PATH = PROCESSED_DATA_DIR / "qc_day_2024_01_01.tif"
DEFAULT_HEATMAP_PATH = PROCESSED_DATA_DIR / "lst_heatmap.png"
DEFAULT_TITLE = "MODIS Land Surface Temperature"

# MOD11A1 LST DN validity range and scaling (DN * 0.02 - 273.15 = °C).
VALID_LST_MIN = 7500
VALID_LST_MAX = 65535
LST_SCALE = 0.02
KELVIN_OFFSET = 273.15

# Mandatory QA bits are the two least-significant bits of the QC layer.
MANDATORY_QA_MASK = 0b00000011


def _load_rasters(lst_path: str | Path, qc_path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Read the LST and QC layers, failing early if either file is missing."""
    lst_file = require_file(lst_path, "LST raster")
    qc_file = require_file(qc_path, "QC raster")

    with rasterio.open(lst_file) as lst_src:
        lst = lst_src.read(1)
    with rasterio.open(qc_file) as qc_src:
        qc = qc_src.read(1)
    return lst, qc


def _valid_pixel_masks(
    lst: np.ndarray, qc: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (LST-valid, good-quality, final valid) pixel masks."""
    valid_lst = (lst >= VALID_LST_MIN) & (lst <= VALID_LST_MAX)
    good_quality = (qc & MANDATORY_QA_MASK) == 0
    return valid_lst, good_quality, valid_lst & good_quality


def analyze_lst_quality(
    lst_path: str | Path = DEFAULT_LST_PATH,
    qc_path: str | Path = DEFAULT_QC_PATH,
) -> dict[str, Any]:
    """Summarize pixel validity and LST statistics for a MOD11A1 raster pair.

    Returns the statistics as a dictionary; also prints a human-readable summary.
    Missing input files raise ``FileNotFoundError`` with the resolved path.
    """
    lst, qc = _load_rasters(lst_path, qc_path)
    valid_lst, _good_quality, valid_pixels = _valid_pixel_masks(lst, qc)

    temperature_celsius = lst[valid_pixels] * LST_SCALE - KELVIN_OFFSET

    result: dict[str, Any] = {
        "lst_path": str(Path(lst_path).resolve()),
        "qc_path": str(Path(qc_path).resolve()),
        "total_pixels": int(lst.size),
        "valid_lst_pixels": int(np.count_nonzero(valid_lst)),
        "good_quality_pixels": int(np.count_nonzero(valid_pixels)),
        "mean_temperature_celsius": None,
        "minimum_temperature_celsius": None,
        "maximum_temperature_celsius": None,
    }
    if temperature_celsius.size > 0:
        result.update(
            mean_temperature_celsius=float(temperature_celsius.mean()),
            minimum_temperature_celsius=float(temperature_celsius.min()),
            maximum_temperature_celsius=float(temperature_celsius.max()),
        )

    print("\n========== LST QUALITY ANALYSIS ==========")
    print("Total pixels:", result["total_pixels"])
    print("Valid LST pixels:", result["valid_lst_pixels"])
    print("Good quality pixels:", result["good_quality_pixels"])
    if temperature_celsius.size > 0:
        print("Mean temperature:", result["mean_temperature_celsius"])
        print("Minimum temperature:", result["minimum_temperature_celsius"])
        print("Maximum temperature:", result["maximum_temperature_celsius"])
    else:
        print("No valid temperature pixels found.")
    print("==========================================")

    return result


def plot_temperature(
    lst_path: str | Path = DEFAULT_LST_PATH,
    qc_path: str | Path = DEFAULT_QC_PATH,
    output_path: str | Path = DEFAULT_HEATMAP_PATH,
    title: str = DEFAULT_TITLE,
) -> Path:
    """Render a QA-filtered LST heatmap and return the written file path.

    The output directory is created if it does not exist. The Agg backend is used
    so this works in headless/server environments.
    """
    lst, qc = _load_rasters(lst_path, qc_path)
    _valid_lst, _good_quality, valid_pixels = _valid_pixel_masks(lst, qc)

    temperature = np.where(
        valid_pixels,
        lst * LST_SCALE - KELVIN_OFFSET,
        np.nan,
    )

    output_file = ensure_parent_directory(output_path)

    fig, ax = plt.subplots(figsize=(10, 8))
    image = ax.imshow(temperature, cmap="inferno")
    fig.colorbar(image, ax=ax, label="Temperature (°C)")
    ax.set_title(title)
    ax.set_xlabel("Pixel Column")
    ax.set_ylabel("Pixel Row")
    fig.tight_layout()
    fig.savefig(output_file, dpi=150, bbox_inches="tight")
    plt.close(fig)

    print(f"Heatmap saved to {output_file}")
    return output_file


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect MOD11A1 LST/QC rasters and render a temperature heatmap."
    )
    parser.add_argument(
        "--lst",
        type=Path,
        default=DEFAULT_LST_PATH,
        help=f"Path to the LST GeoTIFF (default: {DEFAULT_LST_PATH}).",
    )
    parser.add_argument(
        "--qc",
        type=Path,
        default=DEFAULT_QC_PATH,
        help=f"Path to the QC GeoTIFF (default: {DEFAULT_QC_PATH}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_HEATMAP_PATH,
        help=f"Heatmap output path (default: {DEFAULT_HEATMAP_PATH}).",
    )
    parser.add_argument(
        "--title",
        default=DEFAULT_TITLE,
        help="Heatmap title.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stats-only", action="store_true", help="Print statistics without writing a heatmap.")
    mode.add_argument("--plot-only", action="store_true", help="Write the heatmap without printing statistics.")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    if not args.plot_only:
        analyze_lst_quality(args.lst, args.qc)
    if not args.stats_only:
        plot_temperature(args.lst, args.qc, args.output, args.title)


if __name__ == "__main__":
    main()
