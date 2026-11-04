"""OAuth 2.1 & OIDC Authentication dependency for FastAPI.

Validates incoming Bearer tokens issued by SWYRA Auth (SGOD-pro/OAuth2.1).
Supports offline JWKS verification, cryptographic signature validation,
issuer and audience binding, and expiry verification.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import jwt
from fastapi import Header, HTTPException, status
from jwt import PyJWKClient, PyJWTError
from pydantic import BaseModel

from config.config import settings

logger = logging.getLogger(__name__)

_jwk_client: PyJWKClient | None = None


def get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(settings.JWKS_URL, cache_jwk_set=True, lifespan=3600)
    return _jwk_client


def get_verification_key(token: str) -> Any:
    """Retrieve the public verification key from configured PEM or remote JWKS."""
    if settings.AUTH_PUBLIC_KEY:
        return settings.AUTH_PUBLIC_KEY
    client = get_jwk_client()
    signing_key = client.get_signing_key_from_jwt(token)
    return signing_key.key


class AuthenticatedUser(BaseModel):
    id: uuid.UUID
    email: str
    client_id: str | None = None
    claims: dict[str, Any] = {}


async def get_current_user(
    authorization: str | None = Header(default=None),
) -> AuthenticatedUser:
    """Extract and validate OAuth 2.1 Bearer token from the Authorization header."""
    if not authorization or not authorization.strip():
        # Only permit default local identity when explicitly gated in local environment
        is_local_env = settings.ENVIRONMENT == "local"
        dev_user_allowed = getattr(settings, "ALLOW_DEV_LOCAL_USER", False) is True

        if is_local_env and dev_user_allowed:
            return AuthenticatedUser(
                id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                email="traveler@swena.internal",
                client_id=settings.CLIENT_ID,
                claims={"sub": "00000000-0000-0000-0000-000000000001", "dev_identity": True},
            )

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or empty authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = parts[1]

    try:
        verification_key = get_verification_key(token)
        payload = jwt.decode(
            token,
            key=verification_key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
            audience=settings.CLIENT_ID,
            issuer=settings.AUTH_ISSUER,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={
                "WWW-Authenticate": 'Bearer error="invalid_token", error_description="The token has expired"'
            },
        ) from e
    except jwt.InvalidAudienceError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token was not issued for this client application",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from e
    except jwt.InvalidIssuerError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token issuer",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from e
    except jwt.InvalidSignatureError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token signature",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from e
    except (PyJWTError, Exception) as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token credentials: {e!s}",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from e

    # Enforce client_id binding if azp or client_id claim exists
    token_client = payload.get("client_id") or payload.get("azp")
    if token_client and token_client != settings.CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token was not issued for this client application",
        )

    raw_sub = payload.get("sub")
    if not raw_sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token subject claim is missing",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )

    try:
        user_uuid = uuid.UUID(str(raw_sub))
    except ValueError:
        user_uuid = uuid.uuid5(uuid.NAMESPACE_URL, str(raw_sub))

    return AuthenticatedUser(
        id=user_uuid,
        email=str(payload.get("email", "traveler@swena.internal")),
        client_id=token_client,
        claims=payload,
    )
