from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint_is_available():
    client = TestClient(app)
    response = client.get("/api/v1/health/")
    assert response.status_code == 200
    assert response.json()["status"] == "success"


def test_analysis_trend_validates_payload_and_returns_results(monkeypatch):
    client = TestClient(app)

    def fake_collect(config):
        return {
            "region_name": "trend_api",
            "aoi": config.aoi,
            "start_date": config.start_date,
            "end_date": config.end_date,
            "product_short_name": config.product_short_name,
            "temporal_aggregation": config.temporal_aggregation,
            "records": [
                {"observation_date": "2024-01-01", "mean_temperature_celsius": 10.0, "minimum_temperature_celsius": 8.0, "maximum_temperature_celsius": 12.0, "valid_pixels": 100, "total_pixels": 100, "valid_data_percentage": 100.0, "missing_observation": False, "missing_reason": None, "granules_used": ["g1"], "coverage_status": "complete"},
                {"observation_date": "2024-02-01", "mean_temperature_celsius": 11.0, "minimum_temperature_celsius": 9.0, "maximum_temperature_celsius": 13.0, "valid_pixels": 100, "total_pixels": 100, "valid_data_percentage": 100.0, "missing_observation": False, "missing_reason": None, "granules_used": ["g2"], "coverage_status": "complete"},
                {"observation_date": "2024-03-01", "mean_temperature_celsius": 12.0, "minimum_temperature_celsius": 10.0, "maximum_temperature_celsius": 14.0, "valid_pixels": 100, "total_pixels": 100, "valid_data_percentage": 100.0, "missing_observation": False, "missing_reason": None, "granules_used": ["g3"], "coverage_status": "complete"},
                {"observation_date": "2024-04-01", "mean_temperature_celsius": 13.0, "minimum_temperature_celsius": 11.0, "maximum_temperature_celsius": 15.0, "valid_pixels": 100, "total_pixels": 100, "valid_data_percentage": 100.0, "missing_observation": False, "missing_reason": None, "granules_used": ["g4"], "coverage_status": "complete"},
            ],
            "output_files": {"csv": "/tmp/example.csv", "json": "/tmp/example.json"},
            "cache_path": "/tmp/cache.json",
        }

    monkeypatch.setattr("app.api.routes.analysis.collect_mod11a1_timeseries", fake_collect)

    payload = {
        "product": "MOD11A1",
        "aoi": {"west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7},
        "start_date": "2024-01-01",
        "end_date": "2024-04-01",
        "temporal_resolution": "monthly",
        "analysis_methods": ["linear_regression", "mann_kendall"],
    }

    response = client.post("/api/v1/analysis/trend", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["metadata"]["product"] == "MOD11A1"
    assert "linear_regression" in body["analysis_results"]
    assert "mann_kendall" in body["analysis_results"]


def test_analysis_trend_reports_missing_gdal_as_service_unavailable(monkeypatch):
    client = TestClient(app)

    def fake_collect(config):
        raise RuntimeError(
            "gdal_translate was not found. MOD11A1 HDF4 subdatasets need GDAL built with the HDF4 driver."
        )

    monkeypatch.setattr("app.api.routes.analysis.collect_mod11a1_timeseries", fake_collect)

    payload = {
        "product": "MOD11A1",
        "aoi": {"west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7},
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
        "temporal_resolution": "daily",
        "analysis_methods": ["linear_regression"],
    }

    response = client.post("/api/v1/analysis/trend", json=payload)

    assert response.status_code == 503
    assert "gdal_translate" in response.json()["detail"]


def test_analysis_trend_surfaces_missing_reasons_for_empty_series(monkeypatch):
    client = TestClient(app)

    def fake_collect(config):
        return {
            "region_name": "trend_api",
            "aoi": config.aoi,
            "start_date": config.start_date,
            "end_date": config.end_date,
            "product_short_name": config.product_short_name,
            "temporal_aggregation": config.temporal_aggregation,
            "records": [
                {
                    "observation_date": "2024-01-01",
                    "mean_temperature_celsius": None,
                    "minimum_temperature_celsius": None,
                    "maximum_temperature_celsius": None,
                    "valid_pixels": 0,
                    "total_pixels": 1000,
                    "valid_data_percentage": 0.0,
                    "missing_observation": True,
                    "missing_reason": "No granules found for the AOI on this date.",
                    "granules_used": [],
                    "coverage_status": "missing",
                }
            ],
            "output_files": {},
            "cache_path": "/tmp/cache.json",
        }

    monkeypatch.setattr("app.api.routes.analysis.collect_mod11a1_timeseries", fake_collect)

    payload = {
        "product": "MOD11A1",
        "aoi": {"west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7},
        "start_date": "2024-01-01",
        "end_date": "2024-01-01",
        "temporal_resolution": "daily",
        "analysis_methods": ["linear_regression"],
    }

    response = client.post("/api/v1/analysis/trend", json=payload)

    assert response.status_code == 404
    assert "No valid observations" in response.json()["detail"]
    assert "No granules found" in response.json()["detail"]


def test_analysis_trend_rejects_invalid_aoi_and_dates():
    client = TestClient(app)
    payload = {
        "product": "MOD11A1",
        "aoi": {"west": 10.0, "south": 5.0, "east": 5.0, "north": 10.0},
        "start_date": "2024-06-01",
        "end_date": "2024-05-01",
        "temporal_resolution": "monthly",
        "analysis_methods": ["linear_regression"],
    }

    response = client.post("/api/v1/analysis/trend", json=payload)
    assert response.status_code == 400
    assert "AOI" in response.json()["detail"] or "start_date" in response.json()["detail"]
