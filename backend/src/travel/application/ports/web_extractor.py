"""Port definition for web extraction and scraping adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from travel.domain.evidence import FareObservation, HotelObservation
from travel.domain.trips import TransportMode


class WebExtractionPort(ABC):
    """Abstract port for web extraction of lodging and transit observations."""

    @abstractmethod
    async def extract_hotel_observations(
        self,
        destination: str,
        checkin_date: date,
        checkout_date: date,
        rooms: int = 1,
    ) -> list[HotelObservation]:
        """Extract hotel stay rate observations for a destination and date range."""
        ...

    @abstractmethod
    async def extract_transit_observations(
        self,
        origin: str,
        destination: str,
        travel_date: date,
        mode: TransportMode,
    ) -> list[FareObservation]:
        """Extract transit schedule and fare observations between two points."""
        ...
