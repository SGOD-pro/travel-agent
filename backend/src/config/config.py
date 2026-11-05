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
    LOCAL_AUTH_BYPASS: bool = Field(
        default=False,
        description="Allow anonymous requests only for explicitly enabled local development",
    )

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
    CORS_ALLOWED_ORIGINS: list[str] = Field(
        default=["http://localhost:3000"],
        description="Explicit browser origins permitted to call the API",
    )
    PUBLIC_RATE_LIMIT_REQUESTS: int = Field(default=120, ge=1)
    PUBLIC_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, ge=1)

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
    MEDIA_ALLOWED_HOSTS: list[str] = Field(
        default_factory=list,
        description="Exact hostnames permitted for direct media downloads",
    )
    MEDIA_DOWNLOAD_TIMEOUT_SECONDS: int = Field(default=20, ge=1, le=120)
    MEDIA_ANALYSIS_PROVIDER: Literal["mock", "aws"] = "mock"
    MEDIA_GEOCODER_PROVIDER: Literal["none", "nominatim"] = "none"
    AWS_MEDIA_BUCKET: str | None = None
    MEDIA_TRANSCRIBE_TIMEOUT_SECONDS: int = Field(default=300, ge=10, le=1800)
    MEDIA_TRANSCRIBE_POLL_INTERVAL_SECONDS: float = Field(default=2.0, ge=0.1, le=60.0)
    ROUTING_PROVIDER: Literal["osrm", "google"] = "osrm"
    OSRM_BASE_URL: str = "https://router.project-osrm.org"
    GOOGLE_MAPS_API_KEY: str | None = None
    GOOGLE_MAPS_MATRIX_URL: str = (
        "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
    )
    GOOGLE_MAPS_DIRECTIONS_URL: str = (
        "https://routes.googleapis.com/directions/v2:computeRoutes"
    )
    NOMINATIM_BASE_URL: str = "https://nominatim.openstreetmap.org"
    NOMINATIM_USER_AGENT: str | None = None
    NOMINATIM_MIN_INTERVAL_SECONDS: float = Field(default=1.0, ge=1.0)

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
        if self.LOCAL_AUTH_BYPASS and self.ENVIRONMENT != "local":
            raise ValueError("LOCAL_AUTH_BYPASS is only allowed when ENVIRONMENT=local")
        if self.ENVIRONMENT == "production":
            missing: list[str] = []
            if self.ROUTING_PROVIDER == "google" and not self.GOOGLE_MAPS_API_KEY:
                missing.append("GOOGLE_MAPS_API_KEY")
            if not self.MEDIA_ALLOWED_HOSTS:
                missing.append("MEDIA_ALLOWED_HOSTS")
            if self.MEDIA_ANALYSIS_PROVIDER != "aws":
                missing.append("MEDIA_ANALYSIS_PROVIDER=aws")
            if not self.AWS_MEDIA_BUCKET:
                missing.append("AWS_MEDIA_BUCKET")
            if self.MEDIA_GEOCODER_PROVIDER != "nominatim":
                missing.append("MEDIA_GEOCODER_PROVIDER=nominatim")
            if not self.NOMINATIM_USER_AGENT:
                missing.append("NOMINATIM_USER_AGENT")
            if missing:
                raise ValueError(
                    "Production provider configuration is incomplete: "
                    + ", ".join(missing)
                )
        return self


settings = Settings()


def get_settings() -> Settings:
    """Returns the singleton application settings instance."""
    return settings
