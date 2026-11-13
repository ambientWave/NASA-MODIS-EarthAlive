from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.routes import comparison
from app.main import app


def _payload(**overrides):
    payload = {
        "aoi": {"west": 31.0, "south": 29.0, "east": 32.0, "north": 30.0},
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
        "temporal_resolution": "daily",
        "variables": ["land_surface_temperature", "ndvi"],
        "analysis_methods": ["linear_regression", "mann_kendall"],
    }
    payload.update(overrides)
    return payload


def test_compare_variables_returns_accepted_job(monkeypatch):
    seen = {}

    def fake_create_job(kind, payload):
        seen["kind"] = kind
        seen["payload"] = payload
        return "job123"

    monkeypatch.setattr(comparison, "create_job", fake_create_job)
    response = TestClient(app).post("/api/v1/analysis/compare/variables", json=_payload())

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job_id"] == "job123"
    assert body["status"] == "queued"
    assert seen["kind"] == "variable_comparison"
    assert seen["payload"]["variables"] == ["land_surface_temperature", "ndvi"]


def test_compare_variables_rejects_unsupported_variable(monkeypatch):
    monkeypatch.setattr(comparison, "create_job", lambda kind, payload: "unused")
    response = TestClient(app).post(
        "/api/v1/analysis/compare/variables",
        json=_payload(variables=["wind_speed"]),
    )
    assert response.status_code == 400
    assert "Unsupported variables" in response.json()["detail"]


def test_compare_variables_rejects_pre_coverage_start_date(monkeypatch):
    monkeypatch.setattr(comparison, "create_job", lambda kind, payload: "unused")
    response = TestClient(app).post(
        "/api/v1/analysis/compare/variables",
        json=_payload(start_date="1999-01-01"),
    )
    assert response.status_code == 400
    assert "MOD11A1" in response.json()["detail"]


def test_compare_regions_rejects_duplicate_aois(monkeypatch):
    monkeypatch.setattr(comparison, "create_job", lambda kind, payload: "unused")
    region = {"west": 31.0, "south": 29.0, "east": 31.5, "north": 29.5}
    response = TestClient(app).post(
        "/api/v1/analysis/compare/regions",
        json={
            "variable": "land_surface_temperature",
            "regions": [region, dict(region)],
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
            "analysis_methods": ["linear_regression"],
        },
    )
    assert response.status_code == 400
    assert "Duplicate" in response.json()["detail"]


def test_job_status_and_result_routes(monkeypatch):
    monkeypatch.setattr(
        comparison,
        "get_job_status",
        lambda job_id: {
            "job_id": job_id,
            "status": "finished",
            "result_url": f"/api/v1/analysis/jobs/{job_id}/result",
        },
    )
    monkeypatch.setattr(
        comparison,
        "get_job_result",
        lambda job_id: {"success": True, "job_id": job_id, "visualizations": {}},
    )
    client = TestClient(app)

    status_response = client.get("/api/v1/analysis/jobs/job123")
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "finished"

    result_response = client.get("/api/v1/analysis/jobs/job123/result")
    assert result_response.status_code == 200
    assert result_response.json()["success"] is True


def test_result_route_reports_running_job(monkeypatch):
    monkeypatch.setattr(comparison, "get_job_status", lambda job_id: {"job_id": job_id, "status": "started"})
    monkeypatch.setattr(comparison, "get_job_result", lambda job_id: None)
    response = TestClient(app).get("/api/v1/analysis/jobs/job123/result")
    assert response.status_code == 409
