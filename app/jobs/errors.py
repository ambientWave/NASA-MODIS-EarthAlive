"""Typed failures for background analysis jobs.

Workers cannot return HTTP status codes directly, so a job carries the status
code it wants the result endpoint to use once it fails. This keeps the original
API semantics: a missing GDAL install is still a ``503`` and an empty series is
still a ``404`` when the caller fetches the result.
"""

from __future__ import annotations

from typing import Callable


class JobError(Exception):
    """A job failure that maps to a specific HTTP status code."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)


class JobCanceled(Exception):
    """Raised by a worker at a cancellation checkpoint.

    The job ends with ``status: canceled`` and no result; it is not a failure,
    so the worker swallows it instead of recording an error.
    """

    def __init__(self, detail: str = "Job was canceled by the user.") -> None:
        super().__init__(detail)
        self.detail = str(detail)


class JobNotCancelable(Exception):
    """Raised when a job is already finished/failed or cannot be canceled."""


def raise_if_canceled(should_cancel: "Callable[[], bool] | None") -> None:
    """Convenience checkpoint used by services that only receive a callback."""
    if should_cancel is not None and should_cancel():
        raise JobCanceled()
