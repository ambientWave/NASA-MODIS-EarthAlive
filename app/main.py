from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import analysis, health, variables


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
    prefix="/api"
)


@app.get("/")
def root():

    return {
        "message": "Welcome to Earth Alive API",
        "version": "1.0.0",
        "documentation": "/docs"
    }