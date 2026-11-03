"""Authenticated API identity endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from middleware.auth import AuthenticatedUser, get_current_user

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class AuthenticatedUserResponse(BaseModel):
    id: str
    name: str
    email: str
    auth_mode: str = "oauth"


@router.get("/me", response_model=AuthenticatedUserResponse)
async def get_authenticated_user(
    current_user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> AuthenticatedUserResponse:
    """Return the authenticated identity or explicitly enabled local dev identity."""
    claims = current_user.claims
    name = claims.get("name") or claims.get("preferred_username") or current_user.email
    return AuthenticatedUserResponse(
        id=str(current_user.id),
        name=str(name),
        email=current_user.email,
        auth_mode=("local_bypass" if claims.get("auth_mode") == "local_bypass" else "oauth"),
    )
