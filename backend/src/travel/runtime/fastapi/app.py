"""Main FastAPI application entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from travel.infrastructure.config import settings
from travel.runtime.fastapi.routes.benchmarks import router as benchmarks_router
from travel.runtime.fastapi.routes.exports import router as exports_router
from travel.runtime.fastapi.routes.health import router as health_router
from travel.runtime.fastapi.routes.support import router as support_router
from travel.runtime.fastapi.routes.trips import router as trips_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Travel Planning Platform API",
        description="India-first travel planning, optimization, and comparison platform",
        version="0.1.0",
        debug=settings.DEBUG,
    )

    # CORS configuration for Next.js frontend
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(trips_router)
    app.include_router(exports_router)
    app.include_router(benchmarks_router)
    app.include_router(support_router)

    return app


app = create_app()
