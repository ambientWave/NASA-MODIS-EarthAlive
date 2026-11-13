"""Typed failures for background analysis jobs.

Workers cannot return HTTP status codes directly, so a job carries the status
code it wants the result endpoint to use once it fails. This keeps the original
API semantics: a missing GDAL install is still a ``503`` and an empty series is
still a ``404`` when the caller fetches the result.
"""

from __future__ import annotations


class JobError(Exception):
    """A job failure that maps to a specific HTTP status code."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)
