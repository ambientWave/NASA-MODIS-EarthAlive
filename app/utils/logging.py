"""Minimal stdout logging for long-running requests.

The trend endpoint can spend minutes downloading and processing granules inside
a single request. These helpers print immediately flushed, timestamped lines so
progress is visible in ``docker compose logs`` (or any captured stdout) instead
of the request looking hung.
"""

from __future__ import annotations

import sys
from datetime import datetime


def log(message: str) -> None:
    """Print a timestamped line to stdout and flush it immediately."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", file=sys.stdout, flush=True)


def log_progress(label: str, index: int, total: int, detail: str = "") -> None:
    """Log ``label`` with a ``i/n`` counter, e.g. ``[3/31] processing 2024-01-03``."""
    suffix = f" {detail}" if detail else ""
    log(f"{label} [{index}/{total}]{suffix}")
