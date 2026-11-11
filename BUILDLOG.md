I read every module, both docs, and all five test files in D:\assignments\backend\backend. Here's what the codebase actually is, how work flows through it, the conventions worth copying, and where I'd optimize.
What it is
A single FastAPI backend for "Earth Alive" — a NASA Earth-science analysis platform. It fetches MOD11A1 (MODIS daily land-surface temperature) granules from NASA Earthdata, mosaics/crops them to a user AOI, builds a temperature time series, and runs trend statistics on it. There's a Flutter client implied (the CORS comment), but no code for it here. The sibling `EALive/` tree belongs to a different codebase from another machine and is not part of this project.
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
- (The sibling `EALive/` tree is from a different codebase and is out of scope.)
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

A full reference for the project: stack, layout, quick start, API reference, architecture, workflows, both record schemas, extension conventions and recipes, configuration/data directories, testing matrix, known issues (correctness, scale, hygiene), a P0–P3 optimization roadmap, and repository hazards (run-location ambiguity, VCS state).

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

## 2026-11-11 — Path utilities and parameterized `raster_processor`

`app/services/raster_processor.py` hardcoded input/output paths and ended with an interactive `plt.show()`, so it could not run headless or be tested against fixtures.

Added reusable filesystem helpers in `app/utils/paths.py`:

| Helper | Behaviour |
| --- | --- |
| `ensure_directory(path)` | Creates the directory and missing parents; idempotent; returns the `Path`. |
| `ensure_parent_directory(file_path)` | Creates only the parent of a file path; returns the unchanged file `Path`. |
| `require_file(path, description)` | Raises `FileNotFoundError` with the resolved path and a human-readable description. |

Rewrote `app/services/raster_processor.py`:

- Every path is now an argument: `analyze_lst_quality(lst_path, qc_path)` and `plot_temperature(lst_path, qc_path, output_path, title)`.
- Defaults still exist, but are project-rooted via `PROCESSED_DATA_DIR` (`D:\assignments\backend\data\processed`), not relative to the current working directory.
- Missing inputs fail early through `require_file`; the heatmap output directory is created through `ensure_parent_directory`.
- `analyze_lst_quality` returns a statistics dict (`total_pixels`, `valid_lst_pixels`, `good_quality_pixels`, min/mean/max °C) instead of only printing.
- Uses the Matplotlib `Agg` backend and closes figures; no `plt.show()`, so it is safe in CI and on a server.
- Added a CLI: `python -m app.services.raster_processor --lst ... --qc ... --output ... --title ...` plus `--stats-only` / `--plot-only`.

Tests: added `tests/test_raster_processor.py` (8 tests) covering directory creation, parent creation, `require_file` errors, absolute project-rooted defaults, statistics from fixture GeoTIFFs, missing-input handling, output-directory creation, and CLI parsing. All 8 pass.

Also updated the API tests to the current mount point: `analysis` was moved from `/api` to `/api/v1` in `app/main.py`, so `tests/test_api_analysis.py` now calls `/api/v1/analysis/trend`.

Suite result from `backend/backend`:

```powershell
python -m pytest tests -m "not integration" -q
# 56 passed, 2 failed, 3 deselected
```

The two failures remain the artifact-dependent `test_modis_validation.py` cases described below.

Note: the rewrite of `app/main.py` also dropped the unversioned `/health` alias, so external liveness probes will 404 again. The canonical `/api/v1/health/` route is unaffected. Restoring the alias is pending a decision — it was not re-added silently.

---

## 2026-11-11 — Diagnosed the "No valid observations" trend failure

`POST /api/v1/analysis/trend` returned `404 {"detail":"No valid observations: No valid non-missing observations remain for statistical analysis."}`. The saved series `data/processed/time_series/trend_api_modis_timeseries_daily.json` had 31 January records, all `missing_observation: true` with `valid_pixels: 0` and `granules_used: []`.

Root cause: **`gdal_translate` was not available to the server**, so HDF4 extraction failed for every granule. Reproducing one granule directly gave:

```text
warnings: ['Could not process MOD11A1.A2024001.h20v06.061.2024005035508.hdf: [WinError 2] The system cannot find the file specified']
```

`process_aoi_for_date` caught that `FileNotFoundError`, returned `valid_pixels=0`, and `collect_mod11a1_timeseries` then replaced the detailed record with a generic `build_missing_observation_record(..., "No valid pixels after QA filtering.")`. The actual cause was therefore invisible in both the JSON artifacts and the API response.

Changes:

- `app/services/modis_processor.py`: added `find_gdal_translate()` (checks `GDAL_TRANSLATE`, then `PATH`) and an actionable `RuntimeError` when GDAL is absent; `extract_subdataset_to_tiff` now uses it.
- `app/services/modis_timeseries.py`: `collect_mod11a1_timeseries` fails fast before downloading when GDAL is missing; `build_missing_observation_record` now preserves `warnings`, `granules_used`, and `total_pixels`, and the collector passes the real per-granule warnings into `missing_reason`.
- `app/api/routes/analysis.py`: maps a missing-GDAL `RuntimeError` to `503` with installation guidance; otherwise includes the distinct `missing_reason` values in the `404` detail.

Verified against the running server:

```text
HTTP 503
{"detail":"Processing environment is not configured: gdal_translate was not found. ..."}
```

Tests added: environment override for `GDAL_TRANSLATE`, actionable error when GDAL is missing, warning/granule preservation in missing records, `503` for missing GDAL, and `404` reasons for an all-missing series. Suite: **61 passed, 2 failed, 3 deselected** (the two remaining failures are the artifact-dependent `test_modis_validation.py` cases).

Note: a prior run in the sibling `EALive` tree (a different codebase) produced valid January values (e.g. 23.40 °C on 2024-01-01) with the same QA logic, so no QA or scaling change was needed here.

---

## 2026-11-11 — Docker Compose dev container

Added a containerized dev setup, requested as a Compose file on `python:3.14-alpine` that installs `requirements.txt` and runs `uvicorn app.main:app --reload`.

Files:

- `Dockerfile` — `python:3.14-alpine`; installs `gdal`/`gdal-tools` plus build dependencies, installs the pinned requirements, and defaults to `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`. The working directory is `/srv/backend/backend` so the project-rooted paths (`.parents[3]` in `modis_processor.py`) resolve to `/srv/backend/data`, matching the host layout `backend/backend` under `backend/`.
- `docker-compose.yml` — builds the image, publishes `8000:8000`, injects `.env` via `env_file` (optional), bind-mounts `./app` for reload and `./data` for artifacts, sets `WATCHFILES_FORCE_POLLING=true` for Docker Desktop bind mounts, and health-checks `/api/v1/health/`.
- `.dockerignore` — excludes `.env`, `data/`, caches, tests, and docs from the build context.

Checked with `docker compose config --quiet` (passes). The image was **not built** because the Docker Desktop daemon was not running in this session.

Caveats recorded:

- `requirements.txt` is UTF-16 LE (from `pip freeze` in PowerShell). pip's requirements parser detects the BOM, so it works as-is in the image.
- Alpine's `gdal` has **no HDF4 driver**, so MOD11A1 `.hdf` extraction will fail in this image. `gdal_translate` exists, so the failure surfaces as a `404` with the gdal format error rather than the missing-binary `503`.

---

## 2026-11-11 — Docker base switched to conda-forge GDAL (HDF4)

The Alpine image could not process MOD11A1: Alpine's `gdal` package has no **HDF4** driver, so `gdal_translate` rejects HDF4-EOS granules.

Replaced `FROM python:3.14-alpine` with `mambaorg/micromamba:2.10.0-debian13-slim`, then install from conda-forge:

```dockerfile
RUN micromamba install -y -n base -c conda-forge \
        python=3.14 pip gdal libgdal-hdf4
```

Why this combination:

- `libgdal-hdf4` (GDAL ≥ 3.9.1 plugin) is the supported way to get HDF4/HDF4-EOS from conda-forge.
- Debian/Ubuntu do not package an HDF4-enabled GDAL (`libgdal-hdf4` does not exist there), so `python:3.14-slim` was not an option without a source build.
- The micromamba Debian-slim base is smaller than the miniforge/conda base images, while still giving a conda-forge GDAL.

Other changes:

- Build now asserts HDF4 support: `gdalinfo --formats | grep -qi hdf4` (and `gdal_translate --version`), so a bad GDAL fails the build rather than a request.
- `GDAL_TRANSLATE=/opt/conda/bin/gdal_translate` is set in the image and in Compose.
- The `/srv/backend/backend` workdir and `./data:/srv/backend/data` mount are unchanged, so project-rooted paths still resolve correctly.
- `.dockerignore` still excludes `.env`, `data/`, tests, and docs.

`docker compose config --quiet` passes. The image was still not built here because the Docker Desktop daemon is not running, so the conda/pip install itself is unverified.

### Fix: exit 127 during the first build attempt

The first real build failed on the `micromamba install ... && gdal_translate --version` step with exit code **127** (command not found). Cause: the micromamba image sets `SHELL ["/usr/local/bin/_dockerfile_shell.sh"]`, and that wrapper only activates the conda environment when `MAMBA_DOCKERFILE_ACTIVATE=1`. Without it, `/opt/conda/bin` is never added to `PATH`, so `gdal_translate`/`gdalinfo` are not found even though the install succeeded.

Applied:

- `ARG MAMBA_DOCKERFILE_ACTIVATE=1` before the install `RUN`.
- Explicit `ENV PATH=/opt/conda/bin:$PATH` so pip and uvicorn resolve without relying on the entrypoint.
- Build-time HDF4 assertion kept (`gdalinfo --formats | grep -qi hdf4`).

### Python version correction

The micromamba-based image was rejected after inspection showed the interpreter was not Python 3.14 (reported as 3.10): installing conda's `gdal` package makes conda choose the Python build, and the version that lands in the image is whatever the solver picks among the package's ABI builds — not something the Dockerfile should leave implicit.

Replaced the micromamba base with `python:3.14.10-slim-trixie` (official image, pinned patch level) and installed only GDAL's non-Python parts:

```dockerfile
RUN micromamba create -y -p /opt/gdal -c conda-forge --no-pyc \
        libgdal-core libgdal-hdf4
```

- `libgdal-core` has no Python dependency and ships `gdal_translate`/`gdalinfo`.
- `libgdal-hdf4` adds the HDF4/HDF4-EOS plugin (depends on `libgdal-core` + `hdf4`).
- micromamba itself is a 19 MB static binary downloaded for the build step and deleted afterwards.
- Build assertions: `python --version | grep -q 3.14`, `gdal_translate --version`, and `gdalinfo --formats | grep -qi hdf4`.

This also shrinks the image versus installing conda's Python stack: the official Python 3.14 slim layer replaces conda's Python/openssl/sqlite/etc., and `/opt/gdal` contains only the GDAL runtime and its dependencies.

`GDAL_TRANSLATE` now points at `/opt/gdal/bin/gdal_translate` in both the image and Compose.

### Build attempt 2: exit 141 (SIGPIPE), and a stale Dockerfile

The next reported failure still showed the superseded `micromamba install -n base ... gdal` step, with exit code **141**. Two separate things:

1. **141 = 128 + 13 = SIGPIPE.** The micromamba image's `SHELL` wrapper runs with `set -ef -o pipefail`, so `gdalinfo --formats | grep -qi hdf4` fails the pipeline: `grep -q` exits as soon as it matches, `gdalinfo` is killed by SIGPIPE, and `pipefail` propagates 141. Ironically this only happens when HDF4 *is* present. Fixed by writing the output to a file and grepping the file (`gdalinfo --formats > /tmp/gdal-formats.txt && grep -qi hdf4 ...`), and doing the same for `python --version`.
2. **The pasted step does not exist in the current Dockerfile.** The file on disk is the `python:3.14.10-slim-trixie` + `micromamba create -p /opt/gdal ... libgdal-core libgdal-hdf4` version (only Dockerfile under `D:\assignments\backend`). The output therefore came from a build started before the rewrite; a rebuild picks up the new file.

Rebuild with `docker compose build --no-cache` from `D:\assignments\backend\backend`.

## 2026-11-11 — Container runtime: missing `libexpat.so.1`

The container started but crashed while importing rasterio:

```text
ImportError: libexpat.so.1: cannot open shared object file: No such file or directory
```

Cause: `rasterio` ships a manylinux wheel, and `libexpat` is on the manylinux policy allowlist, so auditwheel does not bundle it — the distro must provide it. The official `python:3.14-slim-trixie` runtime image keeps only libraries actually linked by its own Python build and does not guarantee `libexpat1`.

Fix: install the allowlisted shared libraries explicitly:

```dockerfile
apt-get install -y --no-install-recommends curl libexpat1 libgomp1
```

`libgomp1` is installed for the same reason (`libgomp.so.1` is allowlisted and required by SciPy/OpenBLAS-backed wheels). Debian's `libstdc++6`/`libgcc-s1` are already present, and wheels bundle everything else they need.

---

## Known follow-ups from this work

- **Real data runs are still blocked by GDAL.** `gdal_translate` is not on `PATH`, and the `rasterio` wheel's GDAL has no HDF4 driver, so MOD11A1 `.hdf` subdatasets cannot be extracted. Requires a conda/OSGeo4W GDAL with HDF4, or a `GDAL_TRANSLATE` path pointing at one. The API now returns `503` with that guidance instead of `404` "no valid observations".
- The two artifact-dependent tests should move under the `integration` marker (or skip when the files are absent).
- The sibling `EALive/` tree is a separate codebase from another machine; ignore it rather than keeping it in sync.
- The unversioned `/health` alias was removed by the `app/main.py` rewrite; decide whether to restore it for external probes or keep only `/api/v1/health/`.
- The P0–P3 roadmap in `README.md` is unchanged: unify orchestration, preserve missing periods, fix artifact collisions, then async jobs and guards.