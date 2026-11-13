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
    async def generate_search_queries(
        self,
        request: ResearchRequest,
        max_queries: int = 4,
    ) -> list[str]:
        """Generates targeted search queries for real web search and evidence collection.

        Args:
            request: Typed corridor research parameters including origin, destinations,
                preferences, budget tier, and transport mode.
            max_queries: Maximum number of queries to generate (default 4).

        Returns:
            List of targeted search query strings for search engine execution.
        """
        raise NotImplementedError

    @abstractmethod
    async def research_places(self, request: ResearchRequest) -> ResearchResponse:
        """Discovers candidate points of interest along the corridor or prepares search discovery notes."""
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
