"""Factory for constructing language model client adapters based on configuration."""

from __future__ import annotations

from pathlib import Path

from config.config import Settings, get_settings
from functions.orchestrator.llm.adapters.fixture_client import FixtureLlmClient
from functions.orchestrator.llm.port import HostedLlmClientPort


def create_llm_client(
    settings: Settings | None = None,
    provider: str | None = None,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
    fixture_dir: Path | str | None = None,
) -> HostedLlmClientPort:
    """Creates and returns the configured language model client adapter.

    Args:
        settings: Application settings instance (defaults to singleton get_settings()).
        provider: Optional explicit provider override ("fixture", "bedrock", etc.).
        timeout_seconds: Optional socket timeout in seconds.
        max_retries: Optional maximum retry attempts.
        fixture_dir: Optional custom directory containing JSON fixture files.

    Returns:
        Instance conforming to HostedLlmClientPort.

    Raises:
        NotImplementedError: For hosted cloud providers whose adapters are deferred.
        ValueError: For unsupported or invalid provider identifiers.
    """
    effective_settings = settings or get_settings()
    effective_provider = (
        (provider if provider is not None else effective_settings.LLM_PROVIDER).strip().lower()
    )

    effective_timeout = (
        timeout_seconds if timeout_seconds is not None else effective_settings.LLM_TIMEOUT_SECONDS
    )
    effective_retries = (
        max_retries if max_retries is not None else effective_settings.LLM_MAX_RETRIES
    )

    if effective_provider == "fixture":
        resolved_fixture_dir = fixture_dir
        if resolved_fixture_dir is None and effective_settings.LLM_FIXTURE_DIR:
            resolved_fixture_dir = Path(effective_settings.LLM_FIXTURE_DIR)
        return FixtureLlmClient(
            fixture_dir=resolved_fixture_dir,
            timeout_seconds=effective_timeout,
            max_retries=effective_retries,
        )

    if effective_provider in ("bedrock", "anthropic", "openai", "gemini"):
        raise NotImplementedError(
            f"Hosted adapter for provider '{effective_provider}' is deferred "
            "until API credentials and deployment choices are confirmed. "
            "Use 'fixture' for local testing and continuous integration."
        )

    raise ValueError(
        f"Unsupported LLM provider '{effective_provider}'. Supported providers: 'fixture'."
    )
