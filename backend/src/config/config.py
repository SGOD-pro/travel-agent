"""Configuration settings using pydantic-settings.

Supports environments: local, staging, production.
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
    DB_SSL: bool = False

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def default_database_url_if_empty(cls, v: Any) -> str:
        url = str(v).strip() if v else ""
        if not url:
            return "postgresql+asyncpg://postgres:postgres@localhost:5432/travel_planning"
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://") :]
        if url.startswith("postgresql://") and not url.startswith("postgresql+asyncpg://"):
            return "postgresql+asyncpg://" + url[len("postgresql://") :]
        return url

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

    # Evidence Collector and External Providers
    SERPAPI_API_KEY: str | None = Field(
        default=None,
        description="API key for SerpAPI provider",
    )
    SERPAPI_BASE_URL: str = Field(
        default="https://serpapi.com",
        description="Base URL for SerpAPI requests",
    )
    HTTP_REQUEST_TIMEOUT_SECONDS: int = Field(
        default=20,
        description="Request timeout in seconds for external HTTP fetches",
    )
    MAX_SOURCE_PAYLOAD_BYTES: int = Field(
        default=5242880,
        description="Maximum allowed payload download size in bytes",
    )


settings = Settings()

