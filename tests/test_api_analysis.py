from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.variables import get_variable_spec
from app.services.variables.base import VariableSeries


def _records(values: list[float]) -> list[dict]:
    return [
        {
            "observation_date": f"2024-{index + 1:02d}-01",
            "value": value,
            "valid_pixel_count": 100,
            "total_pixel_count": 100,
            "valid_data_percentage": 100.0,
            "missing": False,
            "missing_reason": None,
            "granules_used": [f"g{index}"],
            "coverage_status": "complete",
        }
        for index, value in enumerate(values)
    ]


def _monthly_records() -> list[dict]:
    return _records([10.0, 11.0, 12.0, 13.0])


def _fake_collect(records):
    def collect(
        variable_id,
        aoi,
        start_date,
        end_date,
        output_dir,
        *,
        max_days=None,
        progress=None,
        progress_label=None,
        should_cancel=None,
        collector=None,
    ):
        spec = get_variable_spec(variable_id)
        if progress is not None:
            progress("collecting", progress_label or variable_id, 1, 1)
        return VariableSeries(spec=spec, records=records)

    return collect


def _payload(**overrides):
    payload = {
        "aoi": {"west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7},
        "start_date": "2024-01-01",
        "end_date": "2024-04-01",
        "temporal_resolution": "monthly",
        "analysis_methods": ["linear_regression", "mann_kendall"],
    }
    payload.update(overrides)
    return payload


def _use_inline_jobs(monkeypatch, tmp_path, collect):
    monkeypatch.setattr("app.jobs.queue.QUEUE_MODE", "inline")
    monkeypatch.setattr("app.jobs.store.JOBS_DIR", tmp_path)
    monkeypatch.setattr("app.services.trend_service.JOBS_DIR", tmp_path)
    monkeypatch.setattr("app.services.trend_service.collect_chunked_variable", collect)


def test_health_endpoint_is_available():
    client = TestClient(app)
    response = client.get("/api/v1/health/")
    assert response.status_code == 200
    assert response.json()["status"] == "success"


def test_analysis_trend_is_queued_and_returns_results(monkeypatch, tmp_path):
    _use_inline_jobs(monkeypatch, tmp_path, _fake_collect(_monthly_records()))
    client = TestClient(app)

    response = client.post("/api/v1/analysis/trend", json=_payload())
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["status"] == "queued"
    assert accepted["status_url"] == f"/api/v1/analysis/jobs/{accepted['job_id']}"
    assert accepted["result_url"] == f"/api/v1/analysis/jobs/{accepted['job_id']}/result"

    status_response = client.get(accepted["status_url"])
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "finished"
    assert status_response.json()["kind"] == "trend_analysis"

    result_response = client.get(accepted["result_url"])
    assert result_response.status_code == 200, result_response.text
    body = result_response.json()
    assert body["success"] is True
    assert body["metadata"]["mode"] == "trend_analysis"
    assert body["metadata"]["product"] == "MOD11A1"
    assert "linear_regression" in body["analysis_results"]
    assert "mann_kendall" in body["analysis_results"]

    visualizations = body["visualizations"]
    assert set(visualizations) == {"linear_regression", "mann_kendall"}
    for chart in visualizations.values():
        assert chart["content_type"] == "image/png"
        assert "path" not in chart
        assert "filename" not in chart
        assert base64.b64decode(chart["base64"]).startswith(b"\x89PNG")


def test_analysis_trend_supports_ndvi_variable(monkeypatch, tmp_path):
    _use_inline_jobs(monkeypatch, tmp_path, _fake_collect(_records([0.2, 0.3, 0.4, 0.5])))
    client = TestClient(app)

    response = client.post("/api/v1/analysis/trend", json=_payload(variable="ndvi"))
    assert response.status_code == 202, response.text

    body = client.get(response.json()["result_url"]).json()
    assert body["metadata"]["variable"] == "ndvi"
    assert body["metadata"]["product"] == "MOD13Q1"
    assert body["metadata"]["unit"] == "NDVI"
    assert "linear_regression" in body["analysis_results"]


def test_analysis_trend_accepts_dataset_short_name_as_product(monkeypatch, tmp_path):
    _use_inline_jobs(monkeypatch, tmp_path, _fake_collect(_records([0.2, 0.3, 0.4, 0.5])))
    client = TestClient(app)

    response = client.post("/api/v1/analysis/trend", json=_payload(product="MOD13Q1"))
    assert response.status_code == 202, response.text

    body = client.get(response.json()["result_url"]).json()
    assert body["metadata"]["variable"] == "ndvi"
    assert body["metadata"]["product"] == "MOD13Q1"


def test_analysis_trend_rejects_conflicting_variable_and_product():
    response = TestClient(app).post(
        "/api/v1/analysis/trend",
        json=_payload(variable="ndvi", product="MOD11A1"),
    )
    assert response.status_code == 400
    assert "different variables" in response.json()["detail"]


def test_analysis_trend_reports_missing_gdal_as_service_unavailable(monkeypatch, tmp_path):
    def fake_collect(*args, **kwargs):
        raise RuntimeError(
            "gdal_translate was not found. MOD11A1 HDF4 subdatasets need GDAL built with the HDF4 driver."
        )

    _use_inline_jobs(monkeypatch, tmp_path, fake_collect)
    client = TestClient(app)

    response = client.post(
        "/api/v1/analysis/trend",
        json=_payload(end_date="2024-01-31", temporal_resolution="daily", analysis_methods=["linear_regression"]),
    )
    assert response.status_code == 202, response.text

    result_response = client.get(response.json()["result_url"])
    assert result_response.status_code == 503
    assert "gdal_translate" in result_response.json()["detail"]

    status_response = client.get(response.json()["status_url"])
    assert status_response.json()["status"] == "failed"
    assert status_response.json()["status_code"] == 503


def test_analysis_trend_surfaces_missing_reasons_for_empty_series(monkeypatch, tmp_path):
    records = [
        {
            "observation_date": "2024-01-01",
            "value": None,
            "valid_pixel_count": 0,
            "total_pixel_count": 1000,
            "valid_data_percentage": 0.0,
            "missing": True,
            "missing_reason": "No granules found for the AOI on this date.",
            "granules_used": [],
            "coverage_status": "missing",
        }
    ]
    _use_inline_jobs(monkeypatch, tmp_path, _fake_collect(records))
    client = TestClient(app)

    response = client.post(
        "/api/v1/analysis/trend",
        json=_payload(end_date="2024-01-01", temporal_resolution="daily", analysis_methods=["linear_regression"]),
    )
    assert response.status_code == 202, response.text

    result_response = client.get(response.json()["result_url"])
    assert result_response.status_code == 404
    assert "No valid observations" in result_response.json()["detail"]
    assert "No granules found" in result_response.json()["detail"]


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


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"start_date": "1999-01-01"}, "coverage start"),
        ({"start_date": "2000-03-01", "end_date": "2035-01-01"}, "maximum"),
    ],
)
def test_analysis_trend_rejects_unsupported_ranges(overrides, expected):
    response = TestClient(app).post("/api/v1/analysis/trend", json=_payload(**overrides))
    assert response.status_code == 400
    assert expected in response.json()["detail"]
