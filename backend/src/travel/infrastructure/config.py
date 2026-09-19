"""Configuration settings using pydantic-settings.

Supports environments: local, staging, production.
Supports both Upstash HTTP Redis (prod) and Local Docker Redis (local).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Environment
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    DEBUG: bool = False

    # Database (PostgreSQL + PostGIS)
    # Default to local docker postgres
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/travel_planning",
        description="SQLAlchemy asyncpg PostgreSQL connection URL",
    )

    # Redis configuration
    REDIS_BACKEND: Literal["local", "upstash"] = Field(
        default="local",
        description="Redis provider to use: 'local' for standard async TCP redis, 'upstash' for HTTP REST SDK",
    )
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Connection URL for local Docker Redis",
    )
    UPSTASH_REDIS_URL: str = Field(
        default="",
        description="REST URL for Upstash Redis (e.g. https://xyz.upstash.io)",
    )
    UPSTASH_REDIS_TOKEN: str = Field(
        default="",
        description="REST Token for Upstash Redis",
    )

    # SWYRA Auth (OAuth 2.1 / OIDC)
    AUTH_ISSUER: str = Field(
        default="https://oauth21.vercel.app",
        description="Base URL of the SWYRA Auth identity provider",
    )
    JWKS_URL: str = Field(
        default="https://oauth21.vercel.app/.well-known/jwks.json",
        description="Public JWKS URL for offline RS256 JWT validation",
    )
    CLIENT_ID: str = Field(
        default="swena_travel_agent_client",
        description="OAuth 2.1 client ID for audience enforcement",
    )


settings = Settings()
