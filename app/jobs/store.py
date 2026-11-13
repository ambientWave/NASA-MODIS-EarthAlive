"""Filesystem-backed job status/result storage shared by API and workers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
JOBS_DIR = PROJECT_ROOT / "data" / "jobs"
QUEUE_NAME = "analysis"
QUEUE_MODE = os.environ.get("ANALYSIS_QUEUE_MODE", "redis").lower()
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")


def job_dir(job_id: str) -> Path:
    directory = JOBS_DIR / job_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def status_path(job_id: str) -> Path:
    return job_dir(job_id) / "status.json"


def result_path(job_id: str) -> Path:
    return job_dir(job_id) / "result.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(job_id: str, payload: dict[str, Any]) -> None:
    path = status_path(job_id)
    tmp = path.with_suffix(".tmp")
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
    path = result_path(job_id)
    tmp = path.with_suffix(".tmp")
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
