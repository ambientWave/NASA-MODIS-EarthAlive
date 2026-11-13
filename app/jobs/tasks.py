"""RQ worker entrypoints for queued analysis jobs."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.jobs.errors import JobCanceled, JobError, raise_if_canceled
from app.jobs.store import (
    JOBS_DIR,
    is_cancel_requested,
    read_status,
    redis_connection,
    utc_now,
    write_result,
    write_status,
)
from app.services.comparison_service import (
    assemble_region_comparison,
    assemble_variable_comparison,
    execute_region_comparison,
    execute_variable_comparison,
    failed_part,
    region_part,
    variable_part,
)
from app.services.trend_service import execute_trend_analysis
from app.utils.logging import log

PARTS_DIRNAME = "parts"
PART_COUNTER_TTL_SECONDS = 7 * 86400


def _progress(
    job_id: str,
    kind: str,
    stage: str,
    label: str,
    current: int,
    total: int,
    percent: float | None = None,
) -> None:
    status = read_status(job_id) or {"job_id": job_id, "kind": kind, "created_at": utc_now()}
    cancel_requested = is_cancel_requested(job_id)
    if percent is None:
        percent = 0.0 if total <= 0 else (current / total) * 100.0
    status.update(
        {
            # Keep reporting "canceling" if a cancel arrived while this step ran.
            "status": "canceling" if cancel_requested else "started",
            "cancel_requested": cancel_requested,
            "stage": stage,
            "label": label,
            "current": int(current),
            "total": int(total),
            "percent": round(max(0.0, min(float(percent), 100.0)), 2),
            "updated_at": utc_now(),
        }
    )
    write_status(job_id, status)


def _mark_canceled(job_id: str, fallback: dict[str, Any]) -> dict[str, Any]:
    """Record a cooperative cancellation without leaving failure metadata."""
    status = read_status(job_id) or fallback
    for key in ("error", "error_detail", "status_code"):
        status.pop(key, None)
    status.update(
        {
            "status": "canceled",
            "stage": "canceled",
            "cancel_requested": True,
            "canceled_at": utc_now(),
            "updated_at": utc_now(),
        }
    )
    write_status(job_id, status)
    return status


def run_job(job_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run one queued analysis job and persist its final result."""
    log(f"jobs: {job_id}: starting {kind}")
    initial = read_status(job_id) or {"job_id": job_id, "kind": kind, "created_at": utc_now()}
    initial.update({"status": "started", "started_at": utc_now(), "updated_at": utc_now()})
    write_status(job_id, initial)

    def should_cancel() -> bool:
        return is_cancel_requested(job_id)

    def progress(stage: str, label: str, current: int, total: int, percent: float | None = None) -> None:
        _progress(job_id, kind, stage, label, current, total, percent)

    try:
        # A job can be canceled while it waits in the queue.
        raise_if_canceled(should_cancel)

        if kind == "variable_comparison":
            result = execute_variable_comparison(
                payload, job_id, progress=progress, should_cancel=should_cancel
            )
        elif kind == "region_comparison":
            result = execute_region_comparison(
                payload, job_id, progress=progress, should_cancel=should_cancel
            )
        elif kind == "trend_analysis":
            result = execute_trend_analysis(
                payload, job_id, progress=progress, should_cancel=should_cancel
            )
        else:
            raise ValueError(f"Unknown job kind: '{kind}'")

        # Check before publishing a result so a cancel that landed while the
        # last analysis step ran still wins over a successful finish.
        raise_if_canceled(should_cancel)

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
    except JobCanceled as exc:
        _mark_canceled(job_id, initial)
        log(f"jobs: {job_id}: canceled: {exc.detail}")
        # Not a failure: swallow so RQ does not record a traceback.
        return {"success": False, "canceled": True, "job_id": job_id}
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


# ---------------------------------------------------------------------------
# Distributed comparisons
#
# A comparison job is a *parent* status document with one RQ job per part.
# Parts run on whatever worker is free, write their result under
# ``data/jobs/<parent>/parts/``, and the last part to finish (detected with an
# atomic Redis counter) assembles the final result. No worker is reserved for
# the parent, so two workers really do work on two parts at once.
# ---------------------------------------------------------------------------


def _parts_dir(parent_id: str) -> Path:
    directory = JOBS_DIR / parent_id / PARTS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _part_status_path(parent_id: str, index: int) -> Path:
    return _parts_dir(parent_id) / f"{index}.status.json"


def _part_result_path(parent_id: str, index: int) -> Path:
    return _parts_dir(parent_id) / f"{index}.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_part_status(parent_id: str, index: int, payload: dict[str, Any]) -> None:
    payload = {**payload, "index": index, "updated_at": utc_now()}
    _write_json(_part_status_path(parent_id, index), payload)


def read_part_status(parent_id: str, index: int) -> dict[str, Any] | None:
    return _read_json(_part_status_path(parent_id, index))


def read_part_result(parent_id: str, index: int) -> dict[str, Any] | None:
    return _read_json(_part_result_path(parent_id, index))


