"""Deterministic query planning service for evidence research tasks.

Conforms to spec 0007 AC-13:
1. Formulates bounded research queries from trip brief destinations.
2. Covers destination attractions, authentic local dining, affordable accommodation, and travel advisories.
3. Enforces a strict ceiling of 4 queries per collection batch.
4. Maps query limits to durable collector budgets (4 tool calls, 200 minor units INR).
"""

from __future__ import annotations

import logging
from typing import Any

from contracts.trips import TripBrief

logger = logging.getLogger(__name__)

DEFAULT_MAX_QUERIES: int = 4
DEFAULT_COST_PER_CALL_MINOR: int = 50
DEFAULT_CURRENCY: str = "INR"


class QueryPlanningService:
    """Service that deterministically plans research queries and collector budgets from a trip brief."""

    @classmethod
    def plan_destination_queries(
        cls,
        brief: dict[str, Any] | TripBrief,
        max_queries: int = DEFAULT_MAX_QUERIES,
    ) -> list[str]:
        """Formulates bounded research queries from trip brief inputs.

        Generates queries covering:
        1. Destination attractions and sightseeing activities
        2. Local authentic dining and regional food specialties
        3. Affordable accommodation and budget lodging
        4. Route travel advisories, road conditions, or highway alerts

        Guarantees that total queries never exceed the specified ceiling.
        """
        ceiling = min(max_queries, DEFAULT_MAX_QUERIES)

        # Extract destinations and origin from brief
        destinations: list[str] = []
        origin: str = ""
        dietary: list[str] = []

        if isinstance(brief, TripBrief):
            origin = brief.origin_name.strip()
            for d in brief.destinations:
                if d.name and d.name.strip():
                    destinations.append(d.name.strip())
            dietary = list(brief.dietary_preferences)
        elif isinstance(brief, dict):
            origin = str(brief.get("origin_name") or "").strip()
            raw_dests = brief.get("destinations", [])
            if isinstance(raw_dests, list):
                for item in raw_dests:
                    if isinstance(item, dict):
                        name = item.get("name")
                        if name and str(name).strip():
                            destinations.append(str(name).strip())
                    elif hasattr(item, "name") and item.name:
                        destinations.append(str(item.name).strip())
                    elif isinstance(item, str) and item.strip():
                        destinations.append(item.strip())
            if not destinations:
                dest_name = brief.get("destination_name")
                if dest_name and str(dest_name).strip():
                    destinations.append(str(dest_name).strip())
            raw_dietary = brief.get("dietary_preferences", [])
            if isinstance(raw_dietary, list):
                dietary = [str(x).strip() for x in raw_dietary if x and str(x).strip()]

        primary_dest = destinations[0] if destinations else "Unknown Destination"

        diet_context = ""
        if dietary:
            diet_context = f" {dietary[0]}"

        # Formulate ordered candidate queries
        query_candidates: list[str] = [
            f"{primary_dest} top attractions sightseeing places to visit",
            f"{primary_dest}{diet_context} authentic local food restaurants",
            f"{primary_dest} budget hotels affordable accommodation",
        ]

        if origin:
            query_candidates.append(f"{origin} to {primary_dest} road travel advisory route conditions")
        else:
            query_candidates.append(f"{primary_dest} travel advisory weather road alerts")

        # If multiple destinations exist and room permits, add secondary destination query
        if len(destinations) > 1 and len(query_candidates) < ceiling:
            sec_dest = destinations[1]
            query_candidates.append(f"{sec_dest} top attractions places to visit")

        return query_candidates[:ceiling]

    @classmethod
    def calculate_collector_budget(
        cls,
        queries: list[str],
        cost_per_call_minor: int = DEFAULT_COST_PER_CALL_MINOR,
        currency: str = DEFAULT_CURRENCY,
    ) -> dict[str, Any]:
        """Calculates durable collector budget matching planned queries.

        Conforms to durable collector schema:
        remaining_tool_calls: count of planned queries
        cost_per_call_minor: cost per query in minor currency units
        remaining_cost_minor: total reserved cost
        currency: ISO currency code
        """
        count = len(queries)
        return {
            "remaining_tool_calls": count,
            "remaining_cost_minor": count * cost_per_call_minor,
            "cost_per_call_minor": cost_per_call_minor,
            "currency": currency,
        }

