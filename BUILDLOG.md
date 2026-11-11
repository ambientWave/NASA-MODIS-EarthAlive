I read every module, both docs, and all five test files in D:\assignments\backend\backend. Here's what the codebase actually is, how work flows through it, the conventions worth copying, and where I'd optimize.
What it is
A single FastAPI backend for "Earth Alive" — a NASA Earth-science analysis platform. It fetches MOD11A1 (MODIS daily land-surface temperature) granules from NASA Earthdata, mosaics/crops them to a user AOI, builds a temperature time series, and runs trend statistics on it. There's a Flutter client implied (the CORS comment), but no code for it here. The parent folder also holds EALive/backend, a byte-identical duplicate of this tree (I hashed the key files) that is the only place pytest.ini exists.
app/
  main.py                     app + CORS + router mounting
  api/routes/{health,variables,analysis}.py
  services/
    modis_processor.py        Earthdata search/download + GDAL + raster helpers + Egypt CLI
    modis_timeseries.py       per-date collection, cache, aggregation, CSV/JSON output
    statistical_analysis.py   validation + MK / modified MK / seasonal MK / OLS / HAC / Welch
    modis_downloader.py       DEAD duplicate of modis_processor search/download
    raster_processor.py       LEGACY script, hardcoded paths, not imported by the app