def _refresh_parent_progress(parent_id: str, kind: str) -> None:
    """Recompute the parent's percent from every part's latest status.

    Each part writes its own status file, so no lock is needed. The parent
    percent only moves forward: a slower writer must not undo a newer one.
    """
    parent = read_status(parent_id)
    if parent is None:
        return

    total = int(parent.get("parts_total") or 0)
    if total <= 0:
        return

    statuses = [read_part_status(parent_id, index) or {} for index in range(total)]
    finished = sum(1 for item in statuses if item.get("status") in {"finished", "failed", "canceled"})
    percent = sum(float(item.get("percent") or 0.0) for item in statuses) / total
    active = [
        str(item.get("detail") or item.get("label") or "")
        for item in statuses
        if item.get("status") == "started"
    ]
    cancel_requested = is_cancel_requested(parent_id)

    label = f"{finished}/{total} part(s) finished"
    if active:
        label = f"{label} — {active[0]}"

    previous_percent = float(parent.get("percent") or 0.0)
    parent.update(
        {
            "status": "canceling" if cancel_requested else "started",
            "stage": "comparison",
            "label": label,
            "current": finished,
            "total": total,
            "percent": round(max(previous_percent, percent), 2),
            "cancel_requested": cancel_requested,
            "updated_at": utc_now(),
        }
    )
    if not parent.get("started_at"):
        parent["started_at"] = utc_now()
    write_status(parent_id, parent)


def _mark_parent_canceled(parent_id: str) -> None:
    status = read_status(parent_id) or {"job_id": parent_id}
    for key in ("error", "error_detail", "status_code"):
        status.pop(key, None)
    status.update(
        {
            "status": "canceled",
            "stage": "canceled",
            "cancel_requested": True,
            "canceled_at": utc_now(),
            "updated_at": utc_now(),
        }
    )
    write_status(parent_id, status)


def _mark_parent_failed(parent_id: str, exc: BaseException) -> None:
    status = read_status(parent_id) or {"job_id": parent_id}
    message = str(exc) if isinstance(exc, JobError) else f"{type(exc).__name__}: {exc}"
    status.update(
        {
            "status": "failed",
            "stage": "failed",
            "error": message,
            "error_detail": message,
            "status_code": getattr(exc, "status_code", 500),
            "finished_at": utc_now(),
            "updated_at": utc_now(),
        }
    )
    write_status(parent_id, status)
    log(f"jobs: {parent_id}: comparison failed: {message}")


def _maybe_finish_cancel(parent_id: str) -> None:
    """Flip the parent to ``canceled`` once every part has stopped."""
    if not is_cancel_requested(parent_id):
        return
    parent = read_status(parent_id) or {}
    if parent.get("status") in {"canceled", "finished", "failed"}:
        return
    total = int(parent.get("parts_total") or 0)
    if total <= 0:
        _mark_parent_canceled(parent_id)
        return
    for index in range(total):
        item = read_part_status(parent_id, index) or {}
        if item.get("status") not in {"finished", "failed", "canceled"}:
            return
    _mark_parent_canceled(parent_id)
    log(f"jobs: {parent_id}: all parts stopped; comparison canceled")


def mark_parts_canceled(parent_id: str, indexes: list[int]) -> None:
    """Mark queued parts cancelled so a canceled parent can settle."""
    for index in indexes:
        _write_part_status(
            parent_id,
            index,
            {"status": "canceled", "stage": "canceled", "percent": 0.0, "detail": "canceled before start"},
        )
    _refresh_parent_progress(parent_id, "comparison")
    _maybe_finish_cancel(parent_id)


