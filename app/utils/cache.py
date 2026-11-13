"""Byte-budgeted, process-safe disk cache for analysis artifacts.

Downloads and AOI-subset rasters are shared by every job and every worker
process, so the cache must survive concurrent writers and must not grow without
bound (the container's data volume is small compared with a year of MODIS
granules).

Two rules keep it safe without a central lock:

* Writers always go through :func:`atomic_output`, which writes to a unique
  temporary file and renames it into place. A reader therefore sees either no
  file or a complete one.
* Eviction skips files modified in the last ``MIN_TRIM_AGE_SECONDS``, which
  covers the only realistic race: a worker that is mid-read while another
  worker trims the cache.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.utils.logging import log

# Same anchor as ``app.jobs.store`` and ``modis_processor``: in the container
# this is /srv/backend, so the cache lives in the ``./data`` bind mount and is
# shared by every worker replica.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "cache"
DEFAULT_BUDGET_GB = 0.5
MIN_TRIM_AGE_SECONDS = 600


def cache_root() -> Path:
    """Root of the shared cache (``ANALYSIS_CACHE_DIR`` overrides the default)."""
    override = os.environ.get("ANALYSIS_CACHE_DIR")
    return Path(override) if override else DEFAULT_CACHE_DIR


def cache_budget_bytes() -> int:
    """Budget in bytes (``ANALYSIS_CACHE_GB``, default 0.5 GB)."""
    try:
        gigabytes = float(os.environ.get("ANALYSIS_CACHE_GB", DEFAULT_BUDGET_GB))
    except (TypeError, ValueError):
        gigabytes = DEFAULT_BUDGET_GB
    return max(int(gigabytes * 1024**3), 0)


def directory_size(directory: str | Path) -> int:
    """Total size of every regular file below ``directory``, in bytes."""
    total = 0
    directory = Path(directory)
    if not directory.exists():
        return 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def touch(path: str | Path) -> None:
    """Mark ``path`` as recently used so LRU eviction leaves it alone."""
    try:
        os.utime(path, None)
    except OSError:
        pass


@contextmanager
def atomic_output(target: str | Path) -> Iterator[Path]:
    """Yield a temporary path and atomically move it to ``target`` on success.

    GDAL subprocesses write to the yielded path. The ``.part.tif`` suffix keeps
    the output recognisable while the format is still forced with ``-of``.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.stem}.{uuid.uuid4().hex}.part.tif")
    try:
        yield temporary
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def trim_cache(root: str | Path | None = None, budget_bytes: int | None = None) -> int:
    """Delete least-recently-used files until ``root`` fits the budget.

    Returns the number of bytes reclaimed. Fresh files are skipped, so a worker
    that is still reading a granule will not have it deleted underneath it.
    """
    root = Path(root) if root is not None else cache_root()
    if not root.exists():
        return 0

    budget = cache_budget_bytes() if budget_bytes is None else max(int(budget_bytes), 0)
    now = time.time()

    total = 0
    candidates: list[tuple[float, int, Path]] = []
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        total += stat.st_size
        if now - stat.st_mtime >= MIN_TRIM_AGE_SECONDS:
            candidates.append((stat.st_mtime, stat.st_size, path))

    if total <= budget:
        return 0

    candidates.sort()
    reclaimed = 0
    for _, size, path in candidates:
        if total <= budget:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        reclaimed += size

    if reclaimed:
        log(f"cache: reclaimed {reclaimed / 1024 / 1024:.1f} MB from {root}")
    return reclaimed
