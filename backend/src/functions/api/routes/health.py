"""Health and readiness probe routes."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "travel-backend",
        "version": "0.1.0",
    }
