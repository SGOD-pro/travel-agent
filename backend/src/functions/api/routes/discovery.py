"""Public media discovery API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import get_session
from functions.api.schemas import AcceptedJobResponse, MediaDiscoveryRequest, Product
from functions.api.service import accept_composition_job, load_owned_trip_context
from middleware.auth import AuthenticatedUser, get_current_user
from middleware.rate_limit import public_request_rate_limiter

router = APIRouter(prefix="/api/v1/discovery", tags=["discovery"])


@router.post(
    "/media",
    response_model=AcceptedJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def discover_media(
    request: MediaDiscoveryRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    _: Annotated[None, Depends(public_request_rate_limiter)] = None,
) -> AcceptedJobResponse:
    """Queue media discovery and only the additional products explicitly selected."""
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required for job acceptance.",
        )
    payload = request.model_dump(mode="json", exclude_none=True)
    payload["media_url"] = str(request.media_url)
    trip_id = request.trip_id
    if Product.TRAVEL in request.products:
        if trip_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="trip_id is required when travel is selected.",
            )
        version, brief = await load_owned_trip_context(session, trip_id, current_user.id)
        payload.update({"trip_id": str(trip_id), "version": version, "brief": brief})
    elif Product.ROUTE in request.products and request.route_request is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="route_request is required when route is selected without travel.",
        )

    response = await accept_composition_job(
        session=session,
        owner_id=current_user.id,
        operation="discover_media",
        idempotency_key=idempotency_key.strip(),
        payload=payload,
        trip_id=trip_id if Product.TRAVEL in request.products else None,
    )
    return AcceptedJobResponse(**response)
