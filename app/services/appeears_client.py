"""NASA AppEEARS REST API client for Analysis-Ready Data (ARD) extraction.

AppEEARS (Application for Extracting and Exploring Analysis Ready Samples)
processes geospatial data server-side at NASA LP DAAC, handling spatial
subsetting, layer extraction, mosaicing, and reprojection to EPSG:4326.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests
from requests.auth import HTTPBasicAuth

from app.core.config import get_settings
from app.utils.logging import log

APPEEARS_BASE_URL = "https://appeears.earthdatacloud.nasa.gov/api"


class AppEEARSClient:
    """Client for interacting with the NASA AppEEARS REST API."""

    def __init__(self, base_url: str = APPEEARS_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self._token: str | None = None
        self._token_expires: float = 0.0

    def login(self, force_refresh: bool = False) -> str:
        """Authenticate with NASA Earthdata credentials and retrieve Bearer token.

        Tokens expire after ~48 hours. Cached tokens are reused until 5 minutes
        prior to expiration.
        """
        now = time.time()
        if not force_refresh and self._token and now < (self._token_expires - 300):
            return self._token

        settings = get_settings()
        username = settings.earthdata_username or os.getenv("EARTHDATA_USERNAME")
        password = settings.earthdata_password or os.getenv("EARTHDATA_PASSWORD")

        if not username or not password:
            raise ConnectionError(
                "AppEEARS authentication requires EARTHDATA_USERNAME and EARTHDATA_PASSWORD "
                "to be set in .env or the process environment."
            )

        login_url = f"{self.base_url}/login"
        log("appeears: authenticating with NASA Earthdata Login...")
        response = self.session.post(
            login_url,
            auth=HTTPBasicAuth(username, password),
            headers={"Accept": "application/json"},
            timeout=30,
        )

        if response.status_code != 200:
            log(f"appeears: login failed with status {response.status_code}: {response.text}")
            raise ConnectionError(
                f"NASA AppEEARS authentication failed ({response.status_code}): {response.text}"
            )

        data = response.json()
        token = data.get("token")
        if not token:
            raise ConnectionError("AppEEARS login response missing 'token'.")

        self._token = str(token)

        exp_str = data.get("expiration")
        if exp_str:
            try:
                exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                self._token_expires = exp_dt.timestamp()
            except Exception:
                self._token_expires = now + (47 * 3600)
        else:
            self._token_expires = now + (47 * 3600)

        log("appeears: authentication successful (token cached).")
        return self._token

    def logout(self) -> None:
        """Revoke active AppEEARS token."""
        if not self._token:
            return
        logout_url = f"{self.base_url}/logout"
        try:
            self.session.post(
                logout_url,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=10,
            )
        except Exception as exc:
            log(f"appeears: error during logout: {exc}")
        finally:
            self._token = None
            self._token_expires = 0.0

    def _auth_headers(self) -> dict[str, str]:
        token = self.login()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def _request_with_retry(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> requests.Response:
        """Send an authenticated request, retrying once on 401/403 with a refreshed token."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = kwargs.pop("headers", {})
        headers.update(self._auth_headers())

        resp = self.session.request(method, url, headers=headers, **kwargs)
        if resp.status_code in (401, 403):
            log(f"appeears: received {resp.status_code}, refreshing token and retrying...")
            self.login(force_refresh=True)
            headers.update(self._auth_headers())
            resp = self.session.request(method, url, headers=headers, **kwargs)

        return resp

    @staticmethod
    def _format_date(date_str: str) -> str:
        """Convert 'YYYY-MM-DD' or datetime-compatible string to AppEEARS 'MM-DD-YYYY'."""
        clean_date = date_str.strip()[:10]
        try:
            dt = datetime.strptime(clean_date, "%Y-%m-%d")
            return dt.strftime("%m-%d-%Y")
        except ValueError:
            # Check if already MM-DD-YYYY
            try:
                datetime.strptime(clean_date, "%m-%d-%Y")
                return clean_date
            except ValueError:
                return clean_date

    def submit_area_task(
        self,
        task_name: str,
        start_date: str,
        end_date: str,
        layers: list[tuple[str, str]],
        bbox: dict[str, float] | None = None,
        geojson: dict[str, Any] | None = None,
        file_type: str = "geotiff",
        projection: str = "geographic",
        date_ranges: list[tuple[str, str]] | None = None,
    ) -> str:
        """Submit an area sample extraction task to AppEEARS.

        Args:
            task_name: User-defined name for the task.
            start_date: Start date (YYYY-MM-DD or MM-DD-YYYY).
            end_date: End date (YYYY-MM-DD or MM-DD-YYYY).
            layers: List of (product_id, layer_name) tuples, e.g. [("MOD11A1.061", "LST_Day_1km")].
            bbox: Bounding box dict {"west": float, "south": float, "east": float, "north": float}.
            geojson: Explicit GeoJSON geometry or FeatureCollection (overrides bbox if provided).
            file_type: "geotiff" or "netcdf4".
            projection: "geographic" (EPSG:4326) or "native".
            date_ranges: Optional list of (start, end) ranges. AppEEARS accepts a
                list of date ranges per task, which is how composite-window
                sampling avoids downloading the whole period.

        Returns:
            task_id: UUID string identifying the queued AppEEARS task.
        """
        date_payload = [
            {
                "startDate": self._format_date(range_start),
                "endDate": self._format_date(range_end),
                "recurring": False,
            }
            for range_start, range_end in (date_ranges or [(start_date, end_date)])
        ]

        if geojson is None:
            if bbox is None:
                raise ValueError("Either bbox or geojson must be provided to submit_area_task.")
            west, south, east, north = bbox["west"], bbox["south"], bbox["east"], bbox["north"]
            geo_payload: dict[str, Any] = {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [west, south],
                                    [east, south],
                                    [east, north],
                                    [west, north],
                                    [west, south],
                                ]
                            ],
                        },
                    }
                ],
            }
        else:
            geo_payload = geojson

        payload: dict[str, Any] = {
            "task_type": "area",
            "task_name": task_name[:40],
            "params": {
                "dates": date_payload,
                "layers": [{"product": prod, "layer": lyr} for prod, lyr in layers],
                "geo": geo_payload,
                "output": {
                    "format": {"type": file_type, "filename_date": "calendar"},
                    "projection": projection,
                },
            },
        }

        resp = self._request_with_retry("POST", "task", json=payload, timeout=30)
        if resp.status_code not in (200, 202):
            raise RuntimeError(
                f"Failed to submit AppEEARS area task '{task_name}' ({resp.status_code}): {resp.text}"
            )

        task_info = resp.json()
        task_id = task_info.get("task_id")
        if not task_id:
            raise RuntimeError(f"AppEEARS response did not contain 'task_id': {resp.text}")

        log(f"appeears: task '{task_name}' submitted successfully -> task_id: {task_id}")
        return str(task_id)

    def get_task_status(self, task_id: str) -> dict[str, Any]:
        """Fetch current status and progress for a specific task."""
        resp = self._request_with_retry("GET", f"status/{task_id}", timeout=20)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to retrieve status for task {task_id} ({resp.status_code}): {resp.text}")
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return data

    def get_task_details(self, task_id: str) -> dict[str, Any]:
        """Fetch the full task specification and details."""
        resp = self._request_with_retry("GET", f"task/{task_id}", timeout=20)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to retrieve task details for {task_id} ({resp.status_code}): {resp.text}")
        return resp.json()

    def wait_for_task(
        self,
        task_id: str,
        poll_interval_seconds: int = 15,
        timeout_seconds: int = 36000, # 10 hours
        should_cancel: Callable[[], bool] | None = None,
        on_progress: Callable[[int, str], None] | None = None,
    ) -> dict[str, Any]:
        """Poll AppEEARS until the task reaches 'done' or 'error' state.

        Extracts multi-step progress from progress.details (Initializing ->
        Downloading -> Subsetting -> Generating Files -> Extracting Stats ->
        Finalizing) and verifies completion against the task object.
        """
        start_time = time.time()
        log(f"appeears: waiting for task {task_id} to complete (timeout: {timeout_seconds}s)...")

        while (time.time() - start_time) < timeout_seconds:
            if should_cancel is not None and should_cancel():
                log(f"appeears: cancellation requested, deleting task {task_id}")
                self.delete_task(task_id)
                raise RuntimeError(f"Task {task_id} canceled.")

            status_info: dict[str, Any] = {}
            try:
                status_info = self.get_task_status(task_id)
            except Exception as exc:
                log(f"appeears: transient status error for {task_id}: {exc}")

            # Extract detailed step progress (Initializing, Downloading, Subsetting, Generating Files, Finalizing)
            progress_obj = status_info.get("progress", {}) if isinstance(status_info, dict) else {}
            progress_summary = int(progress_obj.get("summary") or 0)
            details = progress_obj.get("details", [])
            active_step_desc = None
            if isinstance(details, list):
                for step_item in details:
                    if int(step_item.get("pct_complete", 0)) < 100:
                        active_step_desc = step_item.get("desc")
                        break
                if not active_step_desc and details:
                    active_step_desc = details[-1].get("desc")

            status = status_info.get("status") if isinstance(status_info, dict) else None
            if not status and status_info.get("status_type") == "task":
                status = "processing"

            # Check task details when summary reaches 100% or to confirm status
            if status != "done" and (progress_summary >= 100 or not status):
                try:
                    task_details = self.get_task_details(task_id)
                    status = task_details.get("status") or status
                    summary_obj = task_details.get("summary", {})
                    if summary_obj.get("empty") is True:
                        desc = summary_obj.get("description", "No matching source granules found for the request.")
                        log(f"appeears: task {task_id} finished with empty summary: {desc}")
                        raise ValueError(f"No NASA data available: {desc}")
                except ValueError:
                    raise
                except Exception:
                    pass

            step_label = active_step_desc or status or "processing"
            if on_progress:
                on_progress(progress_summary, f"AppEEARS: {step_label} ({progress_summary}%)")

            if status == "done":
                log(f"appeears: task {task_id} is done.")
                return status_info
            if status == "error":
                error_msg = status_info.get("error") or "Unknown task error"
                raise RuntimeError(f"AppEEARS task {task_id} failed: {error_msg}")

            time.sleep(poll_interval_seconds)

        raise TimeoutError(f"AppEEARS task {task_id} timed out after {timeout_seconds} seconds.")

    def list_bundle_files(self, task_id: str) -> list[dict[str, Any]]:
        """List all output files produced by a completed task."""
        resp = self._request_with_retry("GET", f"bundle/{task_id}", timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to list bundle files for task {task_id} ({resp.status_code}): {resp.text}")
        return resp.json().get("files", [])

    def download_bundle(
        self,
        task_id: str,
        target_dir: Path | str,
        file_patterns: tuple[str, ...] | None = None,
        skip_existing: bool = True,
    ) -> list[Path]:
        """Download output files from a completed AppEEARS task bundle.

        Args:
            task_id: UUID of completed task.
            target_dir: Local folder where files should be saved.
            file_patterns: Optional tuple of substring matches to filter downloaded files
                           (e.g. (".tif", "LST_Day_1km")).
            skip_existing: Whether to skip files already downloaded with matching size.

        Returns:
            List of downloaded file Path objects.
        """
        dest_dir = Path(target_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        files = self.list_bundle_files(task_id)
        if not files:
            log(f"appeears: warning: bundle for task {task_id} contains no files")
            return []

        downloaded_paths: list[Path] = []

        for item in files:
            file_id = item.get("file_id")
            file_name = item.get("file_name") or f"{file_id}.tif"
            file_size = int(item.get("file_size") or 0)

            if file_patterns:
                if not any(pattern.lower() in file_name.lower() for pattern in file_patterns):
                    continue

            clean_rel = file_name.lstrip("/\\").replace("\\", "/")
            local_path = dest_dir / clean_rel
            local_path.parent.mkdir(parents=True, exist_ok=True)
            if skip_existing and local_path.is_file():
                if file_size > 0 and local_path.stat().st_size == file_size:
                    log(f"appeears: skipping existing file {file_name}")
                    downloaded_paths.append(local_path)
                    continue

            download_url = f"{self.base_url}/bundle/{task_id}/{file_id}"
            log(f"appeears: downloading {file_name} ({file_size / 1024 / 1024:.2f} MB)...")

            headers = self._auth_headers()
            with self.session.get(download_url, headers=headers, stream=True, allow_redirects=True) as stream_resp:
                stream_resp.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in stream_resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)

            expected_sha256 = item.get("sha256")
            if expected_sha256:
                hasher = hashlib.sha256()
                with open(local_path, "rb") as f:
                    while c := f.read(65536):
                        hasher.update(c)
                if hasher.hexdigest().lower() != expected_sha256.lower():
                    log(f"appeears: warning: sha256 mismatch for {file_name}")

            downloaded_paths.append(local_path)

        return downloaded_paths

    def delete_task(self, task_id: str) -> bool:
        """Delete a task and its associated files from the AppEEARS account."""
        try:
            resp = self._request_with_retry("DELETE", f"task/{task_id}", timeout=15)
            return resp.status_code == 204
        except Exception as exc:
            log(f"appeears: failed to delete task {task_id}: {exc}")
            return False

    def list_products(self) -> list[dict[str, Any]]:
        """List all available products in AppEEARS."""
        resp = self.session.get(f"{self.base_url}/product", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def list_layers(self, product_id: str) -> dict[str, Any]:
        """List all available layers for a specific product."""
        resp = self.session.get(f"{self.base_url}/product/{product_id}", timeout=30)
        resp.raise_for_status()
        return resp.json()

