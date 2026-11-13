"""Job submission and status lookup for the Redis/RQ queue."""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from typing import Any

from app.jobs.errors import JobNotCancelable
from app.jobs.store import (
    QUEUE_MODE,
    QUEUE_NAME,
    iter_statuses,
    mark_cancel_requested,
    read_result,
    read_status,
    redis_connection,
    utc_now,
    write_status,
)
from app.utils.logging import log

COMPARISON_KINDS = {"variable_comparison", "region_comparison"}


def _redis_connection():
    return redis_connection()


def _job_timeout() -> int:
    """RQ timeout in seconds (``ANALYSIS_JOB_TIMEOUT``, default 6 hours).

    Without a finite timeout a wedged download or GDAL process runs forever and
    the queue never releases the worker.
    """
    raw = os.environ.get("ANALYSIS_JOB_TIMEOUT", "21600")
    try:
        timeout = int(float(raw))
    except (TypeError, ValueError):
        timeout = 21600
    return timeout if timeout > 0 else 21600


def comparison_part_labels(kind: str, payload: dict[str, Any]) -> list[str]:
    """Labels for one comparison part: one per variable or per region."""
    if kind == "variable_comparison":
        return [str(variable) for variable in payload.get("variables") or []]
    if kind == "region_comparison":
        return [f"region_{index}" for index in range(1, len(payload.get("regions") or []) + 1)]
    return []


