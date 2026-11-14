"""Unit tests for the NASA AppEEARS REST API client and pipeline integration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from app.services.appeears_client import AppEEARSClient
from app.services.earthdata_auth import ensure_appeears_auth
from app.services.modis_downloader import (
    discover_mod11a1_granules,
    download_mod11a1_appeears,
    download_mod11a1_granules,
    get_tile_name,
)
from app.services.modis_processor import process_appeears_raster_pair
from app.services.modis_timeseries import parse_appeears_filename_date


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EARTHDATA_USERNAME", "test_user")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "test_pass")
    return AppEEARSClient()


def test_appeears_login_success(client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "token_type": "Bearer",
        "token": "fake-bearer-token-12345",
        "expiration": "2030-01-01T00:00:00Z",
    }

    with patch.object(client.session, "post", return_value=mock_resp) as mock_post:
        token = client.login()
        assert token == "fake-bearer-token-12345"
        assert client._token == "fake-bearer-token-12345"
        mock_post.assert_called_once()
        # Call again without force_refresh; should return cached token without calling post
        cached_token = client.login()
        assert cached_token == "fake-bearer-token-12345"
        assert mock_post.call_count == 1


def test_appeears_login_missing_credentials(monkeypatch):
    monkeypatch.delenv("EARTHDATA_USERNAME", raising=False)
    monkeypatch.delenv("EARTHDATA_PASSWORD", raising=False)
    c = AppEEARSClient()
    with pytest.raises(ConnectionError) as exc_info:
        c.login()
    assert "EARTHDATA_USERNAME and EARTHDATA_PASSWORD" in str(exc_info.value)


def test_appeears_login_failure_status(client):
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Invalid credentials"

    with patch.object(client.session, "post", return_value=mock_resp):
        with pytest.raises(ConnectionError) as exc_info:
            client.login()
        assert "401" in str(exc_info.value)


def test_appeears_submit_area_task(client):
    client._token = "valid_token"
    client._token_expires = 9999999999.0

    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.json.return_value = {"task_id": "test-uuid-789", "status": "pending"}

    bbox = {"west": 25.0, "south": 22.0, "east": 35.0, "north": 31.0}

    with patch.object(client.session, "request", return_value=mock_resp) as mock_req:
        task_id = client.submit_area_task(
            task_name="test_task",
            start_date="2024-01-01",
            end_date="2024-01-02",
            layers=[("MOD11A1.061", "LST_Day_1km")],
            bbox=bbox,
        )

        assert task_id == "test-uuid-789"
        mock_req.assert_called_once()
        _, kwargs = mock_req.call_args
        payload = kwargs["json"]

        assert payload["task_type"] == "area"
        # Notice AppEEARS date formatting is MM-DD-YYYY
        assert payload["params"]["dates"][0]["startDate"] == "01-01-2024"
        assert payload["params"]["dates"][0]["endDate"] == "01-02-2024"
        assert payload["params"]["layers"][0]["product"] == "MOD11A1.061"
        assert payload["params"]["output"]["format"]["type"] == "geotiff"
        assert payload["params"]["output"]["projection"] == "geographic"


def test_appeears_wait_for_task_done(client):
    client._token = "valid_token"
    client._token_expires = 9999999999.0

    status_resp = MagicMock()
    status_resp.status_code = 200
    status_resp.json.return_value = {
        "status": "done",
        "task_id": "test-uuid-789",
        "progress": {"summary": 100},
    }

    with patch.object(client.session, "request", return_value=status_resp):
        res = client.wait_for_task("test-uuid-789", poll_interval_seconds=0)
        assert res["status"] == "done"


def test_appeears_wait_for_task_cancellation(client):
    client._token = "valid_token"
    client._token_expires = 9999999999.0

    with patch.object(client, "delete_task") as mock_delete:
        with pytest.raises(RuntimeError) as exc_info:
            client.wait_for_task("test-uuid-789", should_cancel=lambda: True)
        assert "canceled" in str(exc_info.value)
        mock_delete.assert_called_once_with("test-uuid-789")


def test_appeears_download_bundle(client, tmp_path):
    client._token = "valid_token"
    client._token_expires = 9999999999.0

    mock_bundle_resp = MagicMock()
    mock_bundle_resp.status_code = 200
    mock_bundle_resp.json.return_value = {
        "files": [
            {
                "file_id": "f1",
                "file_name": "MOD11A1.061_2024001_to_2024031/MOD11A1.061_LST_Day_1km_20240101T000000_aid0001.tif",
                "file_size": 14,
                "file_type": "geotiff",
            },
            {
                "file_id": "f2",
                "file_name": "README.txt",
                "file_size": 5,
                "file_type": "txt",
            },
        ]
    }

    mock_download_resp = MagicMock()
    mock_download_resp.status_code = 200
    mock_download_resp.__enter__.return_value = mock_download_resp
    mock_download_resp.iter_content.return_value = [b"mock_tif_bytes"]

    with patch.object(client.session, "request", return_value=mock_bundle_resp):
        with patch.object(client.session, "get", return_value=mock_download_resp):
            paths = client.download_bundle(
                task_id="test-uuid-789",
                target_dir=tmp_path,
                file_patterns=("LST_Day_1km",),
            )

            assert len(paths) == 1
            assert paths[0].name == "MOD11A1.061_LST_Day_1km_20240101T000000_aid0001.tif"
            assert paths[0].is_file()
            assert paths[0].read_bytes() == b"mock_tif_bytes"


def test_parse_appeears_filename_date():
    assert parse_appeears_filename_date("MOD11A1_061_LST_Day_1km_doy2024001_aid0001.tif") == "2024-01-01"
    assert parse_appeears_filename_date("MOD11A1_061_LST_Day_1km_doy2024032_aid0001.tif") == "2024-02-01"
    assert parse_appeears_filename_date("MOD11A1_061_LST_Day_1km_20240101T120000.tif") == "2024-01-01"
    assert parse_appeears_filename_date("MOD11A1.061_2024001_to_2024031/MOD11A1.061_LST_Day_1km_20240101T000000_aid0001.tif") == "2024-01-01"
    assert parse_appeears_filename_date("MOD11A1_061_LST_Day_1km_2024-03-15.tif") == "2024-03-15"
    assert parse_appeears_filename_date("unknown_file.txt") is None


def test_process_appeears_raster_pair(tmp_path):
    # Create mock LST GeoTIFF and QC GeoTIFF
    lst_path = tmp_path / "mock_lst.tif"
    qc_path = tmp_path / "mock_qc.tif"

    # Raw value 15000 -> Kelvin = 15000 * 0.02 = 300 K -> 300 - 273.15 = 26.85 C
    lst_array = np.array([[15000, 15000], [15000, 5000]], dtype=np.uint16)
    # QC: 0b00 = good quality (bits 0-1 = 0), 0b01 = bad quality
    qc_array = np.array([[0, 0], [1, 0]], dtype=np.uint8)

    transform = from_bounds(24.0, 22.0, 36.0, 31.0, width=2, height=2)
    profile = {
        "driver": "GTiff",
        "height": 2,
        "width": 2,
        "count": 1,
        "crs": "EPSG:4326",
        "transform": transform,
    }

    with rasterio.open(lst_path, "w", dtype="uint16", **profile) as dst:
        dst.write(lst_array, 1)

    with rasterio.open(qc_path, "w", dtype="uint8", **profile) as dst:
        dst.write(qc_array, 1)

    celsius, warnings, summary = process_appeears_raster_pair(lst_path, qc_path)

    # Pixel (0,0) and (0,1) are valid LST (15000 >= 7500) and good QC (0)
    # Pixel (1,0) is filtered out by QC (qc=1)
    # Pixel (1,1) is filtered out by LST range (5000 < 7500)
    assert summary["valid_pixels"] == 2
    assert summary["total_pixels"] == 4
    assert pytest.approx(summary["mean_temperature_celsius"], 0.01) == 26.85
    assert np.isnan(celsius[1, 0])
    assert np.isnan(celsius[1, 1])
    assert pytest.approx(celsius[0, 0], 0.01) == 26.85


def test_modis_downloader_get_tile_name():
    granule = {
        "umm": {"GranuleUR": "MOD11A1.061_LST_Day_1km_2024-01-01"},
        "layer": "LST_Day_1km",
    }
    assert get_tile_name(granule) == "MOD11A1.061_LST_Day_1km_2024-01-01"
    assert get_tile_name({"layer": "LST_Day_1km"}) == "MOD11A1.061_LST_Day_1km"
    assert get_tile_name("other") == "appeears_mod11a1_ard"


def test_ensure_appeears_auth(monkeypatch):
    monkeypatch.setenv("EARTHDATA_USERNAME", "u")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "p")

    client = AppEEARSClient()
    with patch.object(client, "login", return_value="test-token"):
        tok = ensure_appeears_auth(client)
        assert tok == "test-token"


def test_appeears_wait_for_task_extracts_step_progress(client):
    client._token = "valid_token"
    client._token_expires = 9999999999.0

    in_progress_resp = MagicMock()
    in_progress_resp.status_code = 200
    in_progress_resp.json.return_value = [
        {
            "task_id": "test-uuid-step",
            "status_type": "task",
            "progress": {
                "details": [
                    {"step": 1, "desc": "Initializing", "pct_complete": 100},
                    {"step": 2, "desc": "Downloading", "pct_complete": 45},
                    {"step": 3, "desc": "Subsetting", "pct_complete": 0},
                ],
                "summary": 45,
            },
        }
    ]

    done_resp = MagicMock()
    done_resp.status_code = 200
    done_resp.json.return_value = {
        "status": "done",
        "task_id": "test-uuid-step",
        "progress": {"summary": 100},
    }

    reported_messages = []

    def on_prog(pct, msg):
        reported_messages.append((pct, msg))

    with patch.object(client.session, "request", side_effect=[in_progress_resp, done_resp]):
        res = client.wait_for_task("test-uuid-step", poll_interval_seconds=0, on_progress=on_prog)
        assert res["status"] == "done"
        assert len(reported_messages) == 2
        assert "Downloading (45%)" in reported_messages[0][1]