docs/statistical_methods.md   methodology rationale (good, keep it in sync)
tests/                        4 unit/contract files + 2 network integration tests
How work actually flows
The one live path — POST /api/analysis/trend:
1. Pydantic validates TrendAnalysisRequest (literals, ranges) → 422 on bad shape.
2. The handler re-validates the same things manually → 400 (analysis.py:181-215). Most of that is unreachable because the Literal/Field constraints already reject it.
3. Builds TimeSeriesConfig, which validates again (AOI ordering, product whitelist, aggregation) in modis_timeseries.py:60-81.
4. collect_mod11a1_timeseries → authenticate → loop one HTTP search per calendar day → dedupe tiles → check JSON granule cache → download → 2× gdal_translate subprocesses per granule → QA mask → K→°C → bilinear reproject onto a 0.01° grid → mosaic → summarize → record; monthly/yearly then re-aggregates → writes CSV+JSON to data/processed/time_series/trend_api_*.
5. _as_stats_records (analysis.py:88) translates the time-series vocabulary into the statistics vocabulary — this is the seam between two canonical record shapes:
- collector shape: mean_temperature_celsius, valid_pixels, missing_observation
- stats shape: mean_lst, valid_pixel_count, missing
6. validate_time_series (DataFrame, duplicate-date rejection, coercion, coverage/missing warnings) → dispatch methods → merge warnings → return {success, metadata, data_quality, analysis_results, visualizations, warnings}.
A second, richer path that the API doesn't use. analyze_time_series (statistical_analysis.py:702-818) is the real orchestrator: standard + modified MK, seasonal MK, OLS, HAC, optional Welch, auto-selected method, scientific limitations, charts, and a JSON artifact. The route reimplements a smaller, divergent version of that dispatch inline (analysis.py:261-289). Any new statistical feature added to one path will silently not appear in the other. visualizations is always {} in the API even though generate_analysis_visualizations exists.
A third and fourth copy of the raster pipeline. The tile→mask→reproject→mosaic loop is implemented three times: modis_processor.run_modis_egypt_pipeline:170, modis_timeseries.process_aoi_for_date:220, and the test helper _run_aoi_pipeline in tests/test_global_aoi.py:44. Only the second one is production.
Tests. Unit tests hit pure helpers and services with tmp_path. Integration tests are marked but run by default in this copy (no pytest.ini here), so pytest will attempt live NASA downloads. Tests import backend.app.*, meaning they must be run from D:\assignments\backend, not from the working directory.
Conventions to copy when adding features
- Service = module-level functions operating on plain dicts, no classes except config dataclasses. Config dataclasses validate and coerce Paths in __post_init__.
- Every analysis returns a dict with an explicit status (calculated / insufficient_data / not_applicable / not_supported), plus warnings and assumptions. Insufficient data is never raised from the statistics layer — only from validate_time_series.
- HTTP concerns stay in routes: validate → build config → call one service function → translate exceptions (TimeoutError→504, ConnectionError/OSError→503, ValueError→404) → typed envelope.
- All artifacts go under data/{raw,processed}/...; tests use tmp_path.
- Tests monkeypatch the service function by dotted string path, e.g. backend.app.api.routes.analysis.collect_mod11a1_timeseries.
- Docstrings/docs carry the statistical caveats. docs/statistical_methods.md is treated as a contract — update it when methods change.
Recipes:
- New product (NDVI/MOD13Q1): new <product>_timeseries.py with its own config + QA/scale constants (don't reuse MOD11A1's bits), emit the same canonical collector record shape, add to SUPPORTED_PRODUCTS, extend _as_stats_records, add a unit field to metadata. The current envelope hardcodes "temperature" naming in places.
- New analysis method: pure (records) -> dict in statistical_analysis.py → add to the Literal, SUPPORTED_ANALYSIS_METHODS, and the dispatch → add a test → document assumptions.
- New endpoint: one router module per domain, prefix declared on the router, mounted in main.py. But fix the versioning first — analysis is mounted at /api while everything else is /api/v1 (main.py:28-41), so the main feature is the odd one out.
Problems that matter
Correctness / contract
- SUPPORTED_* validation and manual AOI/date checks duplicate Pydantic; dead code paths that will drift.
- paired_period_t_test (statistical_analysis.py:584) is a Welch unpaired test. The name is wrong and will mislead the next person.
- aggregate_period_records (modis_timeseries.py:271-276) drops periods that are entirely missing. A 12-month request with 6 empty months reports observation_count = 6 and missing_observation_percentage = 0.0 — the data-quality summary hides the gap. That's a scientific-correctness bug, not cosmetic.
- When a date has no valid pixels, collect_mod11a1_timeseries throws away the obs_record it just built (including its warnings, line 394-400) and appends a generic record.
- Overlapping tiles are merged last-wins (np.where at :260), not averaged → visible seams between h3 tile boundaries.
- kendall_tau uses the tau-a denominator (ignores the tie correction that's computed for var_s), so reported tau is slightly wrong on tied series.
- visualizations is hardcoded {}; output_files is discarded by the route.
Scale / robustness — the big one
- A 1-year monthly request synchronously performs ~365 sequential Earthdata searches + downloads + 2 GDAL subprocesses per granule, inside the request. That's minutes-to-hours on one HTTP call, holding an anyio threadpool slot. TimeoutError is mapped to 504 but nothing ever raises it, so the 504 branch is decorative. There is no max date span.
- No cap on AOI size: destination_grid_size with fixed 0.01° means a near-global bbox allocates ~2.5 GB per array, times mosaic copies — a one-line request can OOM the server.
- Output filenames are fixed to region_name="trend_api", so every AOI and every concurrent request overwrites the same CSV/JSON. No AOI/date in the cache key.
- GranuleCache rewrites the whole JSON per granule and has no locking — concurrent requests corrupt it.
- Intermediate per-granule TIFs are never cleaned up; disk grows unbounded. No cache TTL.
- No logging anywhere, no request IDs — a slow job is invisible.
Hygiene
- requirements.txt omits rasterio (imported in two modules), pytest, and httpx; pins nothing; GDAL is an undocumented system dependency.
- No pyproject.toml/conftest.py/pytest.ini; the import shims (try: from app... except ModuleNotFoundError: from backend.app...) are copy-pasted into three modules to paper over the missing package install.
- Two identical copies of the whole codebase (backend/backend and EALive/backend).
- modis_downloader.py is dead duplicate code; raster_processor.py is a legacy script with hardcoded paths, Arabic comments, and plt.show().
- allow_origins=["*"], no auth, no rate limiting, no Dockerfile.
- Pydantic v2 deprecated example= in Field; response models are dict[str, Any], so OpenAPI documents nothing and the client contract is informal.
What I'd optimize, in order
1. Unify the orchestration and the mosaic loop (highest leverage, medium effort). Delete modis_downloader.py; make the route call analyze_time_series; extract one mosaic_tiles_for_aoi(bounds, resolution, files) used by the timeseries path and the Egypt CLI. That removes three-way drift and gives you exactly one place to add methods.
2. Make long runs asynchronous. POST /analysis/jobs → returns job_id; a bounded ThreadPoolExecutor or FastAPI BackgroundTasks runs the collector; GET /analysis/jobs/{id} polls. Add max_range_days and max_grid_pixels guards regardless. This is the difference between "works on the demo laptop" and "usable".
3. Fix the correctness items above — missing-period preservation in aggregation, cache-key/AOI collision, tile averaging, waveform naming, tile warnings loss. Small diffs, real impact on trust in the numbers.
4. Validate once. validate_time_series currently rebuilds the DataFrame ~6 times per request (each method calls it, and modified MK calls standard MK again). Pass the validated frame down. Same result, fraction of the work, and it makes adding methods cheap.
5. Replace per-day search with one range query, group results by tile date. Turns 365 metadata round-trips into one; keep the per-day loop only for processing.
6. Packaging + CI discipline. pyproject.toml with app as a package, pytest.ini with the integration marker and addopts = -m "not integration", pinned deps, .gitignore for data/ and __pycache__, Docker image with GDAL. Then delete the import shims.
7. Infra hardening last: structured logging, typed response models split into app/api/schemas.py, /api/v1 everywhere, config via env (DATA_DIR, MAX_AOI_AREA, CACHE_TTL), CORS allowlist, API key or at least rate limiting.
My default recommendation if you only do one thing: #1 + #3. They're low-risk, they don't require new infrastructure, and they stop the codebase from accumulating a second divergent implementation every time you add a method or a product.
Two things I'd like to know before writing anything down: what feature are you planning next (new product, new statistic, new endpoint, or frontend wiring)? That changes whether the async-job migration should come before or after. And do you want me to write this up as a plan file in C:\Users\CreepingHeadache\.opencode\plan? I can't modify the repo itself while I'm in plan mode — you'd need to switch agents for that.

---

# Build Log

## 2026-11-11 — Environment repair and codebase documentation

### Diagnosed `ModuleNotFoundError: No module named 'backend'`

The reported error was a masked failure. The actual cause was a missing third-party package:

- `venv_earthalive` had FastAPI, NumPy, pandas, SciPy, and earthaccess, but **not `rasterio`**.
- `app/services/modis_processor.py` imports `rasterio`; that failed, and the `except ModuleNotFoundError` import shims in `main.py`, `analysis.py`, and `modis_timeseries.py` caught it and retried as `backend.app...`, producing the misleading `No module named 'backend'`.

Changes:

- Installed into `D:\assignments\backend\venv_earthalive`: `rasterio 1.5.2` (bundled GDAL 3.12.2), `pytest 9.2.0`, `httpx 0.28.1`.
- Hardened the import shims in `app/main.py`, `app/api/routes/analysis.py`, and `app/services/modis_timeseries.py` so they only fall back when `exc.name` is `app` or `backend`, and re-raise every other missing module.
- `requirements.txt` now pins the full environment (pip freeze), including `rasterio`, `pytest`, `httpx`, and `python-dotenv`.

Verified: app imports from both roots, `GET /api/v1/health/` returns 200.

### Added `README.md`

A full reference for the project: stack, layout, quick start, API reference, architecture, workflows, both record schemas, extension conventions and recipes, configuration/data directories, testing matrix, known issues (correctness, scale, hygiene), a P0–P3 optimization roadmap, and repository hazards (the byte-identical `EALive/backend` copy, run-location ambiguity, no VCS).

---

## 2026-11-11 — NASA Earthdata credentials from `.env`

### New files

| File | Purpose |
| --- | --- |
| `app/core/__init__.py` | Configuration package marker |
| `app/core/config.py` | Loads `.env` at import, exposes validated `Settings` via `get_settings()` |
| `app/services/earthdata_auth.py` | Shared credential-aware `ensure_earthaccess_auth()` |
| `.env.example` | Credential template to copy to `.env` |
| `.gitignore` | Ignores `.env`, `data/`, `__pycache__`, venvs, pytest cache |
| `tests/test_earthdata_config.py` | 11 tests for settings, precedence, strategy selection, error handling |

### Modified files

- `app/services/modis_processor.py` — re-exports `ensure_earthaccess_auth` from the shared module; removed the bare `earthaccess.login()`.
- `app/services/modis_downloader.py` — uses the same shared auth; removed the old version that swallowed every exception and returned `None`.
- `README.md` — updated quick start, project layout, configuration, and hygiene notes.

### Behavior

- Credentials come from `EARTHDATA_USERNAME` + `EARTHDATA_PASSWORD`, or `EARTHDATA_TOKEN`.
- Optional `EARTHDATA_AUTH_STRATEGY`: `environment` | `netrc` | `all` | `interactive`.
- Default strategy is `environment` when credentials are present, otherwise `netrc`. The default is never `all`, because `all` can fall through to an interactive prompt and hang an HTTP request.
- Precedence follows `python-dotenv`'s `override=False`: real environment variables (shell, CI, container) beat `.env`.
- A half-set username/password pair or an unknown strategy fails fast with a clear `ValueError`.
- Authentication failures raise `ConnectionError`, which the analysis route maps to `503` with an actionable message.
- `.env` is loaded once at import of `app.core.config`, so `app.main` picks it up before any login.

### Verification

- 11/11 auth tests pass.
- End-to-end `.env` load checked in a subprocess: username/password read from the file, strategy resolves to `environment`.
- An unauthenticated request to the trend endpoint now returns a clear `503` (`No .netrc found...`) instead of failing silently.

---

## 2026-11-11 — Test harness alignment

- Added `pytest.ini` with `testpaths = tests` and registration of the `integration` marker.
- Standardized test imports on `app.*` (matching `uvicorn app.main:app`) and aligned the run root to `backend/backend`.
- Fixed the stale monkeypatch target in `tests/test_api_analysis.py`: `backend.app.api.routes.analysis.collect_mod11a1_timeseries` → `app.api.routes.analysis.collect_mod11a1_timeseries`. The stale path meant the real collector ran, hitting live auth.

Suite result from `backend/backend`:

```powershell
python -m pytest tests -m "not integration" -q
# 48 passed, 2 failed, 3 deselected
```

The two failures are `test_modis_validation.py::test_final_egypt_raster_is_wgs84_and_cropped_to_aoi` and `test_report_contains_required_scientific_summary_fields`; both assert on `data/processed/egypt/*` artifacts that do not exist in this copy. They are artifact-dependent, not code failures, and should be marked/skipped.

---

## 2026-11-11 — Unversioned health endpoint

Liveness probes were calling `GET /health` and receiving 404; the only health route was `/api/v1/health/` (router prefix `/health` + mount prefix `/api/v1`).

- Added `/health` and `/health/` aliases in `app/main.py` via `app.add_api_route(path, health.health_check, include_in_schema=False)`, reusing the existing handler so the payload stays identical.
- Verified `200 success` for `/health`, `/health/`, and `/api/v1/health/`; the three endpoint tests still pass.

---

## Known follow-ups from this work

- **Real data runs are still blocked by GDAL.** `gdal_translate` is not on `PATH`, and the `rasterio` wheel's GDAL has no HDF4 driver, so MOD11A1 `.hdf` subdatasets cannot be extracted. Requires a conda/OSGeo4W GDAL with HDF4, or a `pyhdf`-based reader.
- The two artifact-dependent tests should move under the `integration` marker (or skip when the files are absent).
- The duplicate `EALive/backend` tree still exists and is byte-identical; pick a canonical copy.
- The P0–P3 roadmap in `README.md` is unchanged: unify orchestration, preserve missing periods, fix artifact collisions, then async jobs and guards.