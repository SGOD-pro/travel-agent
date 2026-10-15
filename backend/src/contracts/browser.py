"""Domain contracts for browser escalation and dynamic rendering.

Conforms to spec 0003 Slice 1:
- SUCCESS requires substantive content and a valid SHA256 content_hash.
- UNAVAILABLE, TIMED_OUT, and BLOCKED require an unavailable_reason and permit
  absent content; never fabricate excerpts, hashes, or locators.
- Enforces documented text limits, UUID fields, and allowed wait_until values.
- Distinguishes adapter results (BrowserRenderResult) from persisted worker output
  (BrowserWorkerOutput).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contracts.evidence import FareObservation, HotelObservation

_SHA256_HEX_REGEX = re.compile(r"^[0-9a-f]{64}$")


class BrowserRenderStatus(StrEnum):
    """Normalized outcomes for browser rendering execution."""

    SUCCESS = "SUCCESS"
    UNAVAILABLE = "UNAVAILABLE"
    TIMED_OUT = "TIMED_OUT"
    BLOCKED = "BLOCKED"


class BrowserWaitUntil(StrEnum):
    """Allowed navigation completion strategies conforming to Playwright standards."""

    LOAD = "load"
    DOMCONTENTLOADED = "domcontentloaded"
    NETWORKIDLE = "networkidle"
    COMMIT = "commit"


class BrowserTaskPayload(BaseModel):
    """Input payload dispatched to the BROWSER worker."""

    model_config = ConfigDict(frozen=True)

    url: str = Field(..., min_length=1, max_length=2048, description="Target web page URL to render")
    canonical_url: str = Field(..., min_length=1, max_length=2048, description="Normalized canonical URL")
    category: str = Field(..., min_length=1, max_length=64, description="Evidence category, e.g. lodging, transit, advisory")
    reason: str = Field(..., min_length=1, max_length=255, description="Dynamic detection reason, e.g. NOSCRIPT_JAVASCRIPT_REQUIRED")
    wait_until: BrowserWaitUntil = Field(default=BrowserWaitUntil.NETWORKIDLE, description="Navigation completion strategy")
    timeout_seconds: int = Field(default=20, ge=1, le=60, description="Maximum navigation timeout in seconds")
    selector_target: str | None = Field(default=None, max_length=255, description="Optional CSS selector required before DOM capture")
    placeholder_observation_id: UUID | None = Field(default=None, description="Original NEEDS_BROWSER observation UUID")


class BrowserResourceUsage(BaseModel):
    """Resource consumption metrics for browser page execution."""

    model_config = ConfigDict(frozen=True)

    duration_ms: int = Field(..., ge=0, description="Elapsed execution duration in milliseconds")
    memory_mb: int | None = Field(default=None, ge=0, description="Estimated memory consumption in megabytes")


class BrowserRenderResult(BaseModel):
    """Normalized output produced by the browser extraction port.

    Distinguished from persisted worker output: this model captures the immediate
    outcome of browser page rendering and extraction before database persistence.
    """

    model_config = ConfigDict(frozen=True)

    status: BrowserRenderStatus = Field(default=BrowserRenderStatus.SUCCESS)
    canonical_url: str = Field(..., min_length=1, max_length=2048)
    effective_url: str = Field(..., min_length=1, max_length=2048)
    content_hash: str | None = Field(default=None, description="SHA256 hex digest of clean rendered text")
    excerpt_text: str | None = Field(default=None, max_length=1000, description="Clean substantive text excerpt up to 1000 characters")
    excerpt_locator: str | None = Field(default=None, max_length=256, description="CSS selector or locator for extracted content")
    rendered_html_snippet: str | None = Field(default=None, max_length=50000, description="Bounded excerpt of raw rendered HTML")
    render_duration_ms: int = Field(..., ge=0)
    unavailable_reason: str | None = Field(default=None, max_length=500)
    usage: BrowserResourceUsage
    parsed_lodging_observations: list[HotelObservation] = Field(default_factory=list)
    parsed_transit_observations: list[FareObservation] = Field(default_factory=list)

    @field_validator("content_hash")
    @classmethod
    def validate_content_hash(cls, v: str | None) -> str | None:
        if v is not None:
            cleaned = v.strip().lower()
            if not _SHA256_HEX_REGEX.match(cleaned):
                raise ValueError("content_hash must be a valid 64 character lowercase hex SHA256 digest")
            return cleaned
        return None

    @model_validator(mode="after")
    def validate_status_contract(self) -> BrowserRenderResult:
        if self.status == BrowserRenderStatus.SUCCESS:
            if not self.content_hash:
                raise ValueError("SUCCESS status requires a valid SHA256 content_hash")
            if not self.excerpt_text or not self.excerpt_text.strip():
                raise ValueError("SUCCESS status requires substantive non-empty excerpt_text")
            if not self.excerpt_locator or not self.excerpt_locator.strip():
                raise ValueError("SUCCESS status requires non-empty excerpt_locator")
            if self.unavailable_reason is not None:
                raise ValueError("SUCCESS status must not have an unavailable_reason")
        else:
            if not self.unavailable_reason or not self.unavailable_reason.strip():
                raise ValueError(f"{self.status} status requires a descriptive non-empty unavailable_reason")
            if self.content_hash is not None or self.excerpt_text is not None or self.excerpt_locator is not None:
                raise ValueError(
                    f"{self.status} status permits absent content and must never fabricate excerpts, hashes, or locators"
                )
        return self


class BrowserWorkerOutput(BaseModel):
    """Persisted worker output stored in WorkerResultModel.output_ref upon task completion.

    Distinguished from adapter results: this model captures the persisted database
    observation_id UUID generated for successful renders and normalized unavailable source records.
    """

    model_config = ConfigDict(frozen=True)

    status: BrowserRenderStatus = Field(default=BrowserRenderStatus.SUCCESS)
    canonical_url: str = Field(..., min_length=1, max_length=2048)
    effective_url: str = Field(..., min_length=1, max_length=2048)
    observation_id: UUID | None = Field(default=None, description="Persisted source observation UUID")
    content_hash: str | None = Field(default=None, description="SHA256 hex digest of clean rendered text")
    rendered_html_bytes: int | None = Field(default=None, ge=0)
    render_duration_ms: int = Field(..., ge=0)
    unavailable_sources: list[dict[str, Any]] = Field(default_factory=list)
    parsed_observations: list[dict[str, Any]] = Field(default_factory=list)
    usage: BrowserResourceUsage

    @field_validator("content_hash")
    @classmethod
    def validate_content_hash(cls, v: str | None) -> str | None:
        if v is not None:
            cleaned = v.strip().lower()
            if not _SHA256_HEX_REGEX.match(cleaned):
                raise ValueError("content_hash must be a valid 64 character lowercase hex SHA256 digest")
            return cleaned
        return None

    @model_validator(mode="after")
    def validate_worker_output(self) -> BrowserWorkerOutput:
        if self.status == BrowserRenderStatus.SUCCESS:
            if self.observation_id is None:
                raise ValueError("SUCCESS worker output requires a persisted observation_id UUID")
            if not self.content_hash:
                raise ValueError("SUCCESS worker output requires a valid SHA256 content_hash")
        else:
            if not self.unavailable_sources:
                raise ValueError(f"{self.status} worker output requires non-empty unavailable_sources records")
            if self.observation_id is not None or self.content_hash is not None:
                raise ValueError(
                    f"{self.status} worker output permits absent content and must not fabricate observation_id or content_hash"
                )
        return self


class BrowserExtractionPort(ABC):
    """Abstract port for browser page rendering and dynamic entity extraction."""

    @abstractmethod
    async def render_page(self, payload: BrowserTaskPayload) -> BrowserRenderResult:
        """Renders dynamic page content and returns normalized render outcome."""
        ...
