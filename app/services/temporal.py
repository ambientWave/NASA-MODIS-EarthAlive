"""Temporal aggregation and random-sampling helpers shared by every job.

Collection always returns records at the frequency the caller asked for:

* ``daily`` — untouched collector output.
* ``monthly`` — either one provider-produced monthly value per month
  (``source_frequency="monthly"``) or the mean of the daily records in that
  month.
* ``yearly`` — the day-weighted mean of the monthly values.

Random sampling is a separate axis: it selects real observations instead of
averaging, and is used for variables that have no provider monthly product.
"""

from __future__ import annotations

import random
from calendar import monthrange
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.variables.base import VariableSpec

RESOLUTIONS = ("daily", "monthly", "yearly")
SAMPLING_MODES = ("full_period", "random_sample")


def days_in_month(year: int, month: int) -> int:
    return monthrange(year, month)[1]


def _parse_day(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def period_key(observation_date: str, resolution: str) -> str:
    if resolution == "monthly":
        return observation_date[:7]
    return observation_date[:4]


def period_label(key: str, resolution: str) -> str:
    return f"{key}-01" if resolution == "monthly" else f"{key}-01-01"


def month_key(value: str | date) -> str:
    return _parse_day(value).isoformat()[:7]


def monthly_periods(start_date: str, end_date: str) -> list[str]:
    """Every ``YYYY-MM`` touched by a closed date range."""
    current = date.fromisoformat(start_date).replace(day=1)
    end = date.fromisoformat(end_date)
    periods: list[str] = []
    while current <= end:
        periods.append(current.strftime("%Y-%m"))
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return periods


def _weight(record: dict[str, Any], resolution: str) -> float:
    """Month length weights provider-monthly values when forming a year.

    Daily and sampled records already represent one equal-weight day, so they
    contribute weight 1. Provider monthly values need the days-in-month weight
    so a 31-day January is not treated like a 28-day February.
    """
    if resolution != "yearly":
        return 1.0
    if record.get("source_frequency") != "monthly":
        return 1.0
    try:
        day = _parse_day(str(record["observation_date"]))
    except (KeyError, ValueError):
        return 1.0
    return float(days_in_month(day.year, day.month))


def _already_at_resolution(records: list[dict[str, Any]], resolution: str) -> bool:
    if resolution == "daily":
        return True
    if not records:
        return False
    if resolution == "monthly":
        return all(record.get("source_frequency") == "monthly" for record in records)
    return all(record.get("source_frequency") == "yearly" for record in records)


def aggregate_records(
    records: Iterable[dict[str, Any]],
    spec: "VariableSpec",
    resolution: str,
) -> list[dict[str, Any]]:
    """Roll records up to ``monthly``/``yearly`` with a weighted mean.

    The mean is weighted by the number of source days each record represents.
    Provider monthly values carry a days-in-month weight when forming a year;
    daily values carry weight 1, which makes the daily→monthly mean identical
    to the existing unweighted daily mean.
    """
    records = list(records)
    if resolution not in RESOLUTIONS:
        raise ValueError(f"Unsupported temporal resolution: {resolution}")
    if resolution == "daily" or _already_at_resolution(records, resolution):
        return records

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        day = str(record.get("observation_date") or "")
        if not day:
            continue
        grouped.setdefault(period_key(day, resolution), []).append(record)

    aggregated: list[dict[str, Any]] = []
    from app.services.variables.base import build_record

    for key in sorted(grouped):
        batch = grouped[key]
        weighted_sum = 0.0
        weight_total = 0.0
        valid_pixels = 0
        total_pixels = 0
        for record in batch:
            valid_pixels += int(record.get("valid_pixel_count") or 0)
            total_pixels += int(record.get("total_pixel_count") or 0)
            value = record.get("value")
            if value is None or record.get("missing"):
                continue
            weight = _weight(record, resolution)
            weighted_sum += float(value) * weight
            weight_total += weight

        value = weighted_sum / weight_total if weight_total > 0 else None
        aggregated.append(
            build_record(
                period_label(key, resolution),
                value,
                spec,
                valid_pixels=valid_pixels,
                total_pixels=total_pixels,
                missing_reason=None if value is not None else "No valid observations in this period.",
                coverage_status="aggregated" if value is not None else "missing",
                source_frequency=resolution,
            )
        )
    return aggregated


def resolve_seed(seed: int | None, *, fallback_key: str = "") -> int:
    """Stable seed so re-runs and fan-out parts agree without coordination."""
    if seed is not None:
        return int(seed)
    import hashlib

    digest = hashlib.sha1(fallback_key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def sample_day_plan(
    start_date: str,
    end_date: str,
    *,
    samples_per_month: int,
    seed: int,
    key: str = "",
    extra_candidates: int = 0,
) -> dict[str, list[str]]:
    """Pick uniformly random days per calendar month.

    Returns ``{"YYYY-MM": ["YYYY-MM-DD", ...]}`` in selection order. The first
    ``samples_per_month`` entries are the intended sample; extra candidates are
    only used by the ``redraw`` missing-day policy.
    """
    wanted = max(1, int(samples_per_month)) + max(0, int(extra_candidates))
    plan: dict[str, list[str]] = {}
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    for period in monthly_periods(start_date, end_date):
        year, month = (int(part) for part in period.split("-"))
        first = date(year, month, 1)
        last = date(year, month, days_in_month(year, month))
        first = max(first, start)
        last = min(last, end)
        available = (last - first).days + 1
        if available <= 0:
            continue
        rng = random.Random(f"{seed}:{key}:{period}")
        count = min(wanted, available)
        offsets = sorted(rng.sample(range(available), count))
        plan[period] = [(first + timedelta(days=offset)).isoformat() for offset in offsets]
    return plan


def planned_days(plan: dict[str, list[str]]) -> tuple[str, ...]:
    return tuple(day for days in plan.values() for day in days)


def period_chunks(
    start_date: str,
    end_date: str,
    *,
    max_years: int = 5,
) -> list[tuple[str, str]]:
    """Split a range on calendar-year boundaries so no month is split.

    Chunk boundaries never fall inside a month, which would otherwise produce
    two partial-month averages for provider monthly products.
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    chunks: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(date(cursor.year + max_years - 1, 12, 31), end)
        chunks.append((cursor.isoformat(), chunk_end.isoformat()))
        cursor = date(chunk_end.year + 1, 1, 1)
    return chunks


def select_sampled_records(
    records: Iterable[dict[str, Any]],
    plan: dict[str, list[str]],
    *,
    samples_per_month: int,
    missing_policy: str = "keep",
    spec: Any = None,
) -> list[dict[str, Any]]:
    """Keep the sampled records, applying the ``keep``/``redraw`` policy.

    ``records`` may contain more days than were requested (extra candidates).
    For each month the first ``samples_per_month`` valid candidates win; if
    ``missing_policy == "keep"`` a missing intended day is kept as an explicit
    missing observation instead of being replaced.
    """
    by_day = {str(record.get("observation_date")): record for record in records}
    selected: list[dict[str, Any]] = []
    for period in sorted(plan):
        days = plan[period]
        intended = days[:samples_per_month]
        extra = days[samples_per_month:]
        for day in intended:
            record = by_day.get(day)
            if record is not None and not record.get("missing"):
                selected.append(record)
                continue
            if missing_policy == "redraw":
                replacement = next(
                    (by_day[candidate] for candidate in extra if candidate in by_day and not by_day[candidate].get("missing")),
                    None,
                )
                if replacement is not None:
                    extra.remove(replacement["observation_date"])
                    selected.append(replacement)
                    continue
            if record is not None:
                selected.append(record)
            elif spec is not None:
                from app.services.variables.base import build_record

                selected.append(
                    build_record(
                        day,
                        None,
                        spec,
                        missing_reason="The randomly sampled day has no observation.",
                        coverage_status="missing",
                    )
                )
            else:
                selected.append(
                    {
                        "observation_date": day,
                        "value": None,
                        "missing": True,
                        "missing_reason": "The randomly sampled day has no observation.",
                    }
                )
    return selected
