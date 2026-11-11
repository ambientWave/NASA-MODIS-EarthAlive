from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.services import raster_processor
from app.utils.paths import ensure_directory, ensure_parent_directory, require_file


def test_ensure_directory_creates_nested_directories(tmp_path):
    target = tmp_path / "a" / "b" / "c"

    assert ensure_directory(target) == target
    assert target.is_dir()
    # Idempotent: calling again must not raise.
    assert ensure_directory(target) == target


def test_ensure_parent_directory_creates_parent_only(tmp_path):
    file_path = tmp_path / "nested" / "deep" / "heatmap.png"

    result = ensure_parent_directory(file_path)

    assert result == file_path
    assert file_path.parent.is_dir()
    assert not file_path.exists()


def test_require_file_raises_with_resolved_path(tmp_path):
    missing = tmp_path / "missing.tif"

    with pytest.raises(FileNotFoundError, match="LST raster not found"):
        require_file(missing, "LST raster")


def test_default_paths_are_absolute_and_project_rooted():
    assert raster_processor.DEFAULT_LST_PATH.is_absolute()
    assert raster_processor.DEFAULT_QC_PATH.is_absolute()
    assert raster_processor.DEFAULT_HEATMAP_PATH.is_absolute()


def _write_raster(path: Path, array: np.ndarray) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=array.shape[0],
        width=array.shape[1],
        count=1,
        dtype=array.dtype,
        crs="EPSG:4326",
        transform=from_origin(10, 20, 1, 1),
    ) as dst:
        dst.write(array, 1)


@pytest.fixture
def lst_and_qc(tmp_path: Path) -> tuple[Path, Path]:
    # 0 is fill and must be excluded; QC = 3 marks the last pixel bad.
    lst = np.array([[13657, 15000], [0, 20000]], dtype=np.float32)
    qc = np.array([[0, 0], [0, 3]], dtype=np.uint8)
    lst_path = tmp_path / "lst_day.tif"
    qc_path = tmp_path / "qc_day.tif"
    _write_raster(lst_path, lst)
    _write_raster(qc_path, qc)
    return lst_path, qc_path


def test_analyze_lst_quality_uses_provided_paths(lst_and_qc):
    lst_path, qc_path = lst_and_qc

    result = raster_processor.analyze_lst_quality(lst_path, qc_path)

    assert result["lst_path"] == str(lst_path.resolve())
    assert result["qc_path"] == str(qc_path.resolve())
    assert result["total_pixels"] == 4
    assert result["valid_lst_pixels"] == 3
    assert result["good_quality_pixels"] == 2
    assert result["mean_temperature_celsius"] == pytest.approx(
        ((-0.010009765625) + 26.85) / 2, abs=1e-4
    )
    assert result["minimum_temperature_celsius"] < result["maximum_temperature_celsius"]


def test_analyze_lst_quality_reports_missing_input(tmp_path):
    with pytest.raises(FileNotFoundError, match="LST raster not found"):
        raster_processor.analyze_lst_quality(tmp_path / "absent.tif", tmp_path / "absent_qc.tif")


def test_plot_temperature_creates_missing_output_directory(lst_and_qc, tmp_path):
    lst_path, qc_path = lst_and_qc
    output_path = tmp_path / "reports" / "nested" / "heatmap.png"

    written = raster_processor.plot_temperature(
        lst_path, qc_path, output_path, title="Test LST heatmap"
    )

    assert written == output_path
    assert output_path.is_file()
    assert output_path.stat().st_size > 0


def test_cli_parser_accepts_explicit_paths():
    parser = raster_processor._build_parser()

    args = parser.parse_args(
        ["--lst", "a.tif", "--qc", "b.tif", "--output", "out.png", "--stats-only"]
    )

    assert args.lst == Path("a.tif")
    assert args.qc == Path("b.tif")
    assert args.output == Path("out.png")
    assert args.stats_only is True
