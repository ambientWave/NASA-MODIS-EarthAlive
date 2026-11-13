"""Job submission and status lookup for the Redis/RQ queue."""

from __future__ import annotations

import uuid
from typing import Any

from app.jobs.store import QUEUE_MODE, QUEUE_NAME, REDIS_URL, read_result, read_status, utc_now, write_status
from app.utils.logging import log


def _redis_connection():
    from redis import Redis

    return Redis.from_url(REDIS_URL)


def create_job(kind: str, payload: dict[str, Any]) -> str:
    """Create a job, enqueue it, and return its id.

    ``ANALYSIS_QUEUE_MODE=inline`` runs the job in-process, which is useful for
    tests and local debugging without Redis. The Docker stack uses ``redis``.
    """
    job_id = uuid.uuid4().hex
    write_status(
        job_id,
        {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "percent": 0.0,
        },
    )

    if QUEUE_MODE == "inline":
        from app.jobs.tasks import run_job

        log(f"jobs: {job_id}: running inline ({kind})")
        try:
            run_job(job_id, kind, payload)
        except Exception as exc:
            # Status and result files already record the failure, matching how a
            # queued job behaves. Submit still returns the job id.
            log(f"jobs: {job_id}: inline run failed: {type(exc).__name__}: {exc}")
        return job_id

    from rq import Queue

    try:
        queue = Queue(QUEUE_NAME, connection=_redis_connection())
        queue.enqueue(
            "app.jobs.tasks.run_job",
            job_id,
            kind,
            payload,
            job_id=job_id,
            job_timeout=-1,
            result_ttl=86400,
            failure_ttl=86400,
        )
    except Exception:
        status = read_status(job_id) or {"job_id": job_id, "kind": kind}
        status.update({"status": "failed", "error": "Could not enqueue job: Redis is unavailable.", "updated_at": utc_now()})
        write_status(job_id, status)
        raise
    log(f"jobs: {job_id}: queued ({kind})")
    return job_id


def get_job_status(job_id: str) -> dict[str, Any] | None:
    status = read_status(job_id)
    if status is not None:
        if status.get("status") in {"queued", "started"} and QUEUE_MODE != "inline":
            rq_job = _get_rq_job(job_id)
            if rq_job is not None:
                status["rq_status"] = rq_job.get_status(refresh=True)
        return status

    if QUEUE_MODE == "inline":
        return None
    rq_job = _get_rq_job(job_id)
    if rq_job is None:
        return None
    return {
        "job_id": job_id,
        "status": rq_job.get_status(refresh=True),
        "created_at": rq_job.created_at.isoformat() if rq_job.created_at else None,
        "result_url": f"/api/v1/analysis/jobs/{job_id}/result",
    }


def get_job_result(job_id: str) -> dict[str, Any] | None:
    result = read_result(job_id)
    if result is not None:
        return result
    if QUEUE_MODE == "inline":
        return None
    rq_job = _get_rq_job(job_id)
    if rq_job is None or not rq_job.is_finished:
        return None
    return rq_job.result


def _get_rq_job(job_id: str):
    try:
        from rq.job import Job

        return Job.fetch(job_id, connection=_redis_connection())
    except Exception:
        return None
