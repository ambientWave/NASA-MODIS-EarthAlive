"""Generate ``app/catalog/products.json`` from the AppEEARS product snapshot.

The snapshot (``data/appeears_products.json``) is a verbatim copy of
``GET https://appeears.earthdatacloud.nasa.gov/api/product``. Everything in
the generated catalog is derived from it; nothing here is hand-maintained.

Usage::

    python tools/build_catalog.py            # regenerate
    python tools/build_catalog.py --check    # fail if products.json is stale
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PROJECT_ROOT / "data" / "appeears_products.json"
DEFAULT_TARGET = PROJECT_ROOT / "app" / "catalog" / "products.json"

# AppEEARS ``TemporalGranularity`` -> canonical granularity.
GRANULARITY = {
    "Daily": "daily",
    "4 day": "4_day",
    "8 day": "8_day",
    "16 day": "16_day",
    "Monthly": "monthly",
    "Yearly": "yearly",
    "Static": "static",
    "ISS-dependent": "irregular",
}

# Nominal number of days one source record represents. ``None`` means the
# cadence is event-driven and cannot be used to plan a regular grid.
STEP_DAYS = {
    "daily": 1.0,
    "4_day": 4.0,
    "8_day": 8.0,
    "16_day": 16.0,
    "monthly": 30.44,
    "yearly": 365.25,
    "static": None,
    "irregular": None,
}

# Provider publication latency, used to resolve the open-ended ``"Present"``
# extent at request time instead of freezing today's date into the snapshot.
PROVIDER_LAG_DAYS = {
    "LP DAAC": 3,
    "NSIDC DAAC": 3,
    "GES DISC": 5,
    "ORNL": 30,
    "USGS": 10,
    "OB.DAAC": 3,
}

METRES_PER_DEGREE = 111_320.0


def parse_resolution_m(raw: str) -> float | None:
    """Normalise AppEEARS resolution strings to metres (coarsest if ambiguous).

    ``"1000m"`` -> 1000, ``"0.1deg"`` -> 11132, ``"500/1000m"`` -> 1000.
    """
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if "/" in text:
        # e.g. "500/1000m": keep the coarser of the pair.
        text = text.split("/")[-1]
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*(m|km|deg|°)?", text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2) or "m"
    if unit == "km":
        return value * 1000.0
    if unit in {"deg", "°"}:
        return round(value * METRES_PER_DEGREE, 2)
    return value


def normalise_granularity(raw: str) -> str:
    return GRANULARITY.get(str(raw or "").strip(), "irregular")


def _extent(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() == "present":
        return None
    return text[:10]


def build(source: Path = DEFAULT_SOURCE) -> dict:
    raw = source.read_bytes()
    records = json.loads(raw.decode("utf-8"))
    products: dict[str, dict] = {}
    for item in records:
        name = str(item.get("Product") or "").strip()
        if not name:
            continue
        granularity = normalise_granularity(item.get("TemporalGranularity"))
        products[name] = {
            "product": name,
            "product_id": item.get("ProductAndVersion") or f"{name}.{item.get('Version')}",
            "version": str(item.get("Version") or ""),
            "platform": item.get("Platform"),
            "description": item.get("Description"),
            "raster_type": item.get("RasterType"),
            "resolution_raw": item.get("Resolution"),
            "resolution_m": parse_resolution_m(item.get("Resolution")),
            "granularity_raw": item.get("TemporalGranularity"),
            "granularity": granularity,
            "step_days": STEP_DAYS[granularity],
            "extent_start": _extent(item.get("TemporalExtentStart")),
            "extent_end": _extent(item.get("TemporalExtentEnd")),
            "source": item.get("Source"),
            "provider_lag_days": PROVIDER_LAG_DAYS.get(str(item.get("Source") or ""), 5),
            "available": bool(item.get("Available")) and not bool(item.get("Deleted")),
            "doi": item.get("DOI"),
        }
    return {
        "source": "NASA AppEEARS GET /api/product",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "generated_on": date.today().isoformat(),
        "count": len(products),
        "products": dict(sorted(products.items())),
    }


def render(catalog: dict) -> str:
    return json.dumps(catalog, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--check", action="store_true", help="verify the catalog is current")
    args = parser.parse_args(argv)

    catalog = build(args.source)
    text = render(catalog)

    if args.check:
        current = args.target.read_text(encoding="utf-8") if args.target.exists() else ""
        # ``generated_on`` legitimately changes without the source changing.
        if _strip_generated_on(current) != _strip_generated_on(text):
            print(f"{args.target} is stale; run: python tools/build_catalog.py", file=sys.stderr)
            return 1
        print(f"{args.target} is up to date ({catalog['count']} products)")
        return 0

    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_text(text, encoding="utf-8")
    print(f"wrote {catalog['count']} products to {args.target}")
    return 0


def _strip_generated_on(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    payload.pop("generated_on", None)
    return json.dumps(payload, sort_keys=True)


if __name__ == "__main__":
    raise SystemExit(main())
