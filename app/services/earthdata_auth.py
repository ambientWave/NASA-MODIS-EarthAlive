from __future__ import annotations

from typing import Any

import earthaccess

from app.core.config import get_settings
from app.services.appeears_client import AppEEARSClient
from app.utils.logging import log


def ensure_earthaccess_auth(persist: bool = False) -> Any:
    """Authenticate with NASA Earthdata using credentials from the environment.

    Credentials are read from ``EARTHDATA_USERNAME`` / ``EARTHDATA_PASSWORD`` or
    ``EARTHDATA_TOKEN``, which may be supplied by the process environment or by
    the project ``.env`` file. Failures are raised as ``ConnectionError`` so the
    HTTP layer can map them to 503 Service Unavailable.
    """
    settings = get_settings()
    strategy = settings.earthdata_auth_strategy

    if strategy == "environment" and not (settings.has_username_password or settings.earthdata_token):
        raise ConnectionError(
            "NASA Earthdata credentials are missing. Set EARTHDATA_USERNAME and "
            "EARTHDATA_PASSWORD (or EARTHDATA_TOKEN) in .env or the environment."
        )

    try:
        log(f"earthdata: logging in with strategy '{strategy}'")
        auth = earthaccess.login(strategy=strategy, persist=persist)
    except Exception as exc:
        log(f"earthdata: login failed with strategy '{strategy}': {exc}")
        raise ConnectionError(
            f"NASA Earthdata authentication failed using the '{strategy}' strategy: {exc}"
        ) from exc

    if not getattr(auth, "authenticated", False):
        log(f"earthdata: login did not authenticate using strategy '{strategy}'")
        raise ConnectionError(
            f"NASA Earthdata authentication failed using the '{strategy}' strategy. "
            "Check your credentials in .env, or use EARTHDATA_TOKEN."
        )
    log(f"earthdata: authenticated using strategy '{strategy}'")
    return auth


def ensure_appeears_auth(client: AppEEARSClient | None = None) -> str:
    """Authenticate with NASA AppEEARS API using Earthdata credentials.

    Returns the Bearer token upon success or raises ConnectionError.
    """
    client = client or AppEEARSClient()
    try:
        token = client.login()
        return token
    except Exception as exc:
        log(f"appeears: authentication failed: {exc}")
        raise ConnectionError(f"NASA AppEEARS authentication failed: {exc}") from exc
