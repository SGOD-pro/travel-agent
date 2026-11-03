"""Unit tests for signed JWT validation and public API request limits."""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request

from config.config import Settings, settings
from middleware import auth
from middleware.rate_limit import PublicRequestRateLimiter


class FakeJwksClient:
    def __init__(self, key: Any) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
        jwt.get_unverified_header(token)
        return SimpleNamespace(key=self.key)


def _token(private_key: Any, **claim_overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": settings.AUTH_ISSUER,
        "aud": settings.CLIENT_ID,
        "sub": str(uuid.uuid4()),
        "iat": now,
        "exp": now + 60,
        **claim_overrides,
    }
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})


@pytest.mark.asyncio
async def test_authentication_verifies_signature_issuer_audience_and_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(auth, "_jwks_client", FakeJwksClient(private_key.public_key()))

    user = await auth.get_current_user(f"Bearer {_token(private_key, email='traveler@example.com')}")
    assert user.email == "traveler@example.com"

    invalid_tokens = [
        _token(private_key, iss="https://attacker.invalid"),
        _token(private_key, aud="another-client"),
        _token(private_key, exp=int(time.time()) - 1),
    ]
    for token in invalid_tokens:
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(f"Bearer {token}")
        assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_authentication_rejects_unsigned_and_missing_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth.settings, "LOCAL_AUTH_BYPASS", False)
    with pytest.raises(HTTPException) as missing:
        await auth.get_current_user(None)
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as invalid:
        await auth.get_current_user("Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiJ1c2VyIn0.")
    assert invalid.value.status_code == 401


@pytest.mark.asyncio
async def test_local_auth_bypass_requires_explicit_local_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth.settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(auth.settings, "LOCAL_AUTH_BYPASS", True)

    user = await auth.get_current_user(None)

    assert user.email == "local-dev@swena.internal"
    assert user.claims["auth_mode"] == "local_bypass"
    from functions.api.routes.auth import get_authenticated_user

    identity = await get_authenticated_user(user)
    assert identity.auth_mode == "local_bypass"

    with pytest.raises(HTTPException) as invalid:
        await auth.get_current_user("Bearer invalid-token")
    assert invalid.value.status_code == 401


@pytest.mark.asyncio
async def test_local_auth_bypass_is_not_allowed_outside_local_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(auth.settings, "LOCAL_AUTH_BYPASS", True)

    with pytest.raises(HTTPException) as missing:
        await auth.get_current_user(None)
    assert missing.value.status_code == 401

    with pytest.raises(ValueError, match="only allowed when ENVIRONMENT=local"):
        Settings(_env_file=None, ENVIRONMENT="staging", LOCAL_AUTH_BYPASS=True)


@pytest.mark.asyncio
async def test_public_request_rate_limiter_returns_429_after_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PUBLIC_RATE_LIMIT_REQUESTS", 1)
    limiter = PublicRequestRateLimiter()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/discovery/media",
            "headers": [],
            "client": ("192.0.2.10", 12345),
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "http_version": "1.1",
        }
    )
    await limiter(request)
    with pytest.raises(HTTPException) as exc_info:
        await limiter(request)
    assert exc_info.value.status_code == 429
