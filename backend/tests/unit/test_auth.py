"""Unit tests for OAuth 2.1 authentication dependency."""

import base64
import json
import time
import uuid

import pytest
from fastapi import HTTPException

from travel.runtime.fastapi.auth import AuthenticatedUser, get_current_user


@pytest.mark.asyncio
async def test_get_current_user_default_when_no_header() -> None:
    user = await get_current_user(None)
    assert isinstance(user, AuthenticatedUser)
    assert user.id == uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert user.email == "traveler@swena.internal"


@pytest.mark.asyncio
async def test_get_current_user_valid_jwt_token() -> None:
    payload = {
        "sub": "user-12345",
        "email": "alice@swena.internal",
        "client_id": "swena_travel_agent_client",
        "exp": time.time() + 3600,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    fake_jwt = f"header.{encoded}.signature"

    user = await get_current_user(f"Bearer {fake_jwt}")
    assert isinstance(user, AuthenticatedUser)
    assert user.email == "alice@swena.internal"
    assert user.client_id == "swena_travel_agent_client"


@pytest.mark.asyncio
async def test_get_current_user_expired_token_rejected() -> None:
    payload = {
        "sub": "user-12345",
        "email": "alice@swena.internal",
        "exp": time.time() - 3600,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    fake_jwt = f"header.{encoded}.signature"

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(f"Bearer {fake_jwt}")
    assert exc_info.value.status_code == 401
    assert "expired" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_get_current_user_wrong_client_forbidden() -> None:
    payload = {
        "sub": "user-12345",
        "email": "alice@swena.internal",
        "client_id": "different_app_client",
        "exp": time.time() + 3600,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    fake_jwt = f"header.{encoded}.signature"

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(f"Bearer {fake_jwt}")
    assert exc_info.value.status_code == 403
