"""RQ worker entrypoints for queued analysis jobs."""

from __future__ import annotations

from typing import Any

from app.jobs.errors import JobError
from app.jobs.store import read_status, utc_now, write_result, write_status
from app.services.comparison_service import execute_region_comparison, execute_variable_comparison
from app.services.trend_service import execute_trend_analysis
from app.utils.logging import log


def _progress(job_id: str, kind: str, stage: str, label: str, current: int, total: int) -> None:
    status = read_status(job_id) or {"job_id": job_id, "kind": kind, "created_at": utc_now()}
    status.update(
        {
            "status": "started",
            "stage": stage,
            "label": label,
            "current": int(current),
            "total": int(total),
            "percent": 0.0 if total <= 0 else round((current / total) * 100.0, 2),
            "updated_at": utc_now(),
        }
    )
    write_status(job_id, status)


def run_job(job_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run one queued analysis job and persist its final result."""
    log(f"jobs: {job_id}: starting {kind}")
    initial = read_status(job_id) or {"job_id": job_id, "kind": kind, "created_at": utc_now()}
    initial.update({"status": "started", "started_at": utc_now(), "updated_at": utc_now()})
    write_status(job_id, initial)

    def progress(stage: str, label: str, current: int, total: int) -> None:
        _progress(job_id, kind, stage, label, current, total)

    try:
        if kind == "variable_comparison":
            result = execute_variable_comparison(payload, job_id, progress=progress)
        elif kind == "region_comparison":
            result = execute_region_comparison(payload, job_id, progress=progress)
        elif kind == "trend_analysis":
            result = execute_trend_analysis(payload, job_id, progress=progress)
        else:
            raise ValueError(f"Unknown job kind: '{kind}'")

        write_result(job_id, result)
        status = read_status(job_id) or initial
        status.update(
            {
                "status": "finished",
                "stage": "finished",
                "percent": 100.0,
                "finished_at": utc_now(),
                "updated_at": utc_now(),
                "result_url": f"/api/v1/analysis/jobs/{job_id}/result",
            }
        )
        write_status(job_id, status)
        log(f"jobs: {job_id}: finished")
        return result
    except JobError as exc:
        status = read_status(job_id) or initial
        status.update(
            {
                "status": "failed",
                "stage": "failed",
                "error": exc.detail,
                "error_detail": exc.detail,
                "status_code": exc.status_code,
                "finished_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        write_status(job_id, status)
        log(f"jobs: {job_id}: failed with HTTP {exc.status_code}: {exc.detail}")
        raise
    except Exception as exc:
        status = read_status(job_id) or initial
        status.update(
            {
                "status": "failed",
                "stage": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "error_detail": f"{type(exc).__name__}: {exc}",
                "status_code": 500,
                "finished_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        write_status(job_id, status)
        log(f"jobs: {job_id}: failed: {type(exc).__name__}: {exc}")
        raise
