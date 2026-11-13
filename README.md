# Earth Alive API

Backend for the Earth Alive platform: a FastAPI service that fetches NASA MODIS land-surface temperature data (MOD11A1) from NASA Earthdata, mosaics and crops it to a user-defined area of interest (AOI), builds a temperature time series, and runs trend statistics on it.

The API is consumed by a Flutter client (hence the CORS configuration). There is no client code in this repository.

---

## Contents

- [Tech stack](#tech-stack)
- [Project layout](#project-layout)
- [Quick start](#quick-start)
- [API reference](#api-reference)
- [Architecture](#architecture)
- [Workflows](#workflows)
- [Data schemas](#data-schemas)
- [Conventions: how to add features](#conventions-how-to-add-features)
- [Configuration and data directories](#configuration-and-data-directories)
- [Testing](#testing)
- [Known issues and technical debt](#known-issues-and-technical-debt)
- [Optimization roadmap](#optimization-roadmap)
- [Repository hazards](#repository-hazards)
- [Further reading](#further-reading)

---

## Tech stack

| Layer | Technology |
| --- | --- |
| HTTP | FastAPI, Uvicorn |
| Validation | Pydantic v2 |
| Remote data | `earthaccess` (NASA Earthdata search/download) |
| Raster I/O | `rasterio`, `gdal_translate` (system binary) |
| Numerics | NumPy, pandas, SciPy |
| Charts | Matplotlib (Agg backend) |
| Tests | pytest, FastAPI `TestClient` |

> **Note:** `requirements.txt` is a full `pip freeze`, so it is pinned but tied to the platform it was generated on (Python 3.14 / Windows). GDAL must be installed as a system dependency for `gdal_translate`; the `rasterio` wheel bundles GDAL 3.12 but neither the CLI nor the HDF4 driver.

---

## Project layout

```
backend/backend/
├── app/
│   ├── main.py                       # FastAPI app, CORS, router registration
│   ├── core/
│   │   └── config.py                 # .env loading + Earthdata settings
│   ├── utils/
│   │   ├── paths.py                  # ensure_directory / ensure_parent_directory / require_file
│   │   ├── cache.py                  # shared, byte-capped cache + atomic writes
│   │   └── logging.py                # timestamped stdout logging for long jobs
│   ├── api/
│   │   └── routes/
│   │       ├── health.py             # GET /api/v1/health/
│   │       ├── variables.py          # GET /api/v1/variables/
│   │       ├── analysis.py           # POST /api/v1/analysis/trend
│   │       ├── comparison.py         # POST /api/v1/analysis/compare/*
│   │       └── jobs.py               # GET /jobs, GET /jobs/{id}, POST /jobs/{id}/cancel
│   ├── jobs/
│   │   ├── queue.py                  # create/list/cancel + status/result lookup
│   │   ├── store.py                  # atomic status.json/result.json storage
│   │   ├── tasks.py                  # RQ worker entrypoints, cancellation, comparison fan-out
│   │   └── errors.py                 # JobError / JobCanceled / JobNotCancelable
│   └── services/
│       ├── earthdata_auth.py         # credential-aware Earthdata login (shared)
│       ├── modis_processor.py        # Earthdata search/download, GDAL, raster helpers, Egypt CLI
│       ├── modis_timeseries.py       # per-date collection, granule cache, aggregation, CSV/JSON output
│       ├── statistical_analysis.py   # validation + MK / modified MK / seasonal MK / OLS / HAC / Welch
│       ├── modis_downloader.py       # DEAD CODE: duplicate of modis_processor search/download
│       └── raster_processor.py       # QA/heatmap CLI: path arguments, auto-created output dirs
├── docs/
│   └── statistical_methods.md        # methodology rationale and assumptions
├── tests/
│   ├── test_api_analysis.py          # endpoint contract tests
│   ├── test_api_jobs.py              # job listing, details, cancellation
│   ├── test_job_fanout.py            # comparison parts, assembly, watchdog, cancel
│   ├── test_ndvi_cache.py            # NDVI waves, AOI cache, atomic writes, LRU trim
│   ├── test_statistical_analysis.py  # statistics unit tests
│   ├── test_modis_timeseries.py      # collection/aggregation unit tests + integration
│   ├── test_modis_validation.py      # QA mask / conversion / artifact validation
│   └── test_global_aoi.py            # multi-region pipeline tests + integration
├── .env.example                      # Earthdata credential template
├── Dockerfile                        # Python 3.14 slim + conda-forge GDAL (HDF4)
├── docker-compose.yml                # uvicorn --reload with bind mounts
├── .dockerignore
├── requirements.txt
└── README.md
```

There is **no** `pyproject.toml` or `conftest.py`; `pytest.ini`, `.env`, `.gitignore`, `Dockerfile`, and `docker-compose.yml` now exist.

---

## Quick start

### 1. Install dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

GDAL must also be available on `PATH` (the pipeline shells out to `gdal_translate`). The build must include the **HDF4** driver, because MOD11A1 granules are HDF4-EOS files; the `rasterio` wheel bundles GDAL but not HDF4. Use the `gdal` package from conda-forge or OSGeo4W. If it is installed somewhere off `PATH`, set `GDAL_TRANSLATE` to the full executable path:

```powershell
$env:GDAL_TRANSLATE = "C:\OSGeo4W\bin\gdal_translate.exe"
```

Without GDAL, `POST /api/v1/analysis/trend` returns `503` naming `gdal_translate` instead of a misleading "no valid observations" error.

### 2. Configure NASA Earthdata credentials

Copy the template and fill in your Earthdata account:

```powershell
Copy-Item .env.example .env
```

```dotenv
# .env
EARTHDATA_USERNAME=your_earthdata_username
EARTHDATA_PASSWORD=your_earthdata_password
# or, instead of the pair above:
# EARTHDATA_TOKEN=your_earthdata_token
```

`app/core/config.py` loads `.env` at import time into `os.environ` without overriding variables already set in the shell (so CI/container values win). `ensure_earthaccess_auth()` then calls `earthaccess.login(strategy="environment")`.

Without credentials it falls back to the `netrc` strategy. It never uses the interactive strategy, so an HTTP request can never block waiting for a username prompt. An optional `EARTHDATA_AUTH_STRATEGY` variable accepts `environment`, `netrc`, or `all`.

Authentication failures are raised as `ConnectionError`, which the analysis route maps to `503`.

### 3. Run the API

From this directory (`backend/backend`):

```powershell
uvicorn app.main:app --reload
```

- Docs: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/api/v1/health/>

### 3b. Run the API in Docker

`Dockerfile` and `docker-compose.yml` run the app on `python:3.14-slim-trixie` with the pinned `requirements.txt` and a conda-forge GDAL that includes the **HDF4** driver, then run `uvicorn --reload`:

```powershell
docker compose up --build
```

- `./app` is bind-mounted for reload; `./data` is bind-mounted so downloads and artifacts persist on the host.
- Three services: `redis`, `api`, and `worker`. The worker runs `python -m rq.cli worker --url redis://redis:6379/0 analysis` so it does not depend on the pip console-script PATH.
- `.env` is injected through `env_file` (missing file is tolerated).
- Health is polled through `curl` against `/api/v1/health/`.
- The image mirrors the local layout (`/srv/backend/backend/app`) so project-rooted data paths resolve to `/srv/backend/data`.
- The build asserts HDF4 support with `gdalinfo --formats | grep -qi hdf4` and imports `rq`/`redis` after installing requirements, so both GDAL and queue problems fail the build instead of container startup.

Compose reuses an existing `earth-alive-api:dev` image by default. After changing `requirements.txt` or the `Dockerfile`, force a rebuild:

```powershell
docker compose build --no-cache
docker compose up
```

**Why this split:** Alpine's `gdal` has no HDF4 driver, and Debian/Ubuntu package no HDF4-enabled GDAL (`libgdal-hdf4` does not exist there). Installing conda's `gdal` package would drag in conda's own Python, whose version is decided by the solver. So the image takes Python 3.14 from the official image and installs only `libgdal-core` + `libgdal-hdf4` from conda-forge — no Python dependency, no version substitution.

### 4. Run the tests

Tests import `app.*`, so they must run from **this** directory (`backend/backend`):

```powershell
cd D:\assignments\backend\backend
D:\assignments\backend\venv_earthalive\Scripts\python.exe -m pytest tests -m "not integration" -q
```

Integration tests hit live NASA Earthdata and download files; exclude them unless you intend to.

---

## API reference

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Service banner |
| GET | `/health`, `/health/` | Unversioned liveness alias (same payload as the versioned check) |
| GET | `/api/v1/health/` | Health check |
| GET | `/api/v1/variables/` | List supported environmental variables (static metadata) |
| GET | `/api/v1/variables/{variable_id}` | Single variable metadata |
| POST | `/api/v1/analysis/trend` | Queue a trend analysis job for LST, NDVI, precipitation, soil moisture, or air temperature |
| POST | `/api/v1/analysis/compare/variables` | Queue a background multi-variable comparison job |
| POST | `/api/v1/analysis/compare/regions` | Queue a background multi-region comparison job |
| GET | `/api/v1/analysis/jobs` | List jobs, newest first, with filters and pagination |
| GET | `/api/v1/analysis/jobs/{job_id}` | Job status, progress, and the original request |
| GET | `/api/v1/analysis/jobs/{job_id}/result` | Job result, including base64 charts |
| POST | `/api/v1/analysis/jobs/{job_id}/cancel` | Cancel a queued or running job (`?force=true` to stop immediately) |
| GET | `/api/v1/analysis/health` | Analysis service liveness |


### `POST /api/v1/analysis/trend`

Request body:

```json
{
  "variable": "ndvi",
  "aoi": { "west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7 },
  "start_date": "2024-01-01",
  "end_date": "2024-12-31",
  "temporal_resolution": "monthly",
  "analysis_methods": ["linear_regression", "mann_kendall", "t_test"]
}
```

- `variable`: `land_surface_temperature` (LST), `ndvi`, `precipitation`, `soil_moisture`, or `air_temperature`. Aliases such as `LST`, `MOD11A1`, `MOD13A1`, `MOD13Q1`, `GPM`, `SMAP`, and `MERRA` are accepted.
- `product`: deprecated alias for `variable`; a dataset short name (`MOD11A1`, `MOD13A1`, `MOD13Q1`, `GPM_3IMERGD`, `SPL3SMP_E`, `M2SDNXSLV`). If both are sent they must resolve to the same variable.
- If neither is sent, the endpoint defaults to `land_surface_temperature`.
- `temporal_resolution`: `daily` | `monthly` | `yearly`
- `analysis_methods`: `linear_regression`, `mann_kendall`, `modified_mann_kendall`, `seasonal_mann_kendall`, `t_test`
- `start_date` must be on or after `2000-02-24`; the range is capped at `366 * 30` days.
- `monthly` and `yearly` aggregate the daily variable records to period means. Precipitation stays a mean daily rate (`mm/day`) rather than a monthly total, so all variables use the same statistic.
- NDVI daily records are MODIS 16-day period values carried forward across their window (`source_frequency: "16-day"`, `coverage_status: "period_value"`). The default product is **MOD13A1 (500 m)**; set `NDVI_PRODUCT=MOD13Q1` for 250 m.
- NDVI is analysed on a 0.005° grid (about 500 m) for MOD13A1 and 0.0025° for MOD13Q1, matching each product's native pixel size.

The route validates the request and enqueues a `trend_analysis` job, returning `202`:

```json
{
  "job_id": "9c0f...",
  "status": "queued",
  "status_url": "/api/v1/analysis/jobs/9c0f...",
  "result_url": "/api/v1/analysis/jobs/9c0f.../result"
}
```

Poll `status_url`, then fetch the result envelope from `result_url`:

```json
{
  "success": true,
  "job_id": "9c0f...",
  "metadata": { "mode": "trend_analysis", "variable": "ndvi", "display_name": "Vegetation Index (NDVI)", "product": "MOD13A1", "unit": "NDVI", "native_resolution": "16-day", "temporal_resolution": "monthly", "start_date": "...", "end_date": "...", "aoi": {}, "analysis_methods": [] },
  "data_quality": { "observation_count": 12, "valid_observation_count": 12, "missing_observation_percentage": 0.0, "data_coverage_percentage": 98.3, "warnings": [] },
  "analysis_results": { "linear_regression": {}, "mann_kendall": {} },
  "visualizations": {
    "heatmap": { "content_type": "image/png", "base64": "iVBORw0..." },
    "linear_regression": { "content_type": "image/png", "base64": "iVBORw0..." }
  },
  "warnings": []
}
```

Charts are returned inline as base64 PNG objects keyed by figure or method name (`heatmap`, `linear_regression`, `mann_kendall`, `modified_mann_kendall`, `seasonal_mann_kendall`, `t_test`). No file paths are returned.

Submit-time codes: `400` invalid input, `503` queue unavailable. Worker failures keep their original meaning when the result is fetched: `404` no data / insufficient observations, `503` Earthdata or GDAL configuration failure, `504` processing timeout, `500` unexpected error. The status document records `status_code` and `error_detail`.

### Analysis jobs (background)

All three analysis endpoints (`/trend`, `/compare/variables`, `/compare/regions`) answer `202 Accepted` immediately instead of blocking the request:

```json
{
  "job_id": "9c0f...",
  "status": "queued",
  "status_url": "/api/v1/analysis/jobs/9c0f...",
  "result_url": "/api/v1/analysis/jobs/9c0f.../result"
}
```

Work runs in an RQ worker backed by Redis (`ANALYSIS_QUEUE_MODE=redis`). Set `ANALYSIS_QUEUE_MODE=inline` to execute the job in-process for tests or local debugging without Redis. Job status and results are persisted as JSON under `data/jobs/{job_id}/`.

Compose starts **two workers by default** (`deploy.replicas: 2`) so a download-bound job does not block a compute-bound one. Scale with `docker compose up -d --scale worker=4`. Worker jobs are capped by `ANALYSIS_JOB_TIMEOUT` (default `21600` seconds, six hours) so a wedged download fails instead of holding the worker forever. The job store, cancellation marker, and shared cache are all files under `./data`, so replicas coordinate without extra infrastructure.

Comparison jobs use every idle worker: a request with N variables (or N regions) is enqueued as N independent part jobs. Each part writes its own result under `data/jobs/{job_id}/parts/`; the last part to finish is detected with an atomic Redis counter and assembles the comparison, so no worker is parked waiting for the others. A delayed watchdog job settles the parent if a part is killed hard by the RQ timeout.

Supported variables: `land_surface_temperature`, `ndvi`, `precipitation`, `soil_moisture`, `air_temperature`. Supported methods: `linear_regression`, `mann_kendall`, `modified_mann_kendall`, `seasonal_mann_kendall`, `t_test`. Only `temporal_resolution: "daily"` is accepted.

Shared guards:

- `start_date` must be on or after `2000-02-24` (MOD11A1 v061 coverage start).
- The range is capped at `366 * 30` days.
- Date ranges longer than `MAX_DAYS_PER_DAILY_JOB` (default `1826`, roughly five years) are collected in chunks and merged by observation date before analysis.
- `POST /compare/regions` accepts at most 8 regions and rejects duplicate bounding boxes.

#### `POST /api/v1/analysis/compare/variables`

```json
{
  "aoi": { "west": 31.0, "south": 29.0, "east": 32.0, "north": 30.0 },
  "start_date": "2024-01-01",
  "end_date": "2024-01-31",
  "temporal_resolution": "daily",
  "variables": ["land_surface_temperature", "ndvi", "precipitation"],
  "analysis_methods": ["linear_regression", "mann_kendall", "t_test"],
  "include_visualizations": true
}
```

The result embeds one entry per variable under `variables`, each with `metadata`, `series`, `data_quality`, `analysis_results`, per-method `visualizations`, `status`, and `warnings`. A variable that fails is marked `status: "failed"` with an `error` and does not abort the rest of the job.

With two or more variables, each variable is collected and analysed as a separate RQ part, so the parts run concurrently on different worker replicas. The parent job reports combined progress (`label: "1/3 part(s) finished — ndvi"`, `percent` = mean of part progress) and produces the same result document as the sequential path.

#### `POST /api/v1/analysis/compare/regions`

```json
{
  "variable": "land_surface_temperature",
  "regions": [
    { "west": 31.0, "south": 29.0, "east": 31.5, "north": 29.5 },
    { "west": 31.5, "south": 29.0, "east": 32.0, "north": 29.5 }
  ],
  "start_date": "2024-01-01",
  "end_date": "2024-01-31",
  "temporal_resolution": "daily",
  "analysis_methods": ["linear_regression", "mann_kendall"],
  "include_visualizations": true
}
```

The result embeds one entry per region under `regions` (keys `region_1`, `region_2`, ...), each with the same per-variable shape plus its `aoi`. Comparison-level charts are under `comparison.visualizations`: `timeseries_comparison`, `trend_summary`, and `coverage_comparison`, all base64 PNG objects.

#### `GET /api/v1/analysis/jobs`

Lists persisted jobs, newest first. Query parameters: `limit` (default 50, max 200), `offset`, `kind`, and `status` (one value or a comma-separated list, e.g. `queued,started`).

```json
{
  "items": [
    {
      "job_id": "9c0f...",
      "kind": "trend_analysis",
      "status": "started",
      "percent": 40.0,
      "created_at": "2026-11-13T06:24:52+00:00",
      "request": { "variable": "ndvi", "start_date": "2024-01-01", "end_date": "2024-12-31" }
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0,
  "next": null
}
```

#### `GET /api/v1/analysis/jobs/{job_id}`

Returns the persisted status: `queued`, `started`, `canceling`, `canceled`, `finished`, or `failed`, along with `stage`, `label`, `current`, `total`, `percent`, timestamps, and the original `request`. Custom job ids are also RQ job ids, so `rq_status` is included while the job runs. Unknown ids return `404` without creating a directory.

#### `GET /api/v1/analysis/jobs/{job_id}/result`

Returns the stored result once the job finishes. `409` means still running or canceled; `404` means the job is unknown or no result was written.

#### `POST /api/v1/analysis/jobs/{job_id}/cancel`

Cancellation is cooperative by default. The endpoint sets `cancel_requested` and moves the job to `canceling`; the worker stops at its next checkpoint (between date chunks, composites, or days), so an in-flight download or `gdal_translate` call is never interrupted half-way.

| Job state | Result |
| --- | --- |
| `queued` | Removed from Redis (`Job.cancel()`); status becomes `canceled` |
| `started` | Status becomes `canceling`; worker writes `canceled` at the next checkpoint |
| `started` with `?force=true` | RQ is asked to stop the executing horse; status becomes `canceled` immediately |
| `canceling` / `canceled` | Idempotent `200` |
| `finished` / `failed` | `409` |
| Unknown id | `404` |
| `ANALYSIS_QUEUE_MODE=inline` | `409` — inline jobs run inside the submitting request and cannot be canceled |

If the forced stop command cannot be delivered, the endpoint falls back to cooperative cancellation and leaves the job in `canceling`. A canceled job has no result: `GET .../result` returns `409`.

#### Variable adapters

| Variable | Dataset | Native frequency | Notes |
| --- | --- | --- | --- |
| `land_surface_temperature` | MOD11A1 | daily | Existing MODIS pipeline; QA mask and Kelvin→Celsius |
| `ndvi` | MOD13A1 (default) / MOD13Q1 | 16-day | Each composite is carried forward across its 16-day window; 500 m by default, `NDVI_PRODUCT=MOD13Q1` for 250 m |
| `precipitation` | GPM_3IMERGD | daily | Remote `earthaccess.open()` + xarray AOI subset |
| `soil_moisture` | SPL3SMP_E | daily | Remote subset with retrieval-quality filtering |
| `air_temperature` | M2SDNXSLV | daily | Kelvin→Celsius |

NDVI daily rows are **period values**, not interpolated observations. Each record declares `source_frequency: "16-day"` and `coverage_status: "period_value"`; days beyond the 16-day window of the last available composite are explicit missing observations.

### NDVI performance and caching

NDVI is the only variable that downloads full MODIS tiles. A national AOI such as Egypt straddles four sinusoidal tiles, so one 16-day composite is four granules (about 60 MB each for MOD13A1, 200+ MB each for MOD13Q1).

Three things keep that manageable:

1. **AOI-first warping.** Each granule is warped with a single `gdalwarp -t_srs EPSG:4326 -te <aoi> -ts <w> <h>` call that reads only the slice of the HDF4 subdataset covering the AOI and writes an array already on the analysis grid. This replaces a full-tile `gdal_translate` plus a `rasterio` read plus a `reproject()`, and produces identical means.
2. **Waves.** Composites are processed in groups of whole composites (`NDVI_GRANULE_BATCH`, default 8 granules). A wave is downloaded with up to `NDVI_DOWNLOAD_THREADS` parallel streams (default 8), then its granules are warped in a `NDVI_WARP_WORKERS` thread pool (default 4), then the wave's means are computed. Peak disk use is one wave, not one year.
3. **Shared cache under `data/cache/<product>/`.** `raw/` holds downloaded HDFs, `aoi/<hash>/` holds the warped NDVI and QA rasters for one AOI (about 1 MB per granule). The AOI cache is checked *before* contacting Earthdata, so re-running the same analysis over the same AOI downloads nothing. The whole cache is trimmed to `ANALYSIS_CACHE_GB` (default 0.5 GB) after every wave; files touched in the last 10 minutes are never evicted, so concurrent workers cannot delete each other's inputs.

Downloads are logged with elapsed time and megabytes, an `already cached` line when granules are reused, and a heartbeat every 15 seconds:

```text
modis: downloading 8 granule(s) for 2023-01-01..2023-01-17 (2 composites)
modis: still downloading for 2023-01-01..2023-01-17 (2 composites)… 45s elapsed, 87.2 MB
modis: downloaded 8 granule(s) in 128.4s (479.6 MB) for 2023-01-01..2023-01-17 (2 composites)
variables: ndvi: already cached 4 granule(s) for 2023-02-02
```

Job status mirrors the download heartbeat, so the API shows progress without reading logs:

```json
{ "stage": "collecting", "label": "ndvi downloading composites 2023-01-01..2023-01-17… 45s elapsed, 87.2 MB", "percent": 8.7 }
```

Once a wave is local, status switches to per-composite progress instead of sitting at a chunk boundary:

```json
{ "stage": "collecting", "label": "ndvi composite 2023-03-06 (5/23)", "current": 5, "total": 23, "percent": 21.74 }
```


---

## Architecture

The codebase is a two-layer design:

1. **API layer** (`app/api/routes/*`) owns HTTP concerns: Pydantic request models, validation, exception-to-status translation, and the response envelope.
2. **Service layer** (`app/services/*`) owns everything else: remote data acquisition, raster processing, aggregation, persistence, and statistics. Services operate on plain dictionaries and module-level functions.

There is no dependency-injection container and no relational database. State lives in the filesystem under `data/` and in an in-memory granule cache file. Analysis jobs add Redis/RQ as a queue, but job status and results are still plain JSON files under `data/jobs/{job_id}/`, written by whichever process ran the job.

Flow at a glance:

```
HTTP POST /analysis/trend
   → Pydantic validation (422)
   → route-level validation (400)
   → create_job("trend_analysis") → write status.json
   → enqueue on Redis (RQ) and return 202

RQ worker
   → TimeSeriesConfig (dataclass validation)
   → collect_mod11a1_timeseries
        → earthaccess search (per day)
        → GranuleCache (JSON on disk)
        → earthaccess download
        → gdal_translate ×2 per granule
        → QA mask → Kelvin→Celsius → reproject → mosaic → summarize
        → aggregate to daily/monthly/yearly
        → write CSV + JSON under data/jobs/{job_id}/trend/time_series/
   → _as_stats_records (schema translation)
   → validate_time_series (pandas)
   → method dispatch (linear, MK, modified MK, seasonal MK, t-test)
   → write result.json and mark status finished
```

---

## Workflows

### 1. Trend job (`POST /api/v1/analysis/trend`)

All analysis work is asynchronous.

1. Pydantic validates `TrendAnalysisRequest` (literal enums, coordinate ranges) → 422 on malformed input.
2. The handler re-validates the same constraints manually, plus the coverage start and maximum range → 400.
3. `create_job` writes `data/jobs/{job_id}/status.json` and enqueues `app.jobs.tasks.run_job` on the `analysis` RQ queue → `202` with `job_id`, `status_url`, `result_url`.
4. The worker calls `execute_trend_analysis` (`app/services/trend_service.py`), which resolves the requested variable, collects it through the shared registry/chunking helper, and aggregates daily records to monthly/yearly means when needed. MOD11A1 still searches **one Earthdata query per calendar day**, plus downloads and two `gdal_translate` subprocesses per granule; the other adapters use their product-specific collectors.
5. `_as_stats_records` (`trend_service.py`) translates between the collector and statistics vocabularies (see [Data schemas](#data-schemas)).
6. `validate_time_series` parses dates, rejects duplicates, separates missing observations, and computes coverage warnings.
7. Requested methods are dispatched inline in the worker.
8. Base64 charts are generated, the result is written to `result.json`, and the status becomes `finished`. Failures store `status_code` and `error_detail`; the result endpoint replays the original HTTP status.

### 2. Comparison jobs (`/compare/variables`, `/compare/regions`)

Same job lifecycle, but `execute_variable_comparison` / `execute_region_comparison` (`app/services/comparison_service.py`) run the variable adapters in `app/services/variables/`. Long date ranges are chunked at `MAX_DAYS_PER_DAILY_JOB`, merged by observation date, then analyzed through the shared `app/services/analysis_engine.py`.

In Redis mode a comparison with more than one part is split by `app/jobs/queue.py` into one `run_comparison_part` job per variable or region. Parts write `parts/{index}.json` and `parts/{index}.status.json` under the parent job directory, so the API process, the part workers, and the assembler only share files. Progress updates re-read those files to keep the parent percentage monotonic. When the Redis counter reaches the part count, the finishing worker calls the same assemblers the inline path uses (`assemble_variable_comparison` / `assemble_region_comparison`), writes `result.json`, and marks the job `finished`. Cancelling the parent stops its queued parts and lets running parts stop at their next checkpoint.

### 3. Full statistics orchestrator (not wired to the API)

`analyze_time_series` in `app/services/statistical_analysis.py:702-818` is the *complete* orchestration: standard and modified MK, seasonal MK, OLS, HAC/Newey-West, optional Welch comparison, automatic method selection, scientific limitations, chart generation, and JSON persistence.

The API routes **do not call it**. The trend job keeps a smaller inline dispatch (`trend_service.py`), while comparison jobs use the shared `analysis_engine.py`. `visualizations` is populated with base64 PNG charts on every analysis path.

### 4. Offline / CLI pipelines

- `run_modis_egypt_pipeline` (`app/services/modis_processor.py:170`) is a standalone CLI/`__main__` pipeline for the Egypt bounding box.
- `python -m app.services.raster_processor` inspects an LST/QC GeoTIFF pair and renders a heatmap. All paths are arguments, and defaults are project-rooted under `data/processed`:

```powershell
python -m app.services.raster_processor --lst path/to/LST.tif --qc path/to/QC.tif `
    --output out/heatmap.png --title "MODIS Land Surface Temperature"
# --stats-only prints statistics without writing a heatmap
# --plot-only writes the heatmap without printing statistics
```

  Missing inputs raise `FileNotFoundError` with the resolved path; the output directory is created automatically.
- `tests/test_global_aoi.py` contains `_run_aoi_pipeline`, a third implementation of the same tile → mask → reproject → mosaic loop for four test regions.

The same raster loop therefore exists in three places: `run_modis_egypt_pipeline`, `process_aoi_for_date`, and the test helper. Only `process_aoi_for_date` is used by the API.

### 5. Test workflow

- Unit tests exercise pure helpers and services with `tmp_path`.
- `pytest.ini` registers the `integration` marker, but it is not in `addopts`, so integration tests still run by default. Use `-m "not integration"` to skip live NASA downloads.
- Tests import `app.*`, matching `uvicorn app.main:app`; run pytest from `backend/backend`.

---

## Data schemas

Two canonical record shapes exist, and the adapter between them is the most important seam in the codebase.

### Collector record (`modis_timeseries.py`)

```python
{
  "observation_date": "2024-01-01",
  "mean_temperature_celsius": 12.4,
  "minimum_temperature_celsius": 8.0,
  "maximum_temperature_celsius": 17.2,
  "valid_pixels": 8123,
  "total_pixels": 9000,
  "valid_data_percentage": 90.3,
  "missing_observation": False,
  "missing_reason": None,
  "granules_used": ["MOD11A1...h20v05..."],
  "coverage_status": "complete",
}
```

### Statistics record (`statistical_analysis.py`)

```python
{
  "observation_date": "2024-01-01",
  "mean_lst": 12.4,
  "valid_pixel_count": 8123,
  "total_pixel_count": 9000,
  "valid_data_percentage": 90.3,
  "missing": False,
}
```

`_as_stats_records` maps one to the other. Any new product (NDVI, precipitation, soil moisture) must map into the **collector shape** first, then flow through the adapter.

### Analysis result conventions

Every statistical function returns a dictionary containing:

- `status`: `calculated` | `insufficient_data` | `not_applicable` | `not_supported`
- `warnings`: list of strings
- `assumptions`: list of strings describing what the result depends on

Insufficient data is **never** raised from the statistics layer — only `validate_time_series` raises. This lets the API report partial results.

---

## Conventions: how to add features

### General rules

- Services are module-level functions over plain dictionaries. Only configuration is modelled as a `@dataclass`, with validation and `Path` coercion in `__post_init__`.
- HTTP concerns stay in routes: validate → build config → call **one** service function → translate exceptions (`TimeoutError` → 504, `ConnectionError`/`OSError` → 503, `ValueError` → 404) → return the envelope.
- All artifacts are written under `data/{raw,processed}/...`. Tests always use `tmp_path`.
- Tests monkeypatch service functions by dotted path, e.g. `backend.app.api.routes.analysis.collect_mod11a1_timeseries`.
- Every statistical method documents its assumptions in its result *and* in `docs/statistical_methods.md`. Treat that document as a contract.
- Use `from __future__ import annotations` at the top of every module (existing style).

### Recipe: add a new satellite product

Example: MOD13A1 NDVI.

1. Create `app/services/<product>_timeseries.py` with its own config dataclass, QA/scale constants (do **not** reuse MOD11A1 QA bits or the 0.02/273.15 conversion), and a `collect_<product>_timeseries(config)` function.
2. Emit the canonical collector record shape, replacing temperature fields with the product's measurement fields while keeping date/pixel/missing fields identical.
3. Extend the adapter so the new measurement reaches the statistics layer, or generalize the schema to `mean_value` + `unit`.
4. Add the product to `SUPPORTED_PRODUCTS` in `app/api/routes/analysis.py`.
5. Add a `unit` field to response metadata (the current envelope hardcodes temperature semantics).
6. Add request model options and update the response example in the route docstring.
7. Add unit tests for QA masking and aggregation, plus an integration test marked `@pytest.mark.integration`.

### Recipe: add a new statistical method

1. Write a pure function `(records) -> dict` in `app/services/statistical_analysis.py`.
2. Return `status`, `warnings`, `assumptions`, and the computed statistics.
3. Add the method name to the `Literal` in `TrendAnalysisRequest`, to `SUPPORTED_ANALYSIS_METHODS`, and to the dispatch in the route.
4. Better: wire the route to `analyze_time_series` first, so there is only one dispatch to extend.
5. Add a unit test using `_make_records`-style fixtures.
6. Document the method and its caveats in `docs/statistical_methods.md`.

### Recipe: add a new endpoint

1. Create one router module per domain in `app/api/routes/`, declaring its own `prefix` and `tags`.
2. Mount it in `app/main.py` under `/api/v1`.
3. Keep response models typed — prefer explicit Pydantic models over `dict[str, Any]`.
4. Add a contract test in `tests/test_api_analysis.py` (or a sibling file).

### Recipe: add a new region preset

Regions are plain bounding-box dictionaries `{west, south, east, north}`. Use the existing `AOIRequest`/`TimeSeriesConfig` validation rather than adding new constants; avoid defaulting to `EGYPT_BBOX` where the caller should be explicit.

---

## Configuration and data directories

| Path | Purpose |
| --- | --- |
| `data/raw/` | Downloaded HDF granules |
| `data/processed/time_series/` | Time-series CSV + JSON outputs |
| `data/processed/egypt/` | Egypt CLI raster, heatmap, report |
| `data/processed/analysis/` | Default analysis output directory |
| `data/jobs/{job_id}/` | Comparison job status, intermediate artifacts, and `result.json` |
| `data/cache/modis_granule_cache.json` | Granule download cache (filename → local path) |
| `data/cache/<product>/raw/` | Shared downloaded HDFs (LRU, `ANALYSIS_CACHE_GB`) |
| `data/cache/<product>/aoi/<hash>/` | Warped NDVI/QA rasters for one AOI, reused across jobs |
| `data/processed/global_tests/` | Integration-test outputs, per region |

Paths are derived from `Path(__file__).resolve().parents[3]`, which resolves to `/srv/backend` in the container (where `./data` is mounted) and to the repository parent locally. The shared analysis cache can be relocated with `ANALYSIS_CACHE_DIR` and is size-capped with `ANALYSIS_CACHE_GB` (default `0.5`); job artifacts under `data/jobs/` are still never pruned automatically.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `NDVI_PRODUCT` | `MOD13A1` | NDVI source product (`MOD13A1` 500 m or `MOD13Q1` 250 m) |
| `NDVI_GRANULE_BATCH` | `8` | Granules downloaded per wave |
| `NDVI_DOWNLOAD_THREADS` | `8` | Parallel HTTP streams per wave |
| `NDVI_WARP_WORKERS` | `4` | Parallel `gdalwarp` subprocesses |
| `ANALYSIS_CACHE_DIR` | `data/cache` | Shared cache location |
| `ANALYSIS_CACHE_GB` | `0.5` | Cache budget before LRU eviction |
| `ANALYSIS_JOB_TIMEOUT` | `21600` | RQ job timeout in seconds |
| `MAX_DAYS_PER_DAILY_JOB` | `1826` | Chunk size for long date ranges |

Credentials are handled separately by `app/core/config.py`: it loads the project `.env` at import time (without overriding existing environment variables) and exposes `get_settings()`. Only Earthdata credentials and the auth strategy are configurable this way today; data paths are still code-level constants (project-rooted, but not environment-overridable), apart from the `raster_processor` CLI flags.

---

## Testing

```powershell
# from D:\assignments\backend
pytest backend/tests -m "not integration"     # unit + contract
pytest backend/tests -m "integration"         # requires NASA credentials + network
```

Coverage map:

| File | What it covers |
| --- | --- |
| `test_statistical_analysis.py` | Validation, OLS, MK, modified MK, seasonal MK, HAC, Welch, autocorrelation warnings, constant series |
| `test_modis_timeseries.py` | Date ranges, missing records, pixel summaries, granule cache, monthly aggregation, CSV/JSON output |
| `test_modis_validation.py` | QA bit masking, LST validity, Kelvin conversion, artifact CRS/bounds, report fields |
| `test_global_aoi.py` | Dynamic AOI definitions and multi-region pipeline behavior |
| `test_api_analysis.py` | Health endpoint, trend job submission, LST/NDVI variable selection and aliases, inline job status/result, worker error replay, invalid AOI/date/range rejection |
| `test_comparison.py` | Variable registry, generic stats adapter, base64 charts, date chunking, variable/region execution |
| `test_api_comparison.py` | Comparison job submission, validation guards (variables, dates, duplicate AOIs), status/result routes |
| `test_encoding.py` | Base64 helper round-trips for bytes and files |

Missing coverage: route-level error mapping (503/504/404), `_as_stats_records` edge cases, concurrent request behavior, the full orchestrator's output shape, and live Earthdata checks for the comparison variable adapters.

---

## Known issues and technical debt

### Correctness and contract

| # | Issue | Location |
| --- | --- | --- |
| 1 | `paired_period_t_test` is a Welch **unpaired** two-sample test. The name is misleading. | `statistical_analysis.py:584` |
| 2 | Aggregation **drops periods that are entirely missing**, so `observation_count` and `missing_observation_percentage` under-report gaps (e.g. 6 empty months in a 12-month request → `missing = 0%`). | `modis_timeseries.py:271-276` |
| 3 | When a date has no valid pixels, the detailed `obs_record` (including its warnings) is discarded and a generic missing record is appended. | `modis_timeseries.py:394-400` |
| 4 | Overlapping tiles are merged last-wins instead of averaged, producing seams at tile boundaries. | `modis_timeseries.py:260` |
| 5 | `kendall_tau` uses the tie-free (tau-a) denominator even though the tie correction is computed for the variance. | `statistical_analysis.py:118-120` |
| 6 | Resolved for trend jobs: `region_name` is now `trend_{job_id}`, so artifacts no longer collide. The helper's default remains `trend_api`. | `trend_service.py` |
| 7 | Resolved: trend results include `metadata.output_files`; comparison artifacts persist under `data/jobs/{job_id}/`. | `trend_service.py`, `comparison_service.py` |
| 8 | Resolved in the trend worker: `int(record.get(...) or 0)` tolerates `None`. | `trend_service.py` |
| 9 | Manual validation duplicates Pydantic constraints, though the coverage/range checks add real guards. | `analysis.py` |
| 10 | Result payloads are still `dict[str, Any]`; only the `202` job envelope is typed, so OpenAPI documents no full result contract. | `analysis.py` |

### Scale and robustness

| # | Issue | Impact |
| --- | --- | --- |
| 1 | A one-year monthly request performs ~365 sequential Earthdata searches + downloads + GDAL subprocesses. It now runs in the worker instead of the HTTP request, but is still sequential. | Minutes to hours per job; one worker blocked. |
| 2 | AOI size is not capped. The trend and comparison routes cap the date range at 30 years. | Out-of-memory for a near-global bbox. |
| 3 | `TimeoutError` maps to 504 but nothing ever raises it. | Dead error path. |
| 4 | Granule cache rewrites the entire JSON per granule with no locking. | Corruption under concurrency. |
| 5 | Intermediate per-granule TIFs are never cleaned; cache has no TTL or size limit. | Unbounded disk growth. |
| 6 | No request IDs. Job status exists, but nothing schedules `prune_old_jobs()` for `data/jobs/`. | Stale job artifacts accumulate. |

### Hygiene

- `requirements.txt` is a full `pip freeze` tied to the generating platform (Python 3.14 / Windows), not a curated dependency list; GDAL remains an undocumented system dependency.
- No packaging (`pyproject.toml`); the `try: from app... except ModuleNotFoundError: from backend.app...` shim is copy-pasted across route and service modules. It now re-raises unrelated missing packages, but the duplication remains.
- `modis_downloader.py` is dead duplicate code.
- Path handling is now centralized in `app/utils/paths.py` for the raster CLI and MODIS outputs, but `statistical_analysis`/`modis_timeseries` still build some artifact paths inline instead of going through the helpers.
- `allow_origins=["*"]`, no authentication, no rate limiting.
- Pydantic v2 deprecated `example=` in `Field`; use `json_schema_extra`.
- `__pycache__` artifacts sit in the tree; no `.gitignore`.
- The `integration` marker is registered in `pytest.ini`, but it is not in `addopts`, so network tests still run by default unless you pass `-m "not integration"`.

---

## Optimization roadmap

### P0 — Correctness and contract (small diffs, high trust impact)

1. Preserve entirely-missing periods during aggregation (fixes `data_quality`).
2. Add AOI hash + date range to artifact filenames (or return a per-request directory).
3. Average overlapping tiles instead of last-wins.
4. Rename `paired_period_t_test` to `welch_period_t_test` (or implement a true paired test).
5. Fix tau tie handling.
6. Delete `modis_downloader.py` (done for `raster_processor.py`: rewritten as a parameterized CLI).
7. Complete and pin `requirements.txt`; add `pytest.ini` with `addopts = -m "not integration"`.

### P1 — Unify the architecture (highest leverage)

1. Make `POST /api/v1/analysis/trend` call `analyze_time_series`; delete the inline dispatch.
2. Extract one shared `mosaic_tiles_for_aoi(...)` used by the API, the Egypt CLI, and integration tests.
3. Validate the time series **once** per request and pass the validated frame into each method (currently rebuilt ~6 times per call).
4. Split Pydantic schemas into `app/api/schemas.py` and type the response envelope.
5. Standardize all routes on `/api/v1`.

### P2 — Make long runs asynchronous

Implemented for all three analysis endpoints (`POST /analysis/trend`, `POST /compare/variables`, `POST /compare/regions`, plus `GET /jobs/{job_id}` and `/result`), using Redis + RQ.

1. ~~`POST /api/v1/analysis/jobs` → `{job_id}`; `GET /api/v1/analysis/jobs/{job_id}` → status/progress/result.~~ (all analysis jobs)
2. ~~Run collection in a bounded executor (or a task queue such as Celery/RQ if concurrency grows).~~ RQ worker added
3. Add `max_range_days` and `max_grid_pixels` guards. — date range and region count guarded; AOI pixel cap still missing
4. ~~Persist job state and artifacts under a per-job directory.~~ `data/jobs/{job_id}/`
5. Replace the per-day search loop with one temporal-range query grouped by date (365 metadata round-trips → 1). — done for the new variable adapters; MOD11A1 still searches per day

### P3 — Infrastructure and operations

1. Structured logging with request/job IDs.
2. Environment-based settings: `DATA_DIR`, `MAX_AOI_AREA`, `MAX_RANGE_DAYS`, `CACHE_TTL`.
3. CORS allowlist, API key or rate limiting for public deployment.
4. Dev image exists (`Dockerfile` + `docker-compose.yml`, conda-forge GDAL with HDF4); add a production image (multi-stage, non-root, no `--reload`) and CI that runs `-m "not integration"`.
5. Cache analysis results keyed by `(product, aoi, start, end, resolution, methods)`.
6. Parallelize tile processing with a process pool once memory is bounded.

**If you only do one thing:** P1.1 + P0.1-3. They are low-risk, require no new infrastructure, and stop the codebase from accumulating a second divergent implementation every time a method or product is added.

---

## Repository hazards

- **Leave sibling trees alone:** `D:\assignments\backend\EALive\` belongs to a different codebase from another machine. It is out of scope for this project; do not treat it as a copy, do not sync changes with it, and do not edit it.
- **Run location matters:** tests import `app.*` and run from `D:\assignments\backend\backend`; the `try: from app... except ModuleNotFoundError: from backend.app...` shims still allow importing from the parent directory. Packaging will remove this ambiguity.
- **VCS:** this directory is a git repository, so `git status`/`git diff` are the source of truth for what changed.

---

## Further reading

- [`docs/statistical_methods.md`](docs/statistical_methods.md) — standard vs modified Mann-Kendall, seasonal MK, OLS vs HAC, when results should be marked inconclusive, and the scientific limitations of the current implementation.
