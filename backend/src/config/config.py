"""Configuration settings using pydantic-settings.

Supports environments: local, staging, production.
Supports both Upstash HTTP Redis (prod) and Local Docker Redis (local).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator
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
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/travel_planning",
        description="SQLAlchemy asyncpg PostgreSQL connection URL",
    )
    HOST: str | None = None
    PORT: int | None = None
    USER: str | None = None
    PASSWORD: str | None = None
    DB_NAME: str | None = None
    DB_SSL: bool = False

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def default_database_url_if_empty(cls, v: Any) -> str:
        if not v or not str(v).strip():
            return "postgresql+asyncpg://postgres:postgres@localhost:5432/travel_planning"
        url = str(v).strip()
        if url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + url[len("postgres://") :]
        elif url.startswith("postgresql://") and not url.startswith("postgresql+asyncpg://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://") :]
        return url

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

    # AWS Configuration
    AWS_REGION: str = Field(
        default="ap-south-1",
        description="AWS region name",
    )
    SQS_QUEUE_URL: str | None = Field(
        default=None,
        description="Amazon SQS queue URL for outbox relay events",
    )


settings = Settings()
