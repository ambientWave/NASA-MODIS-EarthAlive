from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio

from app.services.modis_processor import (
    bounds_for_egypt,
    convert_kelvin_to_celsius,
    mandatory_qa_mask,
    valid_lst_mask,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = PROJECT_ROOT / "data" / "processed" / "egypt" / "egypt_modis_report_2024_01_01.json"
RASTER_PATH = PROJECT_ROOT / "data" / "processed" / "egypt" / "egypt_lst_day_2024_01_01.tif"


def test_qc_bits_represent_mandatory_qa_only():
    qc = np.array([0, 1, 2, 3, 4, 5, 6, 7], dtype=np.uint8)
    mask = mandatory_qa_mask(qc)

    expected = np.array([True, False, False, False, True, False, False, False])
    np.testing.assert_array_equal(mask, expected)


def test_valid_lst_mask_excludes_fill_and_invalid_values():
    raw = np.array([0, 7500, 8000, 65535, 65536, 7499, 1], dtype=np.int32)
    valid = valid_lst_mask(raw)

    expected = np.array([False, True, True, True, False, False, False])
    np.testing.assert_array_equal(valid, expected)


def test_kelvin_to_celsius_is_applied_once():
    raw = np.array([13657, 15000, 15500], dtype=np.float32)
    converted = convert_kelvin_to_celsius(raw)
    expected = np.array([-0.010009765625, 26.85, 36.85], dtype=np.float32)
    np.testing.assert_allclose(converted, expected, rtol=1e-6, atol=1e-6)

    twice_applied = (raw * 0.02 - 273.15) * 0.02 - 273.15
    assert not np.allclose(converted, twice_applied, rtol=1e-6, atol=1e-6)


def test_final_egypt_raster_is_wgs84_and_cropped_to_aoi():
    assert RASTER_PATH.exists(), "Egypt raster not found; run the MODIS pipeline first."
    with rasterio.open(RASTER_PATH) as src:
        arr = src.read(1)
        assert src.crs.to_string() == "EPSG:4326"
        assert src.bounds.left == 24.7
        assert src.bounds.bottom == 22.0
        assert src.bounds.right == 36.9
        assert src.bounds.top == 31.7
        assert arr.shape[0] > 0
        assert arr.shape[1] > 0
        assert np.isfinite(arr).any()


def test_report_contains_required_scientific_summary_fields():
    assert REPORT_PATH.exists(), "Egypt report not found; run the MODIS pipeline first."
    with REPORT_PATH.open("r", encoding="utf-8") as handle:
        report = json.load(handle)

    required = {
        "date",
        "product_name",
        "collection_version",
        "bounding_box",
        "granules_downloaded",
        "valid_pixels",
        "good_quality_pixels",
        "mean_temperature_celsius",
        "minimum_temperature_celsius",
        "maximum_temperature_celsius",
        "crs",
        "processing_warnings",
    }
    missing = required - set(report)
    assert not missing, f"Missing required report fields: {sorted(missing)}"

    assert report["crs"] == "EPSG:4326"
    assert report["bounding_box"] == {"west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7}
    assert report["granules_downloaded"] >= 1
    assert report["valid_pixels"] > 0
    assert report["mean_temperature_celsius"] > -100
    assert report["minimum_temperature_celsius"] <= report["maximum_temperature_celsius"]


def test_aoi_bounds_match_expected_egypt_box():
    bounds = bounds_for_egypt()
    expected = (24.7, 22.0, 36.9, 31.7)
    assert bounds == expected
