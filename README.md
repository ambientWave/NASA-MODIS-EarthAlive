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
│   │   └── paths.py                  # ensure_directory / ensure_parent_directory / require_file
│   ├── api/
│   │   └── routes/
│   │       ├── health.py             # GET /api/v1/health/
│   │       ├── variables.py          # GET /api/v1/variables/
│   │       └── analysis.py           # POST /api/v1/analysis/trend
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
- `.env` is injected through `env_file` (missing file is tolerated).
- Health is polled through `curl` against `/api/v1/health/`.
- The image mirrors the local layout (`/srv/backend/backend/app`) so project-rooted data paths resolve to `/srv/backend/data`.
- The build asserts HDF4 support with `gdalinfo --formats | grep -qi hdf4`, so a broken GDAL fails the build instead of at request time.

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
| POST | `/api/v1/analysis/trend` | Run MODIS LST acquisition + trend analysis |
| GET | `/api/v1/analysis/health` | Analysis service liveness |


### `POST /api/v1/analysis/trend`

Request body:

```json
{
  "product": "MOD11A1",
  "aoi": { "west": 24.7, "south": 22.0, "east": 36.9, "north": 31.7 },
  "start_date": "2024-01-01",
  "end_date": "2024-12-31",
  "temporal_resolution": "monthly",
  "analysis_methods": ["linear_regression", "mann_kendall", "t_test"]
}
```

- `temporal_resolution`: `daily` | `monthly` | `yearly`
- `analysis_methods`: `linear_regression`, `mann_kendall`, `modified_mann_kendall`, `seasonal_mann_kendall`, `t_test`

Response envelope:

```json
{
  "success": true,
  "metadata": { "product": "...", "temporal_resolution": "...", "start_date": "...", "end_date": "...", "aoi": {} },
  "data_quality": { "observation_count": 12, "valid_observation_count": 12, "missing_observation_percentage": 0.0, "data_coverage_percentage": 98.3, "warnings": [] },
  "analysis_results": { "linear_regression": {}, "mann_kendall": {} },
  "visualizations": {},
  "warnings": []
}
```

Status codes: `400` invalid input, `404` no data / insufficient observations, `503` Earthdata connection failure, `504` processing timeout, `500` unexpected error.

---

## Architecture

The codebase is a two-layer design:

1. **API layer** (`app/api/routes/*`) owns HTTP concerns: Pydantic request models, validation, exception-to-status translation, and the response envelope.
2. **Service layer** (`app/services/*`) owns everything else: remote data acquisition, raster processing, aggregation, persistence, and statistics. Services operate on plain dictionaries and module-level functions.

There is no dependency-injection container, no repository/database layer, and no persistent job store. State lives in the filesystem under `data/` and in an in-memory granule cache file.

Flow at a glance:

```
HTTP request
   → Pydantic validation (422)
   → route-level validation (400)
   → TimeSeriesConfig (dataclass validation)
   → collect_mod11a1_timeseries
        → earthaccess search (per day)
        → GranuleCache (JSON on disk)
        → earthaccess download
        → gdal_translate ×2 per granule
        → QA mask → Kelvin→Celsius → reproject → mosaic → summarize
        → aggregate to daily/monthly/yearly
        → write CSV + JSON to data/processed/time_series/
   → _as_stats_records (schema translation)
   → validate_time_series (pandas)
   → method dispatch (linear, MK, modified MK, seasonal MK, t-test)
   → response envelope
```

---

## Workflows

### 1. Live analysis path (`POST /api/v1/analysis/trend`)

This is the only production path.

