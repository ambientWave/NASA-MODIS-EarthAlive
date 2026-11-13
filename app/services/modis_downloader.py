"""MODIS MOD11A1 Downloader using NASA AppEEARS API.

Replaces raw HDF4 granule downloads via CMR/earthaccess with cloud-native
Analysis Ready Data (ARD) extraction via the AppEEARS REST API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.services.appeears_client import AppEEARSClient
from app.utils.logging import log

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DATA_DIR = PROJECT_ROOT / "data" / "processed"

EGYPT_BBOX = {
    "west": 24.7,
    "south": 22.0,
    "east": 36.9,
    "north": 31.7,
}

_client: AppEEARSClient | None = None


def get_appeears_client() -> AppEEARSClient:
    """Return a shared singleton instance of AppEEARSClient."""
    global _client
    if _client is None:
        _client = AppEEARSClient()
    return _client


def download_mod11a1_appeears(
    date_str: str = "2024-01-01",
    end_date_str: str | None = None,
    bbox: dict[str, float] | None = None,
    target_dir: Path | str | None = None,
    layers: list[str] | None = None,
    wait: bool = True,
    timeout_seconds: int = 1800,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, str], None] | None = None,
) -> list[Path]:
    """Submit an AppEEARS task for MOD11A1 and download pre-clipped, EPSG:4326 GeoTIFFs.

    Layers default to LST_Day_1km and QC_Day.
    """
    bbox = bbox or EGYPT_BBOX
    target_dir = Path(target_dir) if target_dir is not None else RAW_DATA_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    end_date_str = end_date_str or date_str
    layer_names = layers or ["LST_Day_1km", "QC_Day"]

    task_layers = [("MOD11A1.061", layer) for layer in layer_names]
    task_name = f"mod11a1_{date_str.replace('-', '')}"

    client = get_appeears_client()
    task_id = client.submit_area_task(
        task_name=task_name,
        start_date=date_str,
        end_date=end_date_str,
        layers=task_layers,
        bbox=bbox,
        file_type="geotiff",
        projection="geographic",
    )

    if not wait:
        log(f"appeears: task {task_id} submitted (wait=False)")
        return []

    client.wait_for_task(
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        should_cancel=should_cancel,
        on_progress=on_progress,
    )

    downloaded = client.download_bundle(
        task_id=task_id,
        target_dir=target_dir,
        file_patterns=tuple(layer_names),
    )
    return downloaded


def discover_mod11a1_granules(
    date_str: str = "2024-01-01",
    bbox: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """AppEEARS-backed metadata query for MOD11A1 layers."""
    bbox = bbox or EGYPT_BBOX
    client = get_appeears_client()
    layers = client.list_layers("MOD11A1.061")

    # Present layer metadata in a format compatible with pipeline inspection
    results: list[dict[str, Any]] = []
    for layer_name, info in layers.items():
        if layer_name in ("LST_Day_1km", "QC_Day"):
            results.append(
                {
                    "product": "MOD11A1.061",
                    "layer": layer_name,
                    "date": date_str,
                    "bbox": bbox,
                    "info": info,
                    "umm": {
                        "GranuleUR": f"MOD11A1.061_{layer_name}_{date_str}",
                        "DataGranule": {"ProducerGranuleId": f"MOD11A1.061_{layer_name}"},
                    },
                }
            )
    return results


def download_mod11a1_granules(
    granules: list[Any],
    target_dir: Path | str | None = None,
    show_progress: bool = False,
) -> list[Path]:
    """Download analysis-ready MOD11A1 GeoTIFFs using AppEEARS."""
    target_dir = Path(target_dir) if target_dir is not None else RAW_DATA_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    if not granules:
        return []

    # Extract date and bbox from first granule if available
    first = granules[0]
    date_str = first.get("date", "2024-01-01") if isinstance(first, dict) else "2024-01-01"
    bbox = first.get("bbox") if isinstance(first, dict) else None

    return download_mod11a1_appeears(
        date_str=date_str,
        bbox=bbox,
        target_dir=target_dir,
    )


def get_tile_name(granule: Any) -> str:
    """Return a human-readable identifier for reporting."""
    if isinstance(granule, dict):
        granule_ur = granule.get("umm", {}).get("GranuleUR")
        if granule_ur:
            return str(granule_ur)
        layer = granule.get("layer")
        if layer:
            return f"MOD11A1.061_{layer}"
    return "appeears_mod11a1_ard"
