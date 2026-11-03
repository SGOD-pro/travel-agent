# backend/src/functions/api/app.py
"""Main FastAPI application entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.config import settings
from functions.api.routes.benchmarks import router as benchmarks_router
from functions.api.routes.exports import router as exports_router
from functions.api.routes.health import router as health_router
from functions.api.routes.jobs import router as jobs_router
from functions.api.routes.support import router as support_router
from functions.api.routes.trips import router as trips_router
from functions.api.routes.v1.media import router as media_router  # Phase 9/11 Media Location API
from functions.optimizer.handler import router as optimizer_router    # Phase 10 Route Optimizer API


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

    # Register core route handlers
    app.include_router(health_router)
    app.include_router(trips_router)
    app.include_router(jobs_router)
    app.include_router(exports_router)
    app.include_router(benchmarks_router)
    app.include_router(support_router)
    
    # Register newly integrated Phase 9/11 & Phase 10 routers
    app.include_router(media_router)
    app.include_router(optimizer_router)

    return app


app = create_app()