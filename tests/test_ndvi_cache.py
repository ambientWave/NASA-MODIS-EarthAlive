from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from app.services import modis_processor
from app.services.variables import ndvi
from app.services.variables.base import CollectionRequest
from app.utils import cache


def _write_raster(path: Path, values: np.ndarray, dtype: str, nodata: float) -> None:
    height, width = values.shape
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": dtype,
        "nodata": nodata,
        "crs": "EPSG:4326",
        "transform": from_bounds(30.0, 29.0, 31.0, 30.0, width, height),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values.astype(dtype), 1)


@pytest.fixture
def cache_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ANALYSIS_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path / "cache"


def test_aoi_key_is_stable_and_aoi_specific():
    bounds = (24.7, 22.0, 36.9, 31.7)
    first = ndvi._aoi_key(bounds, 0.005)
    assert first == ndvi._aoi_key(bounds, 0.005)
    assert first != ndvi._aoi_key((24.7, 22.0, 36.8, 31.7), 0.005)
    assert first != ndvi._aoi_key(bounds, 0.0025)


def test_waves_never_split_a_composite():
    grouped = {
        "2024-01-01": ["a", "b", "c", "d"],
        "2024-01-17": ["e", "f", "g", "h"],
        "2024-02-02": ["i", "j", "k", "l"],
    }
    dates = sorted(grouped)

    waves = ndvi._waves(dates, grouped, max_granules=4)

    assert waves == [["2024-01-01"], ["2024-01-17"], ["2024-02-02"]]
    assert all(len(wave) >= 1 for wave in waves)


def test_waves_include_every_date_exactly_once():
    grouped = {f"2024-01-{day:02d}": ["a", "b"] for day in range(1, 10)}
    dates = sorted(grouped)

    waves = ndvi._waves(dates, grouped, max_granules=8)

    flattened = [item for wave in waves for item in wave]
    assert flattened == dates


