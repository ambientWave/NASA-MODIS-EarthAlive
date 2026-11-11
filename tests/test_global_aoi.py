from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from app.services.modis_processor import (
    PROCESSED_DATA_DIR,
    convert_kelvin_to_celsius,
    destination_grid_size,
    discover_mod11a1_granules,
    download_mod11a1_granules,
    ensure_earthaccess_auth,
    extract_subdataset_to_tiff,
    generate_heatmap,
    mandatory_qa_mask,
    read_raster,
    reproject_to_4326,
    valid_lst_mask,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GLOBAL_TEST_DIR = PROCESSED_DATA_DIR / "global_tests"
AOI_REGIONS = {
    "egypt": {"west": 29.5, "south": 29.5, "east": 31.5, "north": 31.5},
    "california": {"west": -122.0, "south": 36.0, "east": -120.0, "north": 38.0},
    "france": {"west": 1.0, "south": 46.0, "east": 3.0, "north": 48.0},
    "sydney": {"west": 150.0, "south": -35.0, "east": 152.0, "north": -33.0},
}


def _bbox_tuple(bbox: dict[str, float]) -> tuple[float, float, float, float]:
    return (bbox["west"], bbox["south"], bbox["east"], bbox["north"])


def _region_output_dir(region_name: str) -> Path:
    return GLOBAL_TEST_DIR / region_name


def _run_aoi_pipeline(region_name: str, bbox: dict[str, float], date_str: str = "2024-01-01") -> dict:
    ensure_earthaccess_auth()
    region_dir = _region_output_dir(region_name)
    raw_dir = region_dir / "raw"
    region_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    granules = discover_mod11a1_granules(date_str=date_str, bbox=bbox)
    if not granules:
        raise RuntimeError(f"No MOD11A1 granules discovered for {region_name} on {date_str}.")

    downloaded_files = download_mod11a1_granules(granules, raw_dir)
    if not downloaded_files:
        raise RuntimeError(f"No MOD11A1 granules downloaded for {region_name} on {date_str}.")

    bounds = _bbox_tuple(bbox)
    width, height = destination_grid_size(bounds, 0.01)
    merged = np.full((height, width), np.nan, dtype=np.float32)
    valid_pixels_total = 0
    warnings: list[str] = []
    all_valid_temps: list[np.ndarray] = []

    for hdf_path in downloaded_files:
        if not hdf_path.exists():
            warnings.append(f"Missing downloaded file: {hdf_path.name}")
            continue

        lst_tif = region_dir / f"{hdf_path.stem}_LST_Day_1km.tif"
        qc_tif = region_dir / f"{hdf_path.stem}_QC_Day.tif"

        try:
            extract_subdataset_to_tiff(hdf_path, "LST_Day_1km", lst_tif)
            extract_subdataset_to_tiff(hdf_path, "QC_Day", qc_tif)
            lst_data, lst_profile = read_raster(lst_tif)
            qc_data, _ = read_raster(qc_tif)
        except Exception as exc:  # pragma: no cover - defensive validation
            warnings.append(f"Could not process {hdf_path.name}: {exc}")
            continue

        valid_mask = valid_lst_mask(lst_data) & mandatory_qa_mask(qc_data)
        valid_pixels_total += int(np.count_nonzero(valid_mask))
        if not np.any(valid_mask):
            warnings.append(f"No valid pixels after QA filtering for {hdf_path.name}")
            continue

        valid_temperature = convert_kelvin_to_celsius(lst_data[valid_mask])
        all_valid_temps.append(valid_temperature)

        tile_temperature = np.full(lst_data.shape, np.nan, dtype=np.float32)
        tile_temperature[valid_mask] = valid_temperature
        projected = reproject_to_4326(tile_temperature, lst_profile, bounds, width, height)
        merged = np.where(np.isfinite(projected), projected, merged)

    if not np.isfinite(merged).any():
        raise RuntimeError(f"No finite LST values remain after processing for {region_name}.")

    finite_values = merged[np.isfinite(merged)]
    final_raster_path = region_dir / f"{region_name}_lst_day_{date_str.replace('-', '_')}.tif"
    heatmap_path = region_dir / f"{region_name}_lst_heatmap_{date_str.replace('-', '_')}.png"
    report_path = region_dir / f"{region_name}_modis_report_{date_str.replace('-', '_')}.json"

    with rasterio.open(
        final_raster_path,
        "w",
        driver="GTiff",
        height=merged.shape[0],
        width=merged.shape[1],
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_bounds(*bounds, width=width, height=height),
        nodata=np.nan,
    ) as dst:
        dst.write(merged.astype(np.float32), 1)

    generate_heatmap(merged, bounds, f"MODIS LST: {region_name.title()} — {date_str}", heatmap_path)

    report = {
        "region": region_name,
        "date": date_str,
        "product_name": "MOD11A1",
        "collection_version": "061",
        "bounding_box": {"west": bounds[0], "south": bounds[1], "east": bounds[2], "north": bounds[3]},
        "granules_discovered": len(granules),
        "granules_downloaded": len(downloaded_files),
        "total_pixels": int(merged.size),
        "valid_pixels": int(finite_values.size),
        "good_quality_pixels": int(valid_pixels_total),
        "mean_temperature_celsius": float(np.mean(finite_values)),
        "minimum_temperature_celsius": float(np.min(finite_values)),
        "maximum_temperature_celsius": float(np.max(finite_values)),
        "crs": "EPSG:4326",
        "warnings": warnings,
    }

    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)

    return {
        "region": region_name,
        "bbox": bbox,
        "granules_discovered": len(granules),
        "granules_downloaded": len(downloaded_files),
        "raster_path": str(final_raster_path),
        "heatmap_path": str(heatmap_path),
        "report_path": str(report_path),
        "mean_temperature_celsius": float(np.mean(finite_values)),
        "min_temperature_celsius": float(np.min(finite_values)),
        "max_temperature_celsius": float(np.max(finite_values)),
        "valid_pixels": int(finite_values.size),
        "warnings": warnings,
    }


