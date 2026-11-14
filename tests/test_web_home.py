"""The Flutter web build is served from the API process.

``/home`` returns ``flutterweb/web/index.html``; the rest of the build is
mounted at ``/`` because the entrypoint declares ``<base href="/">``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


def test_home_serves_the_flutter_entrypoint():
    response = TestClient(app).get("/home")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Earth Alive" in response.text
    assert "flutter_bootstrap.js" in response.text
    # A rebuilt entrypoint must not be cached.
    assert "no-cache" in response.headers["cache-control"]


def test_home_assets_resolve_from_the_server_root():
    client = TestClient(app)

    for asset in ("/main.dart.js", "/flutter_bootstrap.js", "/manifest.json", "/splash_logo.png"):
        response = client.get(asset)
        assert response.status_code == 200, f"{asset} -> {response.status_code}"


def test_static_mount_does_not_shadow_the_api():
    client = TestClient(app)

    assert client.get("/").json()["message"] == "Welcome to Earth Alive API"
    assert client.get("/api/v1/health/").status_code == 200


def test_home_reports_a_missing_build(monkeypatch, tmp_path):
    monkeypatch.setattr(main_module, "FLUTTER_INDEX", tmp_path / "index.html")

    response = TestClient(app).get("/home")

    assert response.status_code == 503
    assert "flutter build web" in response.json()["detail"]
