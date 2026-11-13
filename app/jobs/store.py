"""Filesystem-backed job status/result storage shared by API and workers."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[3]
JOBS_DIR = PROJECT_ROOT / "data" / "jobs"
QUEUE_NAME = "analysis"
QUEUE_MODE = os.environ.get("ANALYSIS_QUEUE_MODE", "redis").lower()
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")


def job_dir(job_id: str) -> Path:
    """Return (and create) the directory that owns one job's artifacts."""
    directory = JOBS_DIR / job_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def status_path(job_id: str) -> Path:
    # Deliberately does not create the directory: readers (including 404
    # lookups) must not leave empty job directories behind.
    return JOBS_DIR / job_id / "status.json"


def result_path(job_id: str) -> Path:
    return JOBS_DIR / job_id / "result.json"


def cancel_marker_path(job_id: str) -> Path:
    return JOBS_DIR / job_id / "cancel.requested"


def mark_cancel_requested(job_id: str) -> None:
    """Create the cancellation marker.

    Kept separate from ``status.json`` so a concurrent progress write cannot
    clobber the flag. Existence of the file is the request.
    """
    job_dir(job_id)
    cancel_marker_path(job_id).write_text(utc_now(), encoding="utf-8")


def is_cancel_requested(job_id: str) -> bool:
    if cancel_marker_path(job_id).is_file():
        return True
    status = read_status(job_id) or {}
    return bool(status.get("cancel_requested"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redis_connection():
    """Redis client used for the queue and comparison part counters."""
    from redis import Redis

    return Redis.from_url(REDIS_URL)


def write_status(job_id: str, payload: dict[str, Any]) -> None:
    job_dir(job_id)
    if is_cancel_requested(job_id) and not payload.get("cancel_requested"):
        # The API and the worker both write status.json. Never let a progress
        # update erase a cancellation that arrived meanwhile.
        payload = {**payload, "cancel_requested": True}
    path = status_path(job_id)
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_status(job_id: str) -> dict[str, Any] | None:
    path = status_path(job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_result(job_id: str, payload: dict[str, Any]) -> None:
    job_dir(job_id)
    path = result_path(job_id)
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_result(job_id: str) -> dict[str, Any] | None:
    path = result_path(job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def iter_statuses() -> Iterator[dict[str, Any]]:
    """Yield every readable ``status.json`` under ``JOBS_DIR``.

    Used by the job-listing endpoint. Directories without a status file are
    skipped rather than created, and unreadable files are ignored.
    """
    if not JOBS_DIR.exists():
        return
    for directory in JOBS_DIR.iterdir():
        if not directory.is_dir():
            continue
        path = directory / "status.json"
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            yield payload


def prune_old_jobs(max_age_days: int = 7) -> None:
    """Best-effort cleanup of old result directories."""
    if not JOBS_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
    for directory in JOBS_DIR.iterdir():
        if not directory.is_dir():
            continue
        try:
            if directory.stat().st_mtime < cutoff:
                for child in directory.rglob("*"):
                    if child.is_file():
                        child.unlink()
                for child in sorted(directory.rglob("*"), reverse=True):
                    if child.is_dir():
                        child.rmdir()
                directory.rmdir()
        except OSError:
            continue
