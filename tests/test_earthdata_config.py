from __future__ import annotations

import os

import pytest

from app.core.config import get_settings, load_environment
from app.services import earthdata_auth

CREDENTIAL_VARS = (
    "EARTHDATA_USERNAME",
    "EARTHDATA_PASSWORD",
    "EARTHDATA_TOKEN",
    "EARTHDATA_AUTH_STRATEGY",
)


@pytest.fixture(autouse=True)
def _clear_earthdata_env(monkeypatch):
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)


def test_load_environment_reads_file_without_overriding_shell_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "EARTHDATA_TEST_FROM_FILE=file_value\nEARTHDATA_TEST_OTHER=other_value\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EARTHDATA_TEST_FROM_FILE", "shell_value")
    monkeypatch.delenv("EARTHDATA_TEST_OTHER", raising=False)

    load_environment(env_file)

    assert os.environ["EARTHDATA_TEST_FROM_FILE"] == "shell_value"
    assert os.environ["EARTHDATA_TEST_OTHER"] == "other_value"


def test_settings_read_username_and_password(monkeypatch):
    monkeypatch.setenv("EARTHDATA_USERNAME", "alice")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "secret")

    settings = get_settings()

    assert settings.has_username_password is True
    assert settings.earthdata_auth_strategy == "environment"


def test_settings_prefer_token_and_environment_strategy(monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "token-value")

    settings = get_settings()

    assert settings.earthdata_token == "token-value"
    assert settings.earthdata_auth_strategy == "environment"


def test_settings_without_credentials_default_to_netrc():
    assert get_settings().earthdata_auth_strategy == "netrc"


def test_settings_reject_partial_credentials(monkeypatch):
    monkeypatch.setenv("EARTHDATA_USERNAME", "alice")

    with pytest.raises(ValueError, match="EARTHDATA_PASSWORD"):
        get_settings()


def test_settings_reject_unknown_strategy(monkeypatch):
    monkeypatch.setenv("EARTHDATA_AUTH_STRATEGY", "carrier-pigeon")

    with pytest.raises(ValueError, match="EARTHDATA_AUTH_STRATEGY"):
        get_settings()


def test_ensure_auth_uses_environment_strategy(monkeypatch):
    calls: dict[str, object] = {}

    class FakeAuth:
        authenticated = True

    def fake_login(strategy, persist=False):
        calls["strategy"] = strategy
        calls["persist"] = persist
        return FakeAuth()

    monkeypatch.setattr(earthdata_auth.earthaccess, "login", fake_login)
    monkeypatch.setenv("EARTHDATA_USERNAME", "alice")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "secret")

    auth = earthdata_auth.ensure_earthaccess_auth()

    assert auth.authenticated is True
    assert calls["strategy"] == "environment"


def test_ensure_auth_uses_netrc_without_credentials(monkeypatch):
    calls: dict[str, object] = {}

    class FakeAuth:
        authenticated = True

    def fake_login(strategy, persist=False):
        calls["strategy"] = strategy
        return FakeAuth()

    monkeypatch.setattr(earthdata_auth.earthaccess, "login", fake_login)

    earthdata_auth.ensure_earthaccess_auth()

    assert calls["strategy"] == "netrc"


def test_ensure_auth_reports_missing_environment_credentials(monkeypatch):
    monkeypatch.setenv("EARTHDATA_AUTH_STRATEGY", "environment")

    with pytest.raises(ConnectionError, match="credentials are missing"):
        earthdata_auth.ensure_earthaccess_auth()


def test_ensure_auth_wraps_login_failures(monkeypatch):
    def failing_login(strategy, persist=False):
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(earthdata_auth.earthaccess, "login", failing_login)
    monkeypatch.setenv("EARTHDATA_USERNAME", "alice")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "wrong")

    with pytest.raises(ConnectionError, match="authentication failed"):
        earthdata_auth.ensure_earthaccess_auth()


def test_ensure_auth_rejects_unauthenticated_result(monkeypatch):
    class FakeAuth:
        authenticated = False

    monkeypatch.setattr(earthdata_auth.earthaccess, "login", lambda **kwargs: FakeAuth())
    monkeypatch.setenv("EARTHDATA_USERNAME", "alice")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "secret")

    with pytest.raises(ConnectionError, match="authentication failed"):
        earthdata_auth.ensure_earthaccess_auth()
