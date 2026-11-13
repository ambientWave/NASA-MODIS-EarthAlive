"""Shared contracts for environmental-variable time series collectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.jobs.errors import raise_if_canceled


@dataclass(frozen=True)
class VariableSpec:
    """Static metadata describing one supported environmental variable."""

    variable_id: str
    display_name: str
    dataset: str
    unit: str
    native_resolution: str
    value_label: str


@dataclass
class CollectionRequest:
    """Inputs shared by every variable collector."""

    aoi: dict[str, float]
    start_date: str
    end_date: str
    output_dir: Path
    temporal_resolution: str = "daily"
    should_cancel: Callable[[], bool] | None = None

    def check_cancel(self) -> None:
        """Raise ``JobCanceled`` when the owning job has been canceled.

        Collectors call this at coarse checkpoints (per chunk, composite, or
        day) so an in-flight download or subprocess is never interrupted
        half-way.
        """
        raise_if_canceled(self.should_cancel)


@dataclass
class VariableSeries:
    """Canonical output of a variable collector."""

    spec: VariableSpec
    records: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    mean_raster: Any = None
    raster_bounds: tuple[float, float, float, float] | None = None


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
