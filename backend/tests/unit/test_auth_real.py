"""Unit tests for real cryptographic OAuth 2.1 authentication and token fencing."""

from __future__ import annotations

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from config.config import settings
from middleware.auth import AuthenticatedUser, get_current_user

# Generate genuine test RSA key pairs for testing
_trusted_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TRUSTED_PRIVATE_PEM = _trusted_private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")
TRUSTED_PUBLIC_PEM = _trusted_private_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("utf-8")

_attacker_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ATTACKER_PRIVATE_PEM = _attacker_private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")


@pytest.fixture(autouse=True)
def configure_auth_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AUTH_PUBLIC_KEY", TRUSTED_PUBLIC_PEM)
    monkeypatch.setattr(settings, "AUTH_ISSUER", "https://oauth21.vercel.app")
    monkeypatch.setattr(settings, "CLIENT_ID", "swena_travel_agent_client")
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "ALLOW_DEV_LOCAL_USER", False)


def create_token(
    sub: str = "11111111-1111-1111-1111-111111111111",
    email: str = "traveler@swena.internal",
    aud: str = "swena_travel_agent_client",
    iss: str = "https://oauth21.vercel.app",
    exp_offset: int = 3600,
    signing_pem: str = TRUSTED_PRIVATE_PEM,
) -> str:
    payload = {
        "sub": sub,
        "email": email,
        "aud": aud,
        "iss": iss,
        "exp": int(time.time()) + exp_offset,
    }
    return jwt.encode(payload, signing_pem, algorithm="RS256")


@pytest.mark.asyncio
async def test_valid_signed_token_authenticates_user() -> None:
    token = create_token()
    user = await get_current_user(f"Bearer {token}")
    assert isinstance(user, AuthenticatedUser)
    assert user.id == uuid.UUID("11111111-1111-1111-1111-111111111111")
    assert user.email == "traveler@swena.internal"


@pytest.mark.asyncio
async def test_forged_token_rejected_with_401() -> None:
    forged_token = create_token(signing_pem=ATTACKER_PRIVATE_PEM)
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(f"Bearer {forged_token}")
    assert exc_info.value.status_code == 401
    assert "invalid token signature" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_expired_token_rejected_with_401() -> None:
    expired_token = create_token(exp_offset=-60)
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(f"Bearer {expired_token}")
    assert exc_info.value.status_code == 401
    assert "expired" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_invalid_issuer_rejected_with_401() -> None:
    bad_iss_token = create_token(iss="https://untrusted-issuer.example.com")
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(f"Bearer {bad_iss_token}")
    assert exc_info.value.status_code == 401
    assert "issuer" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_invalid_audience_rejected_with_403() -> None:
    bad_aud_token = create_token(aud="different_client_id")
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(f"Bearer {bad_aud_token}")
    assert exc_info.value.status_code == 403
    assert "not issued for this client" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_missing_credentials_in_production_rejected_with_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "ALLOW_DEV_LOCAL_USER", True)  # Ignored in production
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(None)
    assert exc_info.value.status_code == 401
    assert "missing" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_missing_credentials_in_local_default_gated_with_401() -> None:
    # ALLOW_DEV_LOCAL_USER is False by default
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(None)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_missing_credentials_in_local_explicitly_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ALLOW_DEV_LOCAL_USER", True)
    user = await get_current_user(None)
    assert isinstance(user, AuthenticatedUser)
    assert user.id == uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.mark.asyncio
async def test_malformed_header_format_rejected_with_401() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user("Basic dXNlcjpwYXNz")
    assert exc_info.value.status_code == 401
    assert "expected 'bearer <token>'" in str(exc_info.value.detail).lower()
