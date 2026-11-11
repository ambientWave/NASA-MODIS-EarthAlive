from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

VALID_AUTH_STRATEGIES = {"environment", "netrc", "all", "interactive"}


def load_environment(dotenv_path: str | Path = DEFAULT_ENV_FILE) -> None:
    """Load ``KEY=VALUE`` pairs from a ``.env`` file into ``os.environ``.

    Existing environment variables are never overwritten, so values supplied by
    the shell, CI, or a container take precedence over the ``.env`` file.
    """
    load_dotenv(dotenv_path=Path(dotenv_path), override=False, encoding="utf-8")


@dataclass(frozen=True)
class Settings:
    earthdata_username: str | None
    earthdata_password: str | None
    earthdata_token: str | None
    earthdata_auth_strategy: str

    @property
    def has_username_password(self) -> bool:
        return bool(self.earthdata_username and self.earthdata_password)

    def validate(self) -> None:
        if bool(self.earthdata_username) != bool(self.earthdata_password):
            raise ValueError(
                "EARTHDATA_USERNAME and EARTHDATA_PASSWORD must be set together. "
                "Set both in .env, or use EARTHDATA_TOKEN instead."
            )
        if self.earthdata_auth_strategy not in VALID_AUTH_STRATEGIES:
            raise ValueError(
                f"Unsupported EARTHDATA_AUTH_STRATEGY: '{self.earthdata_auth_strategy}'. "
                f"Expected one of: {sorted(VALID_AUTH_STRATEGIES)}."
            )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def get_settings() -> Settings:
    """Read Earthdata settings from the environment (including ``.env``)."""
    username = _clean(os.getenv("EARTHDATA_USERNAME"))
    password = _clean(os.getenv("EARTHDATA_PASSWORD"))
    token = _clean(os.getenv("EARTHDATA_TOKEN"))

    strategy = _clean(os.getenv("EARTHDATA_AUTH_STRATEGY"))
    if strategy is None:
        # Never default to "all": it can fall through to an interactive prompt.
        strategy = "environment" if (username and password) or token else "netrc"
    strategy = strategy.lower()

    settings = Settings(
        earthdata_username=username,
        earthdata_password=password,
        earthdata_token=token,
        earthdata_auth_strategy=strategy,
    )
    settings.validate()
    return settings


# Load the project .env once, at import time, before any Earthdata login.
load_environment()
