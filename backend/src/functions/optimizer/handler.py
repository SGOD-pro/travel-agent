"""FastAPI / Task Queue handlers for the Route Optimizer."""
from typing import Annotated

from fastapi import APIRouter, Depends

from middleware.auth import AuthenticatedUser, get_current_user
from middleware.rate_limit import public_request_rate_limiter

from .schemas import RouteRequest, RouteResponse
from .service import RouteOptimizerService

router = APIRouter(prefix="/api/v1/optimizer", tags=["Route Optimizer"])


@router.post("/route", response_model=RouteResponse)
async def optimize_route_endpoint(
    req: RouteRequest,
    _: Annotated[AuthenticatedUser, Depends(get_current_user)],
    __: Annotated[None, Depends(public_request_rate_limiter)],
) -> RouteResponse:
    """Endpoint to optimize a multi-stop route."""
    return await RouteOptimizerService.optimize_route(req)
