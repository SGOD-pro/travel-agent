"""Main FastAPI application entrypoint."""

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response

from config.config import settings
from functions.api.routes.benchmarks import router as benchmarks_router
from functions.api.routes.exports import router as exports_router
from functions.api.routes.health import router as health_router
from functions.api.routes.jobs import router as jobs_router
from functions.api.routes.optimization import router as optimization_router
from functions.api.routes.support import router as support_router
from functions.api.routes.trips import router as trips_router

logger = logging.getLogger("api.timing")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Travel Planning Platform API",
        description="India-first travel planning, optimization, and comparison platform",
        version="0.1.0",
        debug=settings.DEBUG,
    )

    @app.middleware("http")
    async def add_timing_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start_time = time.perf_counter()
        status_code = 500
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            if "response" in locals():
                response.headers["X-Process-Time-Ms"] = f"{duration_ms:.2f}"
            logger.info(
                "API %s %s completed in %.2fms (status %d)",
                request.method,
                request.url.path,
                duration_ms,
                status_code,
            )

    # CORS configuration for Next.js frontend
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Process-Time-Ms"],
    )

    app.include_router(health_router)
    app.include_router(trips_router)
    app.include_router(jobs_router)
    app.include_router(optimization_router)
    app.include_router(exports_router)
    app.include_router(benchmarks_router)
    app.include_router(support_router)

    return app


app = create_app()
