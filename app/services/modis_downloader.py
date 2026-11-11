from __future__ import annotations

from pathlib import Path
from typing import Any

import earthaccess

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DATA_DIR = PROJECT_ROOT / "data" / "processed"
EGYPT_BBOX = {
    "west": 24.7,
    "south": 22.0,
    "east": 36.9,
    "north": 31.7,
}


def discover_mod11a1_granules(
    date_str: str = "2024-01-01",
    bbox: dict[str, float] | None = None,
) -> list[Any]:
    """Search NASA Earthdata for MOD11A1 granules covering the selected region."""
    bbox = bbox or EGYPT_BBOX
    date_start = date_str
    date_end = date_str

    search_args = {
        "short_name": "MOD11A1",
        "version": "061",
        "count": -1,
        "temporal": (date_start, date_end),
        "bounding_box": (
            bbox["west"],
            bbox["south"],
            bbox["east"],
            bbox["north"],
        ),
    }

    products = earthaccess.search_data(**search_args)
    return list(products)


def download_mod11a1_granules(
    granules: list[Any],
    target_dir: Path | str | None = None,
    show_progress: bool = False,
) -> list[Path]:
    """Download a list of granules while preserving original HDF files."""
    target_dir = Path(target_dir) if target_dir is not None else RAW_DATA_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    downloaded_paths = earthaccess.download(
        granules,
        local_path=str(target_dir),
        show_progress=show_progress,
        threads=4,
    )
    return [Path(path) for path in downloaded_paths]


def get_tile_name(granule: Any) -> str:
    """Return a human-readable granule identifier for reporting."""
    umm = getattr(granule, "get", lambda *_args, **_kwargs: {})("umm", {})
    granule_ur = umm.get("GranuleUR")
    if granule_ur:
        return str(granule_ur)

    data_links = getattr(granule, "get", lambda *_args, **_kwargs: [])("data", [])
    if data_links:
        return str(data_links[0]).split("/")[-1]

    return "unknown-granule"
