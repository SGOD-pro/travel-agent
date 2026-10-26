"""FastAPI / Task Queue handlers for the Route Optimizer."""
from fastapi import APIRouter
from .schemas import RouteRequest, RouteResponse
from .service import RouteOptimizerService

router = APIRouter(prefix="/api/v1/optimizer", tags=["Route Optimizer"])


@router.post("/route", response_model=RouteResponse)
async def optimize_route_endpoint(req: RouteRequest):
    """Endpoint to optimize a multi-stop route."""
    return await RouteOptimizerService.optimize_route(req)