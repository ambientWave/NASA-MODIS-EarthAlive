from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import analysis, comparison, health, jobs, variables

# ``app/main.py`` -> repository root (the directory that contains ``flutterweb/``).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
FLUTTER_WEB_DIR = PROJECT_ROOT / "flutterweb" / "web"
FLUTTER_INDEX = FLUTTER_WEB_DIR / "index.html"


app = FastAPI(
    title="Earth Alive API",
    description="NASA Earth Science Data Analysis Platform",
    version="1.0.0"
)


# Allow Flutter to communicate with the backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Register API routes
app.include_router(
    health.router,
    prefix="/api/v1"
)

app.include_router(
    variables.router,
    prefix="/api/v1"
)

app.include_router(
    analysis.router,
    prefix="/api/v1"
)

app.include_router(
    comparison.router,
    prefix="/api/v1"
)

app.include_router(
    jobs.router,
    prefix="/api/v1"
)


@app.get("/")
def root():

    return {
        "message": "Welcome to Earth Alive API",
        "version": "1.0.0",
        "documentation": "/docs"
    }


@app.get("/home", include_in_schema=False)
def flutter_home() -> FileResponse:
    """Serve the Flutter web entrypoint.

    The build's ``index.html`` uses ``<base href="/">``, so its assets
    (``main.dart.js``, ``assets/``, ``canvaskit/``, ...) are requested from the
    server root. Those are served by the static mount below; this route only
    exposes the same entrypoint at ``/home``.
    """
    if not FLUTTER_INDEX.is_file():
        raise HTTPException(
            status_code=503,
            detail=(
                "Flutter web build not found. Run `flutter build web` so that "
                f"'{FLUTTER_INDEX}' exists."
            ),
        )
    return FileResponse(
        FLUTTER_INDEX,
        media_type="text/html",
        # The entrypoint must never be cached across rebuilds; the hashed asset
        # files are safe to cache normally.
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# Registered last so every API route (and ``/``) wins over the static mount.
# ``check_dir=False`` keeps the app importable before the first web build.
app.mount(
    "/",
    StaticFiles(directory=FLUTTER_WEB_DIR, html=True, check_dir=False),
    name="flutter-web",
)
