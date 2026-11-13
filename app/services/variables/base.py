"""Shared contracts for environmental-variable time series collectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.jobs.errors import raise_if_canceled


@dataclass(frozen=True)
class PeriodSource:
    """A coarser-temporal product used for monthly/yearly requests.

    These products are produced by the data provider from every source
    observation (for example MOD11C3 monthly LST), so one file replaces a whole
    month of daily granules. ``reader`` selects how the file is opened:

    * ``"xarray"`` — netCDF/HDF5 opened through ``earthaccess.open``.
    * ``"gdal"`` — HDF4 CMG read through the local GDAL warp path.
    """

    short_name: str
    version: str
    variable_names: tuple[str, ...]
    reader: str = "xarray"
    native_resolution: str = "monthly"
    scale: float = 1.0
    offset: float = 0.0
    # ``total`` means the provider stores a period accumulation (e.g. mm/month);
    # it is converted to a per-day rate so every variable keeps the same unit.
    period_statistic: str = "mean"
    valid_min: float | None = None
    valid_max: float | None = None
    quality_variable: str | None = None
    quality_value: int | None = None
    gdal_grid: str | None = None
    gdal_subdataset: str | None = None
    gdal_src_nodata: float | None = None
    gdal_output_type: str = "Float32"
    gdal_resampling: str = "bilinear"
    # Grid used when warping the coarse source onto the AOI.
    analysis_resolution: float = 0.01


@dataclass(frozen=True)
class VariableSpec:
    """Static metadata describing one supported environmental variable."""

    variable_id: str
    display_name: str
    dataset: str
    unit: str
    native_resolution: str
    value_label: str
    monthly_source: PeriodSource | None = None


@dataclass
class CollectionRequest:
    """Inputs shared by every variable collector."""

    aoi: dict[str, float]
    start_date: str
    end_date: str
    output_dir: Path
    temporal_resolution: str = "daily"
    spatial_strategy: str = "coarse"
    sampling_mode: str = "full_period"
    samples_per_month: int = 1
    sampling_seed: int | None = None
    sampling_missing: str = "keep"
    # Populated by the resolver: the exact ISO days selected by random sampling.
    sample_days: tuple[str, ...] | None = None
    should_cancel: Callable[[], bool] | None = None
    # Optional fine-grained reporting. ``progress`` is the job's callback and is
    # called as ``(stage, label, current, total, percent)``; ``percent`` lets a
    # collector report sub-chunk progress without the job guessing.
    progress: Callable[..., None] | None = None
    progress_label: str = ""
    chunk_index: int = 1
    chunk_total: int = 1

    def check_cancel(self) -> None:
        """Raise ``JobCanceled`` when the owning job has been canceled.

        Collectors call this at coarse checkpoints (per chunk, composite, or
        day) so an in-flight download or subprocess is never interrupted
        half-way.
        """
        raise_if_canceled(self.should_cancel)

    def report(self, current: int, total: int, detail: str = "") -> None:
        """Report progress inside a chunk (e.g. one MODIS composite)."""
        if self.progress is None:
            return
        fraction = (current / total) if total > 0 else 0.0
        percent = None
        if self.chunk_total > 0:
            percent = ((self.chunk_index - 1) + fraction) / self.chunk_total * 100.0
        label = f"{self.progress_label} {detail}".strip() or "collecting"
        self.progress("collecting", label, int(current), int(total), percent)


@dataclass
class VariableSeries:
    """Canonical output of a variable collector."""

    spec: VariableSpec
    records: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    mean_raster: Any = None
    raster_bounds: tuple[float, float, float, float] | None = None
    # Frequency of ``records``: "daily", "monthly" or "yearly".
    frequency: str = "daily"
    # How the series was produced: "daily", "provider_monthly", "sampled" or
    # "native_aggregated".
    collection_strategy: str = "daily"
    source_products: list[str] = field(default_factory=list)
    source_resolution: str | None = None
    sampling: dict[str, Any] = field(default_factory=dict)


def build_record(
    observation_date: str | date,
    value: float | None,
    spec: VariableSpec,
    *,
    valid_pixels: int = 0,
    total_pixels: int = 0,
    missing_reason: str | None = None,
    granules_used: list[str] | None = None,
    coverage_status: str | None = None,
    source_frequency: str | None = None,
) -> dict[str, Any]:
    """Create one canonical observation record."""
    date_text = observation_date.isoformat() if isinstance(observation_date, date) else str(observation_date)
    total = int(total_pixels)
    valid = int(valid_pixels)
    missing = value is None
    return {
        "observation_date": date_text,
        "value": None if value is None else float(value),
        "unit": spec.unit,
        "valid_pixel_count": valid,
        "total_pixel_count": total,
        "valid_data_percentage": 0.0 if total <= 0 else float((valid / total) * 100.0),
        "missing": missing,
        "missing_reason": missing_reason if missing else None,
        "granules_used": list(granules_used or []),
        "coverage_status": coverage_status or ("missing" if missing else "complete"),
        "source_frequency": source_frequency or spec.native_resolution,
    }


Collector = Callable[[CollectionRequest], VariableSeries]