def test_composite_mean_merges_tiles_and_masks_qa(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYSIS_CACHE_DIR", str(tmp_path / "cache"))
    width, height = 4, 2

    ndvi_a = np.full((height, width), -32768, dtype=np.int16)
    ndvi_a[:, :2] = 3000  # NDVI 0.3 in the western half
    qa_a = np.zeros((height, width), dtype=np.uint8)

    ndvi_b = np.full((height, width), -32768, dtype=np.int16)
    ndvi_b[:, 2:] = 6000  # NDVI 0.6 in the eastern half
    qa_b = np.zeros((height, width), dtype=np.uint8)
    qa_b[0, 2] = 3  # cloudy: must be dropped

    paths = []
    for index, (ndvi_values, qa_values) in enumerate([(ndvi_a, qa_a), (ndvi_b, qa_b)]):
        ndvi_path = tmp_path / f"tile{index}_ndvi.tif"
        qa_path = tmp_path / f"tile{index}_qa.tif"
        _write_raster(ndvi_path, ndvi_values, "int16", -32768)
        _write_raster(qa_path, qa_values, "uint8", 255)
        paths.append((ndvi_path, qa_path))

    value, valid, total, granules, merged = ndvi._composite_mean(paths, width, height)

    assert total == width * height
    assert valid == 7  # 8 pixels minus the one cloudy pixel
    assert value == pytest.approx((0.3 * 4 + 0.6 * 3) / 7)
    assert len(granules) == 2
    assert merged is not None


def test_composite_mean_returns_none_when_all_pixels_are_invalid(tmp_path):
    ndvi_path = tmp_path / "empty_ndvi.tif"
    qa_path = tmp_path / "empty_qa.tif"
    _write_raster(ndvi_path, np.full((2, 2), -32768, dtype=np.int16), "int16", -32768)
    _write_raster(qa_path, np.full((2, 2), 255, dtype=np.uint8), "uint8", 255)

    value, valid, total, _, merged = ndvi._composite_mean([(ndvi_path, qa_path)], 2, 2)

    assert value is None
    assert valid == 0
    assert total == 4
    assert merged is None


def test_atomic_output_replaces_target(tmp_path):
    target = tmp_path / "nested" / "result.tif"

    with cache.atomic_output(target) as temporary:
        temporary.write_bytes(b"payload")
        assert not target.exists()

    assert target.read_bytes() == b"payload"
    assert list(target.parent.glob("*.part.tif")) == []


def test_atomic_output_discards_partial_file_on_error(tmp_path):
    target = tmp_path / "result.tif"

    with pytest.raises(RuntimeError):
        with cache.atomic_output(target) as temporary:
            temporary.write_bytes(b"partial")
            raise RuntimeError("gdal failed")

    assert not target.exists()
    assert list(tmp_path.glob("*.part.tif")) == []


def test_trim_cache_evicts_oldest_files_first(cache_dir, monkeypatch):
    monkeypatch.setattr(cache, "MIN_TRIM_AGE_SECONDS", 0)
    root = cache_dir / "mod13a1"
    root.mkdir(parents=True)

    oldest = root / "old.bin"
    middle = root / "middle.bin"
    newest = root / "new.bin"
    for path in (oldest, middle, newest):
        path.write_bytes(b"x" * 100)

    # Make the ordering explicit rather than relying on write timing.
    import os

    os.utime(oldest, (1_000_000, 1_000_000))
    os.utime(middle, (2_000_000, 2_000_000))
    os.utime(newest, (3_000_000, 3_000_000))

    reclaimed = cache.trim_cache(root, budget_bytes=150)

    assert reclaimed == 200
    assert cache.directory_size(root) == 100
    assert newest.exists()


def test_trim_cache_leaves_fresh_files_alone(cache_dir, monkeypatch):
    monkeypatch.setattr(cache, "MIN_TRIM_AGE_SECONDS", 600)
    root = cache_dir / "mod13a1"
    root.mkdir(parents=True)
    fresh = root / "fresh.bin"
    fresh.write_bytes(b"x" * 500)

    assert cache.trim_cache(root, budget_bytes=10) == 0
    assert fresh.exists()


class _FakeGranule:
    def __init__(self, filename: str) -> None:
        self.filename = filename

    def data_links(self) -> list[str]:
        return [f"https://example.test/data/{self.filename}?token=abc"]


def test_download_granules_reports_already_cached(tmp_path, monkeypatch, capsys):
    first = _FakeGranule("MOD13A1.A2024001.h20v05.061.hdf")
    second = _FakeGranule("MOD13A1.A2024001.h21v05.061.hdf")
    (tmp_path / first.filename).write_bytes(b"cached")

    calls = []

    def fake_download(granules, local_path=None, threads=8, show_progress=False):
        calls.append([granule.filename for granule in granules])
        for granule in granules:
            (Path(local_path) / granule.filename).write_bytes(b"x" * 2048)
        return [Path(local_path) / granule.filename for granule in granules]

    monkeypatch.setattr(modis_processor.earthaccess, "download", fake_download)

    paths, reused, downloaded_bytes = modis_processor.download_granules(
        [first, second], tmp_path, threads=4, label="for 2024-01-01", heartbeat_seconds=60
    )

    assert reused == 1
    assert downloaded_bytes == 2048
    assert calls == [[second.filename]]
    assert all(path.is_file() for path in paths)

    logs = capsys.readouterr().out
    assert "already cached 1 granule(s) for 2024-01-01" in logs
    assert "downloading 1 granule(s) for 2024-01-01" in logs
    assert "downloaded 1 granule(s) in" in logs


def test_download_granules_falls_back_to_single_file_retries(tmp_path, monkeypatch):
    granules = [_FakeGranule("a.hdf"), _FakeGranule("b.hdf")]
    attempts = []

    def fake_download(batch, local_path=None, threads=8, show_progress=False):
        attempts.append([granule.filename for granule in batch])
        if len(batch) > 1:
            raise OSError("batch link dropped")
        for granule in batch:
            (Path(local_path) / granule.filename).write_bytes(b"x")
        return [Path(local_path) / granule.filename for granule in batch]

    monkeypatch.setattr(modis_processor.earthaccess, "download", fake_download)

    paths, reused, _ = modis_processor.download_granules(granules, tmp_path, heartbeat_seconds=60)

    assert reused == 0
    assert attempts == [["a.hdf", "b.hdf"], ["a.hdf"], ["b.hdf"]]
    assert all(path.is_file() for path in paths)


def test_download_granules_raises_when_every_download_fails(tmp_path, monkeypatch):
    granules = [_FakeGranule("a.hdf"), _FakeGranule("b.hdf")]

    def failing_download(batch, local_path=None, threads=8, show_progress=False):
        raise OSError("401 Unauthorized")

    monkeypatch.setattr(modis_processor.earthaccess, "download", failing_download)

    with pytest.raises(RuntimeError, match="Could not download any of 2 MODIS granule"):
        modis_processor.download_granules(granules, tmp_path, heartbeat_seconds=60)


def test_download_granules_reports_progress_to_callback(tmp_path, monkeypatch):
    granule = _FakeGranule("a.hdf")
    events: list[tuple[float, int]] = []

    def fake_download(batch, local_path=None, threads=8, show_progress=False):
        (Path(local_path) / batch[0].filename).write_bytes(b"x" * 1024)
        return [Path(local_path) / batch[0].filename]

    monkeypatch.setattr(modis_processor.earthaccess, "download", fake_download)

    modis_processor.download_granules(
        [granule],
        tmp_path,
        heartbeat_seconds=60,
        on_progress=lambda elapsed, written: events.append((elapsed, written)),
    )

    assert events[0] == (0.0, 0)
    assert events[-1][1] == 1024


def test_download_progress_callback_failure_does_not_break_download(tmp_path, monkeypatch):
    granule = _FakeGranule("a.hdf")

    def fake_download(batch, local_path=None, threads=8, show_progress=False):
        (Path(local_path) / batch[0].filename).write_bytes(b"x")
        return [Path(local_path) / batch[0].filename]

    def broken_progress(elapsed, written):
        raise OSError("status disk full")

    monkeypatch.setattr(modis_processor.earthaccess, "download", fake_download)

    paths, _, _ = modis_processor.download_granules(
        [granule], tmp_path, heartbeat_seconds=60, on_progress=broken_progress
    )

    assert paths[0].is_file()


def test_collection_request_reports_blended_percent():
    reported = []

    def progress(stage, label, current, total, percent=None):
        reported.append((stage, label, current, total, percent))

    request = CollectionRequest(
        aoi={"west": 30.0, "south": 29.0, "east": 31.0, "north": 30.0},
        start_date="2024-01-01",
        end_date="2024-01-31",
        output_dir=Path("."),
        progress=progress,
        progress_label="ndvi",
        chunk_index=2,
        chunk_total=4,
    )

    request.report(1, 2, detail="composite 2024-01-17 (1/2)")

    assert reported == [("collecting", "ndvi composite 2024-01-17 (1/2)", 1, 2, 37.5)]


def test_collection_request_report_is_optional():
    request = CollectionRequest(
        aoi={"west": 30.0, "south": 29.0, "east": 31.0, "north": 30.0},
        start_date="2024-01-01",
        end_date="2024-01-31",
        output_dir=Path("."),
    )

    request.report(1, 1)  # must not raise when no callback is attached
