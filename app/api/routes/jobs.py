"""Job inspection, result retrieval, and cancellation endpoints.

Jobs are persisted under ``data/jobs/{job_id}/`` as ``status.json`` and
``result.json``; Redis/RQ only carries the queue entry. The status document now
also stores the original request, so one lookup answers "what was this job".
"""

from __future__ import annotations

import base64
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from app.jobs import JobNotCancelable, cancel_job, get_job_result, get_job_status, list_jobs
from app.services.pdf_report_service import build_analysis_pdf

router = APIRouter(prefix="/analysis/jobs", tags=["Jobs"])

MAX_LIST_LIMIT = 200


def _require_status(job_id: str) -> dict[str, Any]:
    status = get_job_status(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return status


@router.get("", summary="List jobs, newest first.")
def jobs_index(
    limit: int = Query(50, ge=1, le=MAX_LIST_LIMIT, description="Maximum number of jobs to return."),
    offset: int = Query(0, ge=0, description="Number of jobs to skip."),
    kind: str | None = Query(None, description="Filter by job kind, e.g. `trend_analysis`."),
    status: str | None = Query(
        None,
        description="Filter by status. Accepts one status or a comma-separated list.",
        examples=["queued,started"],
    ),
) -> dict[str, Any]:
    statuses = {value.strip() for value in status.split(",") if value.strip()} if status else None
    return list_jobs(limit=limit, offset=offset, kind=kind, statuses=statuses)


@router.get("/{job_id}", summary="Fetch one job's status, progress, and original request.")
def job_status(job_id: str) -> dict[str, Any]:
    return _require_status(job_id)


@router.get("/{job_id}/result", summary="Fetch a finished job's result, including base64 charts.")
def job_result(job_id: str) -> dict[str, Any]:
    status = _require_status(job_id)
    if status.get("status") == "failed":
        try:
            status_code = int(status.get("status_code") or 409)
        except (TypeError, ValueError):
            status_code = 409
        raise HTTPException(
            status_code=status_code,
            detail=status.get("error_detail") or status.get("error", "Job failed."),
        )
    if status.get("status") in {"canceled", "canceling"}:
        raise HTTPException(status_code=409, detail="Job was canceled and has no result.")

    result = get_job_result(job_id)
    if result is None:
        if status.get("status") in {"queued", "started"}:
            raise HTTPException(status_code=409, detail="Job has not finished yet.")
        raise HTTPException(status_code=404, detail=f"Result not found for job: {job_id}")
    return result


@router.get(
    "/{job_id}/pdf",
    summary="Generate and stream a publication-quality PDF report for a finished analysis.",
    responses={
        200: {
            "content": {
                "application/pdf": {},
                "application/json": {},
            },
            "description": "Returns raw PDF binary stream or JSON base64 envelope.",
        },
        404: {"description": "Job or result not found."},
        409: {"description": "Job has not finished yet or was canceled."},
    },
)
def job_pdf(
    job_id: str,
    download: bool = Query(
        False,
        description="If true, sets Content-Disposition to attachment to trigger direct browser file save.",
    ),
    format: Literal["binary", "base64"] = Query(
        "binary",
        description="'binary' streams raw application/pdf; 'base64' returns JSON envelope with base64 string.",
    ),
) -> Response:
    result = job_result(job_id)
    try:
        pdf_bytes = build_analysis_pdf(result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to generate PDF report: {exc}") from exc

    if format == "base64":
        return JSONResponse(
            content={
                "job_id": job_id,
                "content_type": "application/pdf",
                "pdf_base64": base64.b64encode(pdf_bytes).decode("ascii"),
                "filename": f"analysis_{job_id}.pdf",
            }
        )

    disposition_type = "attachment" if download else "inline"
    filename = f"analysis_{job_id}.pdf"
    headers = {
        "Content-Disposition": f'{disposition_type}; filename="{filename}"',
        "Content-Length": str(len(pdf_bytes)),
    }
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)



@router.post(
    "/{job_id}/cancel",
    summary="Request cancellation of a queued or running job.",
    responses={
        200: {"description": "Cancellation accepted. Running jobs report `canceling` until the worker stops."},
        404: {"description": "Job not found."},
        409: {"description": "Job already finished/failed, or inline jobs cannot be canceled."},
    },
)
def job_cancel(
    job_id: str,
    force: bool = Query(
        False,
        description=(
            "Stop a running job immediately by killing the worker horse. Without this, the job stops "
            "at the next cooperative checkpoint (after the in-flight download/processing step)."
        ),
    ),
) -> dict[str, Any]:
    try:
        status = cancel_job(job_id, force=force)
    except JobNotCancelable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if status is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return status
