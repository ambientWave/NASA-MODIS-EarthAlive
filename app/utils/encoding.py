"""Reusable byte/file encoding helpers."""

from __future__ import annotations

import base64
from pathlib import Path

from app.utils.paths import require_file


def encode_bytes_base64(data: bytes) -> str:
    """Return ``data`` encoded as a standard base64 ASCII string."""
    return base64.b64encode(data).decode("ascii")


def encode_file_base64(path: str | Path, description: str = "file") -> str:
    """Read ``path`` and return its contents as a base64 ASCII string.

    Raises ``FileNotFoundError`` through :func:`require_file` when the file does
    not exist, so callers get an actionable error instead of a raw OSError.
    """
    file_path = require_file(path, description)
    return encode_bytes_base64(file_path.read_bytes())
