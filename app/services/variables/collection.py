"""Shared chunked collection for environmental-variable time series.

Long date ranges are split into bounded chunks, collected one at a time, and
merged by observation date. Both the comparison jobs and the trend job use this
so chunking behavior stays identical.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from app.jobs.errors import JobCanceled
from app.services.variables import collect_variable
from app.services.variables.base import CollectionRequest, VariableSeries
from app.services.variables.remote_grid import date_chunks
from app.utils.logging import log

DEFAULT_MAX_DAYS_PER_JOB = int(os.environ.get("MAX_DAYS_PER_DAILY_JOB", "1826"))

ProgressCallback = Callable[[str, str, int, int], None]
Collector = Callable[[str, CollectionRequest], VariableSeries]


def collect_chunked_variable(
    variable_id: str,
    aoi: dict[str, float],
    start_date: str,
    end_date: str,
    output_dir: str | Path,
    *,
    max_days: int | None = None,
    progress: ProgressCallback | None = None,
    progress_label: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
    collector: Collector = collect_variable,
) -> VariableSeries:
    """Collect ``variable_id`` over ``start_date``..``end_date`` in chunks."""
    days_per_chunk = DEFAULT_MAX_DAYS_PER_JOB if max_days is None else int(max_days)
    chunks = date_chunks(start_date, end_date, days_per_chunk)

    merged: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    mean_raster: Any = None
    raster_bounds: tuple[float, float, float, float] | None = None
    spec = None

    for index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        if should_cancel is not None and should_cancel():
            raise JobCanceled()

        log(f"collection: {variable_id}: chunk {index}/{len(chunks)} {chunk_start} -> {chunk_end}")
        series = collector(
            variable_id,
            CollectionRequest(
                aoi=aoi,
                start_date=chunk_start,
                end_date=chunk_end,
                output_dir=Path(output_dir),
                temporal_resolution="daily",
                should_cancel=should_cancel,
                progress=progress,
                progress_label=progress_label or variable_id,
                chunk_index=index,
                chunk_total=len(chunks),
            ),
        )
        spec = series.spec
        for record in series.records:
            merged[str(record["observation_date"])] = record
        warnings.extend(series.warnings)
        if series.mean_raster is not None:
            mean_raster = series.mean_raster
            raster_bounds = series.raster_bounds
        if progress is not None:
            progress("collecting", progress_label or variable_id, index, len(chunks))

    log(f"collection: {variable_id}: merged {len(merged)} daily record(s)")

    if spec is None:
        raise ValueError(f"No collector output was produced for variable '{variable_id}'.")

    return VariableSeries(
        spec=spec,
        records=[merged[day] for day in sorted(merged)],
        warnings=warnings,
        mean_raster=mean_raster,
        raster_bounds=raster_bounds,
    )
