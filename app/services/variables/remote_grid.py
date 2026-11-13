"""Helpers for daily gridded products read remotely with earthaccess + xarray.

GPM, SMAP and MERRA-2 files are global. Downloading them just to average a small
AOI is wasteful, so these collectors open the granules remotely and subset the
relevant latitude/longitude window.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Iterable

import numpy as np
import xarray as xr

import earthaccess

from app.services.earthdata_auth import ensure_earthaccess_auth
from app.services.variables.base import CollectionRequest, VariableSeries, VariableSpec, build_record
from app.utils.logging import log

_LAT_NAMES = ("lat", "latitude", "LAT", "Latitude")
_LON_NAMES = ("lon", "longitude", "LON", "Longitude")
_TIME_NAMES = ("time", "Time")


def granule_date(granule: Any) -> str | None:
    """Best-effort extraction of a granule's observation date."""
    umm = granule.get("umm", {}) if hasattr(granule, "get") else {}
    temporal = umm.get("TemporalExtent", {}) if isinstance(umm, dict) else {}
    for key in ("RangeDateTime", "SingleDateTime"):
        payload = temporal.get(key) if isinstance(temporal, dict) else None
        if isinstance(payload, dict):
            for field in ("BeginningDateTime", "Date"):
                if payload.get(field):
                    return str(payload[field])[:10]

    for candidate in (
        umm.get("GranuleUR") if isinstance(umm, dict) else None,
        str(granule),
    ):
        if not candidate:
            continue
        match = re.search(r"(19|20)\d{2}[-_]?(\d{2})[-_]?(\d{2})", str(candidate))
        if match:
            digits = match.group(0).replace("_", "").replace("-", "")
            return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def date_chunks(start_date: str, end_date: str, max_days: int) -> list[tuple[str, str]]:
    """Split an inclusive date range into chunks of at most ``max_days``."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    chunks: list[tuple[str, str]] = []
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=max_days - 1), end)
        chunks.append((current.isoformat(), chunk_end.isoformat()))
        current = chunk_end + timedelta(days=1)
    return chunks


def _coordinate_name(dataset: xr.Dataset, candidates: Iterable[str]) -> str | None:
    for name in candidates:
        if name in dataset.coords or name in dataset.dims:
            return name
    lowered = {str(name).lower(): str(name) for name in list(dataset.coords) + list(dataset.dims)}
    for name in candidates:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _open_dataset(handle: Any) -> xr.Dataset:
    try:
        return xr.open_dataset(handle, engine="h5netcdf", decode_times=True, mask_and_scale=True)
    except Exception:
        return xr.open_dataset(handle, decode_times=True, mask_and_scale=True)


def _subset_values(dataset: xr.Dataset, variable: str, aoi: dict[str, float]) -> tuple[np.ndarray, str, str]:
    data = dataset[variable]
    lat_name = _coordinate_name(dataset, _LAT_NAMES) or _coordinate_name(data, _LAT_NAMES)
    lon_name = _coordinate_name(dataset, _LON_NAMES) or _coordinate_name(data, _LON_NAMES)
    if lat_name is None or lon_name is None:
        raise ValueError(f"Could not identify latitude/longitude coordinates for variable '{variable}'.")

    lat_values = np.asarray(dataset[lat_name].values, dtype=float)
    lon_values = np.asarray(dataset[lon_name].values, dtype=float)
    west, east = float(aoi["west"]), float(aoi["east"])
    if lon_values.size and float(np.nanmax(lon_values)) > 180.0 and west < 0.0:
        west %= 360.0
        east %= 360.0
        if east < west:
            east += 360.0

    lat_slice = slice(float(aoi["south"]), float(aoi["north"]))
    if lat_values.size > 1 and lat_values[0] > lat_values[-1]:
        lat_slice = slice(float(aoi["north"]), float(aoi["south"]))
    lon_slice = slice(west, east)
    if lon_values.size > 1 and lon_values[0] > lon_values[-1]:
        lon_slice = slice(east, west)

    subset = data.sel({lat_name: lat_slice, lon_name: lon_slice})
    time_name = _coordinate_name(subset, _TIME_NAMES)
    if time_name is not None and time_name in subset.dims:
        subset = subset.mean(dim=time_name, skipna=True)
    return np.asarray(subset.values, dtype=float), lat_name, lon_name


def _mean_for_granules(
    granules: Iterable[Any],
    aoi: dict[str, float],
    variable_names: Iterable[str],
    *,
    scale: float,
    offset: float,
    valid_min: float | None,
    valid_max: float | None,
    quality_variable: str | None,
    quality_value: int | None,
) -> tuple[float | None, int, int]:
    handles = earthaccess.open(list(granules))
    if not isinstance(handles, (list, tuple)):
        handles = [handles]

    values: list[np.ndarray] = []
    for handle in handles:
        dataset = _open_dataset(handle)
        try:
            variable = next((name for name in variable_names if name in dataset), None)
            if variable is None:
                continue
            data, _, _ = _subset_values(dataset, variable, aoi)

            if quality_variable and quality_variable in dataset:
                quality = dataset[quality_variable]
                lat_name = _coordinate_name(dataset, _LAT_NAMES)
                lon_name = _coordinate_name(dataset, _LON_NAMES)
                if lat_name and lon_name:
                    lat_values = np.asarray(dataset[lat_name].values, dtype=float)
                    lon_values = np.asarray(dataset[lon_name].values, dtype=float)
                    lat_slice = slice(float(aoi["south"]), float(aoi["north"]))
                    if lat_values.size > 1 and lat_values[0] > lat_values[-1]:
                        lat_slice = slice(float(aoi["north"]), float(aoi["south"]))
                    lon_slice = slice(float(aoi["west"]), float(aoi["east"]))
                    if lon_values.size > 1 and lon_values[0] > lon_values[-1]:
                        lon_slice = slice(float(aoi["east"]), float(aoi["west"]))
                    quality_subset = quality.sel({lat_name: lat_slice, lon_name: lon_slice})
                    time_name = _coordinate_name(quality_subset, _TIME_NAMES)
                    if time_name and time_name in quality_subset.dims:
                        quality_subset = quality_subset.mean(dim=time_name, skipna=True)
                    data = np.where(np.asarray(quality_subset.values) == quality_value, data, np.nan)

            values.append(np.asarray(data, dtype=float))
        finally:
            dataset.close()

    if not values:
        return None, 0, 0

    combined = np.concatenate([value.ravel() for value in values])
    total = int(combined.size)
    finite = np.isfinite(combined)
    if valid_min is not None:
        finite &= combined >= valid_min
    if valid_max is not None:
        finite &= combined <= valid_max
    if not np.any(finite):
        return None, 0, total
    mean_value = float(np.mean(combined[finite]) * scale + offset)
    return mean_value, int(np.sum(finite)), total


def collect_daily_grid(
    request: CollectionRequest,
    spec: VariableSpec,
    *,
    short_name: str,
    version: str,
    variable_names: Iterable[str],
    scale: float = 1.0,
    offset: float = 0.0,
    valid_min: float | None = None,
    valid_max: float | None = None,
    quality_variable: str | None = None,
    quality_value: int | None = None,
    search_chunk_days: int = 366,
) -> VariableSeries:
    """Collect a daily AOI mean from a remotely-readable global grid product."""
    # ``earthaccess.open`` needs an initialised store; search alone does not
    # create one, so a worker that only reads remote granules would fail with
    # "'NoneType' object has no attribute 'open'".
    ensure_earthaccess_auth()
    variable_names = tuple(variable_names)
    start = date.fromisoformat(request.start_date)
    end = date.fromisoformat(request.end_date)
    sample_days = set(request.sample_days) if request.sample_days else None
    warnings: list[str] = []
    records: list[dict[str, Any]] = []

    for chunk_start, chunk_end in date_chunks(request.start_date, request.end_date, search_chunk_days):
        log(f"variables: {spec.variable_id}: searching {short_name} {chunk_start} -> {chunk_end}")
        granules = earthaccess.search_data(
            short_name=short_name,
            version=version,
            count=-1,
            temporal=(chunk_start, chunk_end),
            bounding_box=(
                request.aoi["west"],
                request.aoi["south"],
                request.aoi["east"],
                request.aoi["north"],
            ),
        )
        grouped: dict[str, list[Any]] = {}
        for granule in granules:
            granule_day = granule_date(granule)
            if granule_day:
                grouped.setdefault(granule_day, []).append(granule)

        current = date.fromisoformat(chunk_start)
        chunk_end_date = date.fromisoformat(chunk_end)
        while current <= chunk_end_date:
            request.check_cancel()
            day = current.isoformat()
            current += timedelta(days=1)
            if sample_days is not None and day not in sample_days:
                continue
            day_granules = grouped.get(day)
            if not day_granules:
                records.append(
                    build_record(day, None, spec, missing_reason=f"No {short_name} granules found for this date.")
                )
                continue
            try:
                value, valid_pixels, total_pixels = _mean_for_granules(
                    day_granules,
                    request.aoi,
                    variable_names,
                    scale=scale,
                    offset=offset,
                    valid_min=valid_min,
                    valid_max=valid_max,
                    quality_variable=quality_variable,
                    quality_value=quality_value,
                )
            except Exception as exc:
                warnings.append(f"{day}: could not read {short_name} granule: {exc}")
                records.append(build_record(day, None, spec, missing_reason=str(exc)))
                continue

            if value is None:
                records.append(
                    build_record(
                        day,
                        None,
                        spec,
                        valid_pixels=valid_pixels,
                        total_pixels=total_pixels,
                        missing_reason="No valid pixels after QA filtering.",
                    )
                )
            else:
                records.append(
                    build_record(
                        day,
                        value,
                        spec,
                        valid_pixels=valid_pixels,
                        total_pixels=total_pixels,
                    )
                )

    return VariableSeries(spec=spec, records=records, warnings=warnings)
