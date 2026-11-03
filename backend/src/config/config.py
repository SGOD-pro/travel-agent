"""Configuration settings using pydantic-settings.

Supports environments: local, staging, production.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
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

    # Browser Escalation Worker
    BROWSER_WORKER_QUEUE_URL: str | None = Field(
        default=None,
        description="Amazon SQS queue URL for browser worker task dispatch",
    )
    CHROMIUM_PATH: str | None = Field(
        default=None,
        description="Path to Chromium executable for browser automation",
    )
    BROWSER_NAVIGATION_TIMEOUT_SECONDS: int = Field(
        default=20,
        description="Maximum seconds allowed for browser page navigation",
    )
    BROWSER_MAX_ESCALATION_TASKS: int = Field(
        default=3,
        description="Maximum browser tasks permitted per trip execution",
    )

    # Remote LAYA Decision Router
    LAYA_ENDPOINT_URL: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LAYA_ENDPOINT_URL", "LAYA_ENDPOINT"),
        description="HTTPS URL of remote LAYA service",
    )
    LAYA_API_KEY: str | None = Field(
        default=None,
        description="Optional Bearer authentication token for LAYA application server",
    )
    CF_ACCESS_CLIENT_ID: str | None = Field(
        default=None,
        description="Optional Cloudflare Access service token client identifier",
    )
    CF_ACCESS_CLIENT_SECRET: str | None = Field(
        default=None,
        description="Optional Cloudflare Access service token client secret",
    )
    LAYA_REQUEST_TIMEOUT_SECONDS: float = Field(
        default=2.5,
        ge=0.5,
        le=30.0,
        description="Individual socket request timeout in seconds",
    )
    LAYA_TOTAL_DEADLINE_SECONDS: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        description="Cumulative request deadline across all retries in seconds",
    )
    LAYA_MAX_RETRIES: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Maximum retry attempts for transient 5xx or network errors",
    )
    LAYA_CONFIDENCE_THRESHOLD: float = Field(
        default=0.700,
        ge=0.0,
        le=1.0,
        description="Minimum answer_confidence score required to accept proposal",
    )
    LAYA_MODEL_NAME: str = Field(
        default="typed-decisions",
        description="Target checkpoint name or empty to auto route",
    )

    @model_validator(mode="after")
    def validate_laya_deadlines(self) -> Settings:
        if self.LAYA_TOTAL_DEADLINE_SECONDS < self.LAYA_REQUEST_TIMEOUT_SECONDS:
            raise ValueError(
                "LAYA_TOTAL_DEADLINE_SECONDS must be greater than or equal to LAYA_REQUEST_TIMEOUT_SECONDS"
            )
        return self


settings = Settings()


def get_settings() -> Settings:
    """Returns the singleton application settings instance."""
    return settings

