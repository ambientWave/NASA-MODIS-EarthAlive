"""Shared chunked collection for environmental-variable time series.

Long date ranges are split into bounded chunks, collected one at a time, and
merged by observation date. Both the comparison jobs and the trend job use this
so chunking behavior stays identical.

The chunk is also where the temporal resolution is applied: the resolver chooses
a provider monthly product (12 files/year) or the daily product (365 files/year),
so a ``monthly`` request never pays for daily granules it will only average away.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from app.jobs.errors import JobCanceled
from app.services.temporal import period_chunks
from app.services.variables import collect_resolved_variable
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
    temporal_resolution: str = "daily",
    spatial_strategy: str = "coarse",
    sampling_mode: str = "all",
    samples_per_month: int = 1,
    sampling_seed: int | None = None,
    sampling_missing: str = "keep",
    collector: Collector | None = None,
) -> VariableSeries:
    """Collect ``variable_id`` over ``start_date``..``end_date`` in chunks."""
    resolution = str(temporal_resolution or "daily").lower()
    days_per_chunk = DEFAULT_MAX_DAYS_PER_JOB if max_days is None else int(max_days)
    if resolution == "daily":
        chunks = date_chunks(start_date, end_date, days_per_chunk)
    else:
        chunks = period_chunks(start_date, end_date, max_years=max(1, days_per_chunk // 366))
    if collector is None:
        collector = collect_resolved_variable

    merged: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    mean_raster: Any = None
    raster_bounds: tuple[float, float, float, float] | None = None
    spec = None
    frequency = "daily"
    collection_strategy = "daily"
    source_products: list[str] = []
    source_resolution: str | None = None
    sampling: dict[str, Any] = {}

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
                temporal_resolution=resolution,
                spatial_strategy=spatial_strategy,
                sampling_mode=sampling_mode,
                samples_per_month=samples_per_month,
                sampling_seed=sampling_seed,
                sampling_missing=sampling_missing,
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
        frequency = series.frequency
        collection_strategy = series.collection_strategy
        source_products = series.source_products or source_products
        source_resolution = series.source_resolution or source_resolution
        if series.sampling:
            sampling = series.sampling
        if progress is not None:
            progress("collecting", progress_label or variable_id, index, len(chunks))

    log(f"collection: {variable_id}: merged {len(merged)} {frequency} record(s)")

    if spec is None:
        raise ValueError(f"No collector output was produced for variable '{variable_id}'.")

    return VariableSeries(
        spec=spec,
        records=[merged[day] for day in sorted(merged)],
        warnings=warnings,
        mean_raster=mean_raster,
        raster_bounds=raster_bounds,
        frequency=frequency,
        collection_strategy=collection_strategy,
        source_products=source_products,
        source_resolution=source_resolution,
        sampling=sampling,
    )
