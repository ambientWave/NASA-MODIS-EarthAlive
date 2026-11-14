"""Shared chunked collection for environmental-variable time series.

Long date ranges are split into bounded chunks, collected one at a time, and
merged by observation date. Both the comparison jobs and the trend job use this
so chunking behavior stays identical.

The chunk is also where the temporal resolution is applied. ``collect_resolved_variable``
reads the product the catalog selector chose for that request: a provider
monthly/aggregate product when one exists, a coarser composite (MOD11A2) when it
does not, or the daily product. Sampled requests are chunked one year at a time
so each AppEEARS task carries at most ~12 date ranges.
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
    sampling_mode: str = "full_period",
    samples_per_month: int = 1,
    sampling_seed: int | None = None,
    sampling_missing: str = "keep",
    collection_product: str | None = None,
    collector: Collector | None = None,
) -> VariableSeries:
    """Collect ``variable_id`` over ``start_date``..``end_date`` in chunks."""
    resolution = str(temporal_resolution or "daily").lower()
    if collection_product is None:
        # Comparisons enter here without an API-level selection; resolve the
        # same product decision so every job reads the same dataset.
        try:
            from datetime import date as _date

            from app.catalog.selector import resolve_selection

            selection = resolve_selection(
                variable_id,
                _date.fromisoformat(start_date),
                _date.fromisoformat(end_date),
                resolution,
                spatial_strategy,
                sampling_mode,
                samples_per_month,
            )
            collection_product = selection.product_name
        except (ValueError, KeyError):
            collection_product = None
    days_per_chunk = DEFAULT_MAX_DAYS_PER_JOB if max_days is None else int(max_days)
    if resolution == "daily":
        chunks = date_chunks(start_date, end_date, days_per_chunk)
    else:
        # Sampled requests become one AppEEARS date range per observation, so
        # keep chunks to a single year to stay well inside task-size limits.
        max_years = 1 if sampling_mode == "random_sample" else max(1, days_per_chunk // 366)
        chunks = period_chunks(start_date, end_date, max_years=max_years)
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
                collection_product=collection_product,
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