def _assemble_comparison(parent_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    parent = read_status(parent_id) or {}
    total = int(parent.get("parts_total") or 0)
    parts: list[dict[str, Any]] = []
    for index in range(total):
        part = read_part_result(parent_id, index)
        if part is None:
            raise JobError(
                500,
                f"Comparison part {index + 1}/{total} produced no result. "
                "Retry the job, or check the worker logs for that part.",
            )
        parts.append(part)

    log(f"jobs: {parent_id}: assembling {kind} from {total} part(s)")
    parent.update(
        {
            "status": "started",
            "stage": "finalizing",
            "label": "assembling comparison result",
            "percent": max(float(parent.get("percent") or 0.0), 99.0),
            "updated_at": utc_now(),
        }
    )
    write_status(parent_id, parent)

    if kind == "variable_comparison":
        result = assemble_variable_comparison(payload, parent_id, parts)
    elif kind == "region_comparison":
        result = assemble_region_comparison(payload, parent_id, parts)
    else:
        raise ValueError(f"Unknown comparison kind: '{kind}'")

    write_result(parent_id, result)
    status = read_status(parent_id) or parent
    status.update(
        {
            "status": "finished",
            "stage": "finished",
            "label": "comparison finished",
            "percent": 100.0,
            "finished_at": utc_now(),
            "updated_at": utc_now(),
            "result_url": f"/api/v1/analysis/jobs/{parent_id}/result",
        }
    )
    write_status(parent_id, status)
    log(f"jobs: {parent_id}: comparison finished")
    return result


def _maybe_finalize(parent_id: str, kind: str, payload: dict[str, Any]) -> None:
    """Assemble once the Redis part counter reaches the number of parts."""
    parent = read_status(parent_id) or {}
    if parent.get("status") in {"finished", "failed", "canceled"}:
        return
    total = int(parent.get("parts_total") or 0)
    if total <= 0:
        return
    connection = redis_connection()
    key = f"analysis:parts:{parent_id}"
    done = int(connection.incr(key))
    connection.expire(key, PART_COUNTER_TTL_SECONDS)

    if done < total:
        return
    if is_cancel_requested(parent_id):
        _maybe_finish_cancel(parent_id)
        return

    try:
        _assemble_comparison(parent_id, kind, payload)
    except Exception as exc:
        _mark_parent_failed(parent_id, exc)


def run_comparison_part(
    parent_id: str,
    kind: str,
    payload: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    """Run one part of a distributed comparison on this worker."""
    parent = read_status(parent_id) or {}
    meta = next(
        (part for part in parent.get("parts") or [] if int(part.get("index", -1)) == index),
        {},
    )
    label = str(meta.get("label") or f"part_{index + 1}")
    log(f"jobs: {parent_id}: part {index + 1} ({label}) starting {kind}")

    _write_part_status(
        parent_id,
        index,
        {"label": label, "status": "started", "stage": "starting", "detail": label, "percent": 0.0},
    )
    _refresh_parent_progress(parent_id, kind)

    def should_cancel() -> bool:
        return is_cancel_requested(parent_id)

    def progress(
        stage: str,
        detail: str,
        current: int,
        total: int,
        percent: float | None = None,
    ) -> None:
        if percent is None:
            percent = 0.0 if total <= 0 else (current / total) * 100.0
        _write_part_status(
            parent_id,
            index,
            {
                "label": label,
                "status": "started",
                "stage": stage,
                "detail": detail,
                "percent": round(max(0.0, min(float(percent), 100.0)), 2),
            },
        )
        _refresh_parent_progress(parent_id, kind)

    try:
        raise_if_canceled(should_cancel)
        if kind == "variable_comparison":
            variable_id = payload["variables"][index]
            part = variable_part(
                payload,
                variable_id,
                parent_id,
                progress=progress,
                should_cancel=should_cancel,
                position=(index + 1, len(payload["variables"])),
            )
        elif kind == "region_comparison":
            label = f"region_{index + 1}"
            part = region_part(
                payload,
                payload["regions"][index],
                label,
                parent_id,
                progress=progress,
                should_cancel=should_cancel,
            )
        else:
            raise ValueError(f"Unknown comparison kind: '{kind}'")

        raise_if_canceled(should_cancel)
    except JobCanceled:
        _write_part_status(
            parent_id,
            index,
            {"label": label, "status": "canceled", "stage": "canceled", "detail": label, "percent": 0.0},
        )
        _refresh_parent_progress(parent_id, kind)
        _maybe_finish_cancel(parent_id)
        log(f"jobs: {parent_id}: part {index + 1} ({label}) canceled")
        return {"success": False, "canceled": True, "job_id": parent_id, "part": index}
    except Exception as exc:
        log(f"jobs: {parent_id}: part {index + 1} ({label}) failed: {type(exc).__name__}: {exc}")
        key = payload["variables"][index] if kind == "variable_comparison" else f"region_{index + 1}"
        part = failed_part(key, label, exc)

    _write_json(_part_result_path(parent_id, index), part)
    _write_part_status(
        parent_id,
        index,
        {"label": label, "status": "finished", "stage": "finished", "detail": label, "percent": 100.0},
    )
    _refresh_parent_progress(parent_id, kind)
    log(f"jobs: {parent_id}: part {index + 1} ({label}) finished")

    _maybe_finalize(parent_id, kind, payload)
    return part


def finalize_comparison_watchdog(parent_id: str, kind: str, payload: dict[str, Any]) -> None:
    """Last-resort assembler for a comparison that never reached its counter.

    RQ timeouts kill a worker horse without running ``run_comparison_part``'s
    epilogue, so the counter never reaches the part count. This delayed job
    settles the parent instead of leaving it ``started`` forever.
    """
    status = read_status(parent_id)
    if status is None or status.get("status") in {"finished", "failed", "canceled"}:
        return
    if is_cancel_requested(parent_id):
        _maybe_finish_cancel(parent_id)
        return

    total = int(status.get("parts_total") or 0)
    missing = [index + 1 for index in range(total) if read_part_result(parent_id, index) is None]
    if missing:
        _mark_parent_failed(
            parent_id,
            JobError(
                500,
                f"Comparison did not finish: part(s) {missing} produced no result. "
                "A worker may have timed out; retry the job.",
            ),
        )
        return

    try:
        _assemble_comparison(parent_id, kind, payload)
    except Exception as exc:
        _mark_parent_failed(parent_id, exc)
