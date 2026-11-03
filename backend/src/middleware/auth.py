"""OAuth 2.1 & OIDC authentication dependency for FastAPI."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import jwt
from fastapi import Header, HTTPException, status
from jwt import PyJWKClient, PyJWKClientConnectionError, PyJWKClientError
from pydantic import BaseModel, Field

from config.config import settings

_jwks_client = PyJWKClient(settings.JWKS_URL, cache_keys=True, timeout=5)


class AuthenticatedUser(BaseModel):
    id: uuid.UUID
    email: str
    client_id: str | None = None
    claims: dict[str, Any] = Field(default_factory=dict)


async def get_current_user(
    authorization: str | None = Header(default=None),
) -> AuthenticatedUser:
    """Validate a signed OAuth 2.1 JWT and map its subject to an application user."""
    if not authorization:
        if settings.ENVIRONMENT == "local" and settings.LOCAL_AUTH_BYPASS:
            return AuthenticatedUser(
                id=uuid.uuid5(uuid.NAMESPACE_URL, "swena-local-development-user"),
                email="local-dev@swena.internal",
                claims={"name": "Local developer", "auth_mode": "local_bypass"},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = parts[1]
    try:
        signing_key = await asyncio.to_thread(_jwks_client.get_signing_key_from_jwt, token)
        payload_data = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.CLIENT_ID,
            issuer=settings.AUTH_ISSUER,
            options={"require": ["exp", "iat", "sub"]},
        )
        token_client = payload_data.get("client_id") or payload_data.get("azp")
        if token_client is not None and token_client != settings.CLIENT_ID:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token was not issued for this client application",
            )

        raw_sub = payload_data["sub"]
        try:
            user_uuid = uuid.UUID(str(raw_sub))
        except ValueError:
            user_uuid = uuid.uuid5(uuid.NAMESPACE_URL, str(raw_sub))

        return AuthenticatedUser(
            id=user_uuid,
            email=str(payload_data.get("email", "")),
            client_id=token_client,
            claims=payload_data,
        )
    except HTTPException:
        raise
    except PyJWKClientConnectionError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Token verification service is temporarily unavailable",
        ) from e
    except (jwt.PyJWTError, PyJWKClientError, ValueError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or unverifiable bearer token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from e
