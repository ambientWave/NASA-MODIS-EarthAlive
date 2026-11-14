"""Turn a frontend request into one concrete AppEEARS plan.

The selector is the single place that decides:

* which product is read (structural, not a user field),
* which date range is actually available (clamped, never silent),
* which temporal granularity is used to satisfy a monthly/yearly request,
* whether random sampling can be honoured as days or only as composite windows.

Everything it decides is returned as data so the API can declare it in
``metadata`` and ``warnings`` instead of substituting silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from app.catalog import Product, get_product
from app.catalog.overlay import VariableOverlay, implemented_products, overlay_for

GRANULARITY_RANK = {
    "static": 0,
    "daily": 1,
    "irregular": 1,
    "4_day": 2,
    "8_day": 3,
    "16_day": 4,
    "monthly": 5,
    "yearly": 6,
}

PERIOD_CEILING = {"daily": 1, "monthly": 5, "yearly": 6}

GRANULARITY_LABEL = {
    "daily": "daily",
    "4_day": "4-day",
    "8_day": "8-day",
    "16_day": "16-day",
    "monthly": "monthly",
    "yearly": "yearly",
    "irregular": "irregular",
    "static": "static",
}

# Observation-level products whose collector honours ``sample_days``.
DAY_LEVEL_COLLECTORS = {"daily", "irregular"}


@dataclass(frozen=True)
class DateRange:
    requested_start: date
    requested_end: date
    start: date
    end: date
    clamped: bool
    truncated_partial_periods: bool

    def metadata(self) -> dict:
        return {
            "requested": {"start": self.requested_start.isoformat(), "end": self.requested_end.isoformat()},
            "effective": {"start": self.start.isoformat(), "end": self.end.isoformat()},
            "clamped": self.clamped,
            "truncated_partial_periods": self.truncated_partial_periods,
        }


@dataclass(frozen=True)
class Selection:
    variable: str
    label: str
    period: str
    spatial_strategy: str
    product: Product | None
    role: str | None
    basis: str
    collection: str
    sampling_applied: str
    sampling_unit: str
    date_range: DateRange
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def step_days(self) -> float | None:
        return self.product.step_days if self.product else None

    @property
    def product_id(self) -> str | None:
        return self.product.product_id if self.product else None

    @property
    def product_name(self) -> str | None:
        return self.product.name if self.product else None

    def metadata(self) -> dict:
        product = self.product
        return {
            "date_range": self.date_range.metadata(),
            "resolution": {
                "requested": self.period,
                "applied": self.period,
                "basis": self.basis,
                "product": self.product_name,
                "product_id": self.product_id,
                "source_granularity": product.granularity if product else None,
                "source_granularity_label": GRANULARITY_LABEL.get(product.granularity) if product else None,
                "source_resolution": product.resolution_raw if product else None,
                "role": self.role,
            },
            "collection": self.collection,
            "sampling_applied": self.sampling_applied,
            "sampling_unit": self.sampling_unit,
        }


def _max_rank_for_samples(samples_per_month: int) -> int:
    """Cap granularity by how many distinct observations a month must yield.

    1-2 samples/month can be served by a 16-day composite, 3-4 by an 8-day
    composite, and anything above that needs daily observations.
    """
    if samples_per_month <= 2:
        return 4
    if samples_per_month <= 4:
        return 3
    return 1


def _rank(product: Product) -> int:
    return GRANULARITY_RANK.get(product.granularity, 1)


def _whole_periods(start: date, end: date, period: str) -> tuple[date, date, bool]:
    """Trim to whole output periods so a partial edge never skews a trend."""
    if period == "monthly":
        effective_start = date(start.year, start.month, 1)
        if effective_start != start:
            effective_start = _next_month(effective_start)
        effective_end = _month_end(end)
        if effective_end != end:
            effective_end = _month_end(effective_end - timedelta(days=1))
    elif period == "yearly":
        effective_start = date(start.year, 1, 1)
        if effective_start != start:
            effective_start = date(start.year + 1, 1, 1)
        effective_end = date(end.year, 12, 31)
        if effective_end != end:
            effective_end = date(end.year - 1, 12, 31)
    else:
        return start, end, False
    truncated = effective_start != start or effective_end != end
    return effective_start, effective_end, truncated


def _month_end(day: date) -> date:
    return _next_month(date(day.year, day.month, 1)) - timedelta(days=1)


def _next_month(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def _product_extent(product: Product, today: date) -> tuple[date, date]:
    start = product.extent_start or date(1900, 1, 1)
    return start, product.extent_end_resolved(today)


def _select_catalog_product(
    overlay: VariableOverlay,
    period: str,
    strategy: str,
    sampling_mode: str,
    samples_per_month: int,
    requested: tuple[date, date],
) -> tuple[Product, str]:
    ceiling = PERIOD_CEILING[period]
    implemented = implemented_products(overlay.variable_id)

    pool: list[tuple[Product, str, int]] = []
    for name, (role, priority) in overlay.products.items():
        if name not in implemented:
            continue
        product = get_product(name)
        if not product.available:
            continue
        if _rank(product) > ceiling:
            continue
        pool.append((product, role, priority))

    # Extent: keep products that overlap the requested range at all; clamping
    # handles the edges. If nothing overlaps, the request cannot be served.
    overlapping = [
        item
        for item in pool
        if _overlaps(item[0], requested)
    ]
    if overlapping:
        pool = overlapping

    if sampling_mode == "random_sample":
        max_rank = _max_rank_for_samples(samples_per_month)
        pool = [item for item in pool if _rank(item[0]) <= max_rank]

    if not pool:
        raise ValueError(
            f"No {overlay.label} product can serve {period} + {strategy} "
            f"for {requested[0].isoformat()}..{requested[1].isoformat()}."
        )

    resolutions = [item[0].resolution_m for item in pool if item[0].resolution_m is not None]
    if resolutions:
        band = max(resolutions) if strategy == "coarse" else min(resolutions)
        banded = [item for item in pool if item[0].resolution_m == band]
        pool = banded or pool

    # Coarsest granularity that is still valid, then priority, then name so the
    # result is deterministic.
    product, role, _priority = max(
        pool,
        key=lambda item: (_rank(item[0]), item[2], item[0].name),
    )
    return product, role


def _overlaps(product: Product, requested: tuple[date, date], today: date | None = None) -> bool:
    start, end = _product_extent(product, today or date.today())
    return requested[0] <= end and requested[1] >= start


def resolve_selection(
    variable: str,
    start: date,
    end: date,
    period: str,
    strategy: str = "coarse",
    sampling_mode: str = "full_period",
    samples_per_month: int = 1,
    *,
    today: date | None = None,
) -> Selection:
    """Resolve one request into a product, effective range and declared plan."""
    if start > end:
        raise ValueError("start_date must not be after end_date.")
    if period not in PERIOD_CEILING:
        raise ValueError(f"Unsupported temporal resolution: '{period}'.")
    if strategy not in {"coarse", "native"}:
        raise ValueError(f"Unsupported spatial strategy: '{strategy}'.")
    if sampling_mode not in {"full_period", "random_sample"}:
        raise ValueError(f"Unsupported sampling mode: '{sampling_mode}'.")

    overlay = overlay_for(variable)
    today = today or date.today()
    warnings: list[str] = []

    product: Product | None = None
    role: str | None = None
    if overlay.catalog_selection:
        product, role = _select_catalog_product(
            overlay, period, strategy, sampling_mode, samples_per_month, (start, end)
        )

    if product is not None:
        extent_start, extent_end = _product_extent(product, today)
        extent_label = product.name
    elif overlay.clamp_product is not None:
        clamp = get_product(overlay.clamp_product)
        extent_start, extent_end = _product_extent(clamp, today)
        extent_label = clamp.name
    elif overlay.legacy_extent is not None:
        legacy_start, legacy_end = overlay.legacy_extent
        extent_start = date.fromisoformat(legacy_start) if legacy_start else date(1900, 1, 1)
        extent_end = date.fromisoformat(legacy_end) if legacy_end else today - timedelta(days=5)
        extent_label = overlay.label
    else:
        extent_start, extent_end = date(1900, 1, 1), today - timedelta(days=5)
        extent_label = overlay.label

    clamped_start = max(start, extent_start)
    clamped_end = min(end, extent_end)
    if clamped_end < clamped_start:
        raise ValueError(
            f"{extent_label} covers {extent_start.isoformat()}..{extent_end.isoformat()}; "
            f"the requested range {start.isoformat()}..{end.isoformat()} has no overlap."
        )

    effective_start, effective_end, truncated = _whole_periods(clamped_start, clamped_end, period)
    if effective_end < effective_start:
        raise ValueError(
            f"The overlap with {extent_label} is shorter than one {period} period."
        )

    clamped = clamped_start != start or clamped_end != end
    if clamped_start != start:
        warnings.append(
            f"Requested start {start.isoformat()} precedes {extent_label} availability "
            f"({extent_start.isoformat()}); clipped to {clamped_start.isoformat()}."
        )
    if clamped_end != end:
        warnings.append(
            f"Requested end {end.isoformat()} is after {extent_label} availability "
            f"({extent_end.isoformat()}); clipped to {clamped_end.isoformat()}."
        )
    if truncated and period != "daily":
        warnings.append(
            f"Partial {period} periods at the edges were excluded so the trend is not "
            f"skewed; effective range is {effective_start.isoformat()}..{effective_end.isoformat()}."
        )

    source_rank = _rank(product) if product else None
    basis = "provider" if period == "daily" or source_rank == PERIOD_CEILING[period] else "derived"

    if period == "daily":
        collection = "daily"
    elif product is not None and product.name == overlay.aggregate_product:
        collection = "composite"
    elif product is not None and product.granularity in {"monthly", "yearly"}:
        collection = "provider_aggregate"
    else:
        collection = "native_aggregated"

    if sampling_mode == "full_period":
        sampling_applied = "full_period"
        sampling_unit = "period"
    elif collection in {"composite", "provider_aggregate"}:
        sampling_applied = "composite_window"
        sampling_unit = f"{GRANULARITY_LABEL.get(product.granularity) if product else period}_window"
    elif product is not None and product.granularity in DAY_LEVEL_COLLECTORS:
        sampling_applied = "random_sample"
        sampling_unit = "day"
    else:
        sampling_applied = "random_sample"
        sampling_unit = "day"

    if basis == "derived" and product is not None:
        warnings.append(
            f"{period.capitalize()} {overlay.label} is derived from {product.name} "
            f"{GRANULARITY_LABEL.get(product.granularity, product.granularity)} "
            f"{'composites' if product.granularity not in {'monthly', 'yearly'} else 'values'}; "
            f"NASA publishes no {period} product for this variable."
        )
    if sampling_applied == "composite_window":
        warnings.append(
            "random_sample was applied at provider-composite granularity: one "
            f"{GRANULARITY_LABEL.get(product.granularity) if product else period} window per month."
        )
    elif sampling_mode == "random_sample" and sampling_applied == "random_sample":
        warnings.append(
            f"random_sample selects individual observation days for {overlay.label}."
        )
    if strategy == "native" and product is not None and product.name in {"MOD11A1", "MOD11A2"}:
        warnings.append(
            "Fine-resolution LST products are not wired yet; native resolution is "
            f"served at {product.resolution_raw} by {product.name}."
        )

    return Selection(
        variable=variable,
        label=overlay.label,
        period=period,
        spatial_strategy=strategy,
        product=product,
        role=role,
        basis=basis,
        collection=collection,
        sampling_applied=sampling_applied,
        sampling_unit=sampling_unit,
        date_range=DateRange(
            requested_start=start,
            requested_end=end,
            start=effective_start,
            end=effective_end,
            clamped=clamped,
            truncated_partial_periods=truncated,
        ),
        warnings=tuple(warnings),
    )