def create_job(kind: str, payload: dict[str, Any]) -> str:
    """Create a job, enqueue it, and return its id.

    ``ANALYSIS_QUEUE_MODE=inline`` runs the job in-process, which is useful for
    tests and local debugging without Redis. The Docker stack uses ``redis``.

    Comparisons with more than one part are split into one RQ job per part so
    idle worker replicas can work on them in parallel; the last part to finish
    assembles the result. Single-part comparisons keep the normal path.
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
            # The request is persisted so a job can be inspected later without
            # replaying the original HTTP call (and so cancellations can show
            # what was being worked on).
            "request": payload,
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

    if kind in COMPARISON_KINDS:
        labels = comparison_part_labels(kind, payload)
        if len(labels) > 1:
            return _enqueue_comparison(job_id, kind, payload, labels)

    from rq import Queue

    try:
        queue = Queue(QUEUE_NAME, connection=_redis_connection())
        queue.enqueue(
            "app.jobs.tasks.run_job",
            job_id,
            kind,
            payload,
            job_id=job_id,
            job_timeout=_job_timeout(),
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


def _enqueue_comparison(
    job_id: str,
    kind: str,
    payload: dict[str, Any],
    labels: list[str],
) -> str:
    """Split a comparison into per-part RQ jobs plus a timeout watchdog."""
    from rq import Queue

    parts = [
        {"index": index, "label": label, "rq_job_id": f"{job_id}-p{index}"}
        for index, label in enumerate(labels)
    ]
    status = read_status(job_id) or {"job_id": job_id, "kind": kind}
    status.update({"parts": parts, "parts_total": len(parts)})
    write_status(job_id, status)

    queue = None
    timeout = _job_timeout()
    enqueued: list[dict[str, Any]] = []
    try:
        queue = Queue(QUEUE_NAME, connection=_redis_connection())
        for part in parts:
            queue.enqueue(
                "app.jobs.tasks.run_comparison_part",
                job_id,
                kind,
                payload,
                part["index"],
                job_id=part["rq_job_id"],
                job_timeout=timeout,
                result_ttl=86400,
                failure_ttl=86400,
            )
            enqueued.append(part)
        # If a part is killed hard (RQ timeout), it never bumps the completion
        # counter. This delayed job settles the parent instead of leaving it
        # "started" forever.
        queue.enqueue_in(
            timedelta(seconds=timeout + 120),
            "app.jobs.tasks.finalize_comparison_watchdog",
            job_id,
            kind,
            payload,
            job_id=f"{job_id}-watchdog",
            job_timeout=900,
            result_ttl=86400,
            failure_ttl=86400,
        )
    except Exception:
        for part in enqueued:
            _cancel_queued_rq_job(part["rq_job_id"])
        status.update(
            {
                "status": "failed",
                "error": "Could not enqueue comparison parts: Redis is unavailable.",
                "updated_at": utc_now(),
            }
        )
        write_status(job_id, status)
        raise

    log(f"jobs: {job_id}: queued {kind} as {len(parts)} part(s): {', '.join(labels)}")
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


def list_jobs(
    *,
    limit: int = 50,
    offset: int = 0,
    kind: str | None = None,
    statuses: set[str] | None = None,
) -> dict[str, Any]:
    """Return job status documents newest-first with simple pagination."""
    items = [status for status in iter_statuses() if status.get("job_id")]
    if kind:
        items = [item for item in items if str(item.get("kind")) == kind]
    if statuses:
        items = [item for item in items if str(item.get("status")) in statuses]

    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    total = len(items)
    window = items[offset : offset + limit]
    next_offset = offset + len(window)
    return {
        "items": window,
        "total": total,
        "limit": limit,
        "offset": offset,
        "next": next_offset if next_offset < total else None,
    }


def cancel_job(job_id: str, *, force: bool = False) -> dict[str, Any] | None:
    """Request cancellation of a job.

    Queued jobs are removed from Redis immediately. Running jobs stop at the
    next worker checkpoint (``status: canceling``) unless ``force`` is set, in
    which case RQ is asked to stop the executing horse right away. Returns
    ``None`` when the job does not exist.
    """
    status = read_status(job_id)
    if status is None:
        return None

    current = str(status.get("status"))
    if current in {"canceled", "canceling"}:
        return status
    if current in {"finished", "failed"}:
        raise JobNotCancelable(f"Job {job_id} is already {current} and cannot be canceled.")
    if QUEUE_MODE == "inline":
        raise JobNotCancelable(
            "Inline jobs run inside the submitting request and cannot be canceled. "
            "Use the Redis queue mode to enable cancellation."
        )

    requested_at = utc_now()
    mark_cancel_requested(job_id)
    status.update(
        {
            "cancel_requested": True,
            "cancel_requested_at": requested_at,
            "updated_at": requested_at,
        }
    )

    if status.get("parts"):
        return _cancel_comparison_parts(status, force=force, requested_at=requested_at)

    if current == "queued":
        if _cancel_queued_rq_job(job_id):
            status.update({"status": "canceled", "stage": "canceled", "canceled_at": requested_at})
            log(f"jobs: {job_id}: canceled while queued")
        else:
            # The job may have been dequeued between our read and the RQ call.
            # The worker checks the flag before starting work.
            status.update({"status": "canceling", "stage": "canceling"})
            log(f"jobs: {job_id}: cancel requested while queued")
        write_status(job_id, status)
        return status

    if force and _stop_running_rq_job(job_id):
        status.update({"status": "canceled", "stage": "canceled", "canceled_at": requested_at})
        log(f"jobs: {job_id}: force-canceled while running")
    else:
        status.update({"status": "canceling", "stage": "canceling"})
        log(f"jobs: {job_id}: cancel requested while running")
    write_status(job_id, status)
    return status


def _cancel_comparison_parts(
    status: dict[str, Any], *, force: bool, requested_at: str
) -> dict[str, Any]:
    """Cancel every RQ job that makes up a distributed comparison."""
    job_id = str(status.get("job_id"))
    removed: list[int] = []
    active = False

    for part in status.get("parts") or []:
        index = int(part.get("index", 0))
        rq_id = part.get("rq_job_id")
        if not rq_id:
            continue
        if force:
            _stop_running_rq_job(rq_id)
            removed.append(index)
            continue
        if _cancel_queued_rq_job(rq_id):
            removed.append(index)
            continue
        rq_job = _get_rq_job(rq_id)
        if rq_job is not None and rq_job.get_status(refresh=True) not in {
            "canceled",
            "stopped",
            "failed",
            "finished",
        }:
            active = True

    if removed:
        try:
            from app.jobs.tasks import mark_parts_canceled

            mark_parts_canceled(job_id, sorted(set(removed)))
        except Exception as exc:
            log(f"jobs: {job_id}: could not mark canceled parts: {type(exc).__name__}: {exc}")

    if force or not active:
        status.update(
            {"status": "canceled", "stage": "canceled", "canceled_at": requested_at, "updated_at": requested_at}
        )
        log(f"jobs: {job_id}: comparison canceled ({len(removed)} part(s) stopped)")
    else:
        status.update({"status": "canceling", "stage": "canceling", "updated_at": requested_at})
        log(f"jobs: {job_id}: cancel requested for comparison parts")
    write_status(job_id, status)
    return status


def _cancel_queued_rq_job(job_id: str) -> bool:
    job = _get_rq_job(job_id)
    if job is None:
        return False
    try:
        job.cancel()
        return True
    except Exception as exc:
        log(f"jobs: {job_id}: could not cancel queued RQ job: {type(exc).__name__}: {exc}")
        return False


def _stop_running_rq_job(job_id: str) -> bool:
    try:
        from rq.command import send_stop_job_command

        send_stop_job_command(_redis_connection(), job_id)
        return True
    except Exception as exc:
        log(f"jobs: {job_id}: could not send stop command: {type(exc).__name__}: {exc}")
        return False


def _get_rq_job(job_id: str):
    try:
        from rq.job import Job

        return Job.fetch(job_id, connection=_redis_connection())
    except Exception:
        return None
