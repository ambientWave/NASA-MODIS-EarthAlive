"""Filesystem path helpers."""

from __future__ import annotations

from pathlib import Path


def ensure_directory(path: str | Path) -> Path:
    """Create a directory (and any missing parents) if it does not exist.

    Returns the path as a ``Path`` so callers can chain it.
    """
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def ensure_parent_directory(path: str | Path) -> Path:
    """Create the parent directory of a file path if it does not exist.

    Returns the file path unchanged, as a ``Path``.
    """
    file_path = Path(path)
    ensure_directory(file_path.parent)
    return file_path


def require_file(path: str | Path, description: str = "file") -> Path:
    """Return ``path`` as a ``Path`` after checking that it is an existing file.

    Raises ``FileNotFoundError`` with an actionable message instead of letting a
    lower-level library fail with a less obvious error.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"{description} not found: {file_path.resolve()}")
    return file_path
