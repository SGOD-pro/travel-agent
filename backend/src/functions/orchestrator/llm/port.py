"""Abstract port definition for hosted language model integration.

Declares the client interface for:
- Researching candidate points of interest along travel corridors
- Scoring and ranking candidate places based on traveler brief constraints
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from contracts.llm import (
    RankingRequest,
    RankingResponse,
    ResearchRequest,
    ResearchResponse,
)


class HostedLlmClientPort(ABC):
    """Provider-neutral abstract port for language model services."""

    @abstractmethod
    async def research_places(self, request: ResearchRequest) -> ResearchResponse:
        """Discovers candidate points of interest along the destination corridor.

        Args:
            request: Typed corridor research parameters including origin, destinations,
                preferences, budget tier, and transport mode.

        Returns:
            ResearchResponse with candidate place proposals and travel advisories.

        Raises:
            TimeoutError: When request duration exceeds configured socket timeout.
            Exception: On unrecoverable provider failures after retries.
        """
        raise NotImplementedError

    @abstractmethod
    async def rank_places(self, request: RankingRequest) -> RankingResponse:
        """Evaluates and orders candidate places by brief alignment and feasibility.

        Args:
            request: Candidate places list, travel brief context, and constraints.

        Returns:
            RankingResponse with prioritized places and selection rationales.

        Raises:
            TimeoutError: When request duration exceeds configured socket timeout.
            Exception: On unrecoverable provider failures after retries.
        """
        raise NotImplementedError