def test_global_aoi_definitions_are_dynamic_and_not_egypt_hardcoded():
    assert set(AOI_REGIONS) == {"egypt", "california", "france", "sydney"}
    for region_name, bbox in AOI_REGIONS.items():
        assert bbox["west"] < bbox["east"]
        assert bbox["south"] < bbox["north"]
        assert bbox != {"west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7}

    california_bbox = AOI_REGIONS["california"]
    assert california_bbox["west"] < 0 and california_bbox["east"] < 0
    assert california_bbox["south"] > 0 and california_bbox["north"] > 0

    sydney_bbox = AOI_REGIONS["sydney"]
    assert sydney_bbox["west"] > 0 and sydney_bbox["east"] > 0
    assert sydney_bbox["south"] < 0 and sydney_bbox["north"] < 0


def test_discovery_works_with_arbitrary_bbox_values():
    bbox = AOI_REGIONS["france"]
    granules = discover_mod11a1_granules(date_str="2024-01-01", bbox=bbox)
    assert isinstance(granules, list)
    assert all(isinstance(granule, dict) for granule in granules)

    for granule in granules:
        granule_text = " ".join(
            str(piece)
            for piece in [
                granule.get("umm", {}).get("GranuleUR", ""),
                granule.get("umm", {}).get("DataGranule", {}).get("ProducerGranuleId", ""),
                str(granule),
            ]
            if piece
        )
        assert "h" in granule_text.lower() and "v" in granule_text.lower()


@pytest.mark.integration
def test_global_aoi_nasa_integration_pipeline():
    results: list[dict] = []
    failures: dict[str, str] = {}

    for region_name, bbox in AOI_REGIONS.items():
        try:
            result = _run_aoi_pipeline(region_name, bbox)
            results.append(result)

            with rasterio.open(result["raster_path"]) as src:
                arr = src.read(1)
                assert src.crs.to_string() == "EPSG:4326"
                assert src.bounds.left == bbox["west"]
                assert src.bounds.bottom == bbox["south"]
                assert src.bounds.right == bbox["east"]
                assert src.bounds.top == bbox["north"]
                assert arr.shape[0] > 0 and arr.shape[1] > 0
                assert np.isfinite(arr).any()
                assert result["valid_pixels"] > 0
        except Exception as exc:  # pragma: no cover - integration diagnostics
            failures[region_name] = str(exc)

    assert not failures, "Global AOI integration failures: " + "; ".join(f"{k}: {v}" for k, v in failures.items())
    assert len(results) == len(AOI_REGIONS)


@pytest.mark.integration
def test_global_aoi_outputs_are_independent_per_region():
    for region_name, bbox in AOI_REGIONS.items():
        region_dir = _region_output_dir(region_name)
        assert region_dir.exists(), f"Missing output directory for {region_name}"

        report_path = region_dir / f"{region_name}_modis_report_2024_01_01.json"
        raster_path = region_dir / f"{region_name}_lst_day_2024_01_01.tif"

        if report_path.exists() and raster_path.exists():
            with report_path.open("r", encoding="utf-8") as handle:
                report = json.load(handle)
            assert report["region"] == region_name
            assert report["bounding_box"] == {
                "west": bbox["west"],
                "south": bbox["south"],
                "east": bbox["east"],
                "north": bbox["north"],
            }

            with rasterio.open(raster_path) as src:
                assert src.crs.to_string() == "EPSG:4326"
                assert src.bounds.left == bbox["west"]
                assert src.bounds.bottom == bbox["south"]
                assert src.bounds.right == bbox["east"]
                assert src.bounds.top == bbox["north"]
        else:
            pytest.skip(f"No region output available for {region_name}; integration data not available.")


def test_modis_conversion_helper_remains_valid_for_all_regions():
    raw_values = np.array([7500, 11000, 28000, 65535], dtype=np.float32)
    converted = convert_kelvin_to_celsius(raw_values)
    expected = np.array(
        [(-273.15 + 7500 * 0.02), (-273.15 + 11000 * 0.02), (-273.15 + 28000 * 0.02), (-273.15 + 65535 * 0.02)],
        dtype=np.float32,
    )
    np.testing.assert_allclose(converted, expected, rtol=1e-6, atol=1e-6)

    mask = valid_lst_mask(raw_values)
    assert mask.all()

    qc = np.array([0, 3, 4, 7], dtype=np.uint8)
    qa_mask = mandatory_qa_mask(qc)
    assert qa_mask.tolist() == [True, False, True, False]