1. Pydantic validates `TrendAnalysisRequest` (literal enums, coordinate ranges) → 422 on malformed input.
2. The handler re-validates the same constraints manually → 400 (`app/api/routes/analysis.py:181-215`). Most of this is unreachable because the `Literal`/`Field` constraints already reject the values.
3. `TimeSeriesConfig` validates again (AOI ordering, product whitelist, aggregation) in `app/services/modis_timeseries.py:60-81`.
4. `collect_mod11a1_timeseries` runs the acquisition loop described above. **One Earthdata search per calendar day**, plus downloads and two `gdal_translate` subprocesses per granule.
5. `_as_stats_records` (`analysis.py:88`) translates between two record vocabularies (see [Data schemas](#data-schemas)).
6. `validate_time_series` parses dates, rejects duplicates, separates missing observations, and computes coverage warnings.
7. Requested methods are dispatched inline (`analysis.py:261-289`).
8. Warnings are de-duplicated and returned alongside `metadata` and `data_quality`.

### 2. Full statistics orchestrator (not wired to the API)

`analyze_time_series` in `app/services/statistical_analysis.py:702-818` is the *complete* orchestration: standard and modified MK, seasonal MK, OLS, HAC/Newey-West, optional Welch comparison, automatic method selection, scientific limitations, chart generation, and JSON persistence.

The API route **does not call it**. It reimplements a smaller, divergent subset inline. Net effect: any method added to one path silently does not appear in the other, and `visualizations` is always `{}` in the API even though the chart generator exists.

### 3. Offline / CLI pipelines

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

### 4. Test workflow

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

Example: MOD13Q1 NDVI.

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
| `data/cache/modis_granule_cache.json` | Granule download cache (filename → local path) |
| `data/processed/global_tests/` | Integration-test outputs, per region |

All paths are derived from `Path(__file__).resolve().parents[3]`, which is fragile and cannot be overridden by environment variables. There is no TTL, no cleanup, and no size limit.

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
| `test_api_analysis.py` | Health endpoint, trend endpoint contract, invalid AOI/date rejection |

Missing coverage: route-level error mapping (503/504/404), `_as_stats_records` edge cases, concurrent request behavior, and the full orchestrator's output shape.

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
| 6 | Output filenames are fixed to `region_name="trend_api"`; every AOI and concurrent request overwrites the same CSV/JSON. | `modis_timeseries.py:319-320` |
| 7 | Service returns `output_files`, but the route discards them; `visualizations` is always `{}`. | `analysis.py:302` |
| 8 | `int(record.get("valid_pixels", ...))` raises when the key exists with `None`. | `analysis.py:108` |
| 9 | Manual validation duplicates Pydantic constraints and is largely unreachable. | `analysis.py:192-215` |
| 10 | Response models are `dict[str, Any]`, so OpenAPI documents no result contract. | `analysis.py:69-75` |

### Scale and robustness

| # | Issue | Impact |
| --- | --- | --- |
| 1 | A one-year monthly request performs ~365 sequential Earthdata searches + downloads + GDAL subprocesses **inside one HTTP request**. | Minutes to hours per call; exhausts the threadpool. |
| 2 | No maximum date range. | Trivial denial of service. |
| 3 | No maximum AOI size. A near-global bbox at 0.01° allocates ~2.5 GB per array, multiplied by mosaic copies. | Out-of-memory. |
| 4 | `TimeoutError` maps to 504 but nothing ever raises it. | Dead error path. |
| 5 | Granule cache rewrites the entire JSON per granule with no locking. | Corruption under concurrency. |
| 6 | Intermediate per-granule TIFs are never cleaned; cache has no TTL or size limit. | Unbounded disk growth. |
| 7 | No logging, request IDs, or job visibility. | Long runs are opaque. |

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

1. `POST /api/v1/analysis/jobs` → `{job_id}`; `GET /api/v1/analysis/jobs/{job_id}` → status/progress/result.
2. Run collection in a bounded executor (or a task queue such as Celery/RQ if concurrency grows).
3. Add `max_range_days` and `max_grid_pixels` guards.
4. Persist job state and artifacts under a per-job directory.
5. Replace the per-day search loop with one temporal-range query grouped by date (365 metadata round-trips → 1).

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
