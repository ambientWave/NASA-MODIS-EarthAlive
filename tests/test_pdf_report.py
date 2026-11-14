"""Tests for PDF report generation and the /jobs/{job_id}/pdf endpoint."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.services.pdf_report_service import build_analysis_pdf


def _dummy_png_base64(color=(70, 130, 180), size=(200, 100)) -> str:
    """Create a small dummy base64 PNG image."""
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _sample_trend_result(job_id: str = "trend_12345") -> dict[str, Any]:
    return {
        "success": True,
        "job_id": job_id,
        "metadata": {
            "mode": "trend_analysis",
            "variable": "land_surface_temperature",
            "display_name": "Land Surface Temperature",
            "dataset": "MOD11A1.061",
            "unit": "Celsius",
            "native_resolution": "1km",
            "temporal_resolution": "monthly",
            "spatial_strategy": "coarse",
            "collection_strategy": "coarse_period",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "aoi": {"west": 29.0, "south": 25.0, "east": 31.0, "north": 27.0},
            "analysis_methods": ["linear_regression", "mann_kendall", "t_test"],
        },
        "data_quality": {
            "observation_count": 12,
            "valid_observation_count": 12,
            "missing_observation_count": 0,
            "missing_observation_percentage": 0.0,
            "data_coverage_percentage": 100.0,
            "warnings": [],
        },
        "analysis_results": {
            "linear_regression": {
                "status": "calculated",
                "slope": 0.045,
                "intercept": 22.1,
                "r_squared": 0.88,
                "p_value": 0.0012,
                "significant": True,
            },
            "mann_kendall": {
                "status": "calculated",
                "trend": "increasing",
                "p_value": 0.003,
                "tau": 0.72,
                "z": 2.85,
            },
            "t_test": {
                "status": "calculated",
                "t_statistic": 3.42,
                "p_value": 0.005,
            },
        },
        "visualizations": {
            "trend_line": {"image_base64": _dummy_png_base64((50, 100, 200)), "content_type": "image/png"},
            "mean_raster": {"image_base64": _dummy_png_base64((200, 80, 80)), "content_type": "image/png"},
        },
        "warnings": ["Test observation notice."],
    }


def _sample_comparison_result(job_id: str = "comp_67890") -> dict[str, Any]:
    return {
        "success": True,
        "job_id": job_id,
        "metadata": {
            "mode": "variable_comparison",
            "start_date": "2024-01-01",
            "end_date": "2024-06-30",
            "temporal_resolution": "monthly",
            "spatial_strategy": "coarse",
            "aoi": {"west": 29.0, "south": 25.0, "east": 31.0, "north": 27.0},
            "variables": ["land_surface_temperature", "ndvi"],
            "analysis_methods": ["linear_regression", "mann_kendall"],
        },
        "variables": {
            "land_surface_temperature": {
                "metadata": {
                    "variable_id": "land_surface_temperature",
                    "display_name": "Land Surface Temperature",
                    "dataset": "MOD11A1.061",
                    "unit": "Celsius",
                },
                "data_quality": {
                    "observation_count": 6,
                    "valid_observation_count": 6,
                    "data_coverage_percentage": 100.0,
                },
                "analysis_results": {
                    "linear_regression": {"slope": 0.12, "p_value": 0.004, "r_squared": 0.81},
                    "mann_kendall": {"trend": "increasing", "p_value": 0.01},
                },
                "visualizations": {
                    "trend_line": {"image_base64": _dummy_png_base64((100, 150, 250))},
                    "mean_raster": {"image_base64": _dummy_png_base64((250, 120, 100))},
                },
                "status": "calculated",
            },
            "ndvi": {
                "metadata": {
                    "variable_id": "ndvi",
                    "display_name": "Normalized Difference Vegetation Index",
                    "dataset": "MOD13Q1.061",
                    "unit": "NDVI",
                },
                "data_quality": {
                    "observation_count": 6,
                    "valid_observation_count": 5,
                    "data_coverage_percentage": 83.3,
                },
                "analysis_results": {
                    "linear_regression": {"slope": -0.015, "p_value": 0.03, "r_squared": 0.65},
                    "mann_kendall": {"trend": "decreasing", "p_value": 0.04},
                },
                "visualizations": {
                    "trend_line": {"image_base64": _dummy_png_base64((50, 180, 100))},
                    "mean_raster": {"image_base64": _dummy_png_base64((80, 220, 120))},
                },
                "status": "calculated",
            },
        },
        "comparison": {
            "visualizations": {
                "multi_variable_series": {"image_base64": _dummy_png_base64((180, 80, 200))},
                "correlation_heatmap": {"image_base64": _dummy_png_base64((220, 180, 50))},
            }
        },
        "warnings": [],
    }


def test_build_trend_pdf():
    result = _sample_trend_result()
    pdf_bytes = build_analysis_pdf(result)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 1000
    assert pdf_bytes.startswith(b"%PDF")


def test_build_comparison_pdf():
    result = _sample_comparison_result()
    pdf_bytes = build_analysis_pdf(result)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 1000
    assert pdf_bytes.startswith(b"%PDF")


def test_job_pdf_endpoint_binary(monkeypatch):
    sample = _sample_trend_result("job_test_pdf_1")

    monkeypatch.setattr("app.api.routes.jobs.get_job_status", lambda jid: {"job_id": jid, "status": "finished"})
    monkeypatch.setattr("app.api.routes.jobs.get_job_result", lambda jid: sample)

    client = TestClient(app)
    response = client.get("/api/v1/analysis/jobs/job_test_pdf_1/pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "inline" in response.headers.get("content-disposition", "")
    assert response.content.startswith(b"%PDF")


def test_job_pdf_endpoint_base64(monkeypatch):
    sample = _sample_comparison_result("job_test_pdf_2")

    monkeypatch.setattr("app.api.routes.jobs.get_job_status", lambda jid: {"job_id": jid, "status": "finished"})
    monkeypatch.setattr("app.api.routes.jobs.get_job_result", lambda jid: sample)

    client = TestClient(app)
    response = client.get("/api/v1/analysis/jobs/job_test_pdf_2/pdf?format=base64")
    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == "job_test_pdf_2"
    assert data["content_type"] == "application/pdf"
    assert "pdf_base64" in data
    decoded = base64.b64decode(data["pdf_base64"])
    assert decoded.startswith(b"%PDF")

