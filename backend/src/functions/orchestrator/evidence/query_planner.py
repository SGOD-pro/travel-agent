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

        # Extract destinations, origin, dates, preferences, and budget from brief
        destinations: list[str] = []
        origin: str = ""
        dietary: list[str] = []
        traveler_prefs: list[str] = []
        start_date_val: Any = None
        budget_tier: str = "MODERATE"

        if isinstance(brief, TripBrief):
            origin = brief.origin_name.strip()
            for d in brief.destinations:
                if d.name and d.name.strip():
                    destinations.append(d.name.strip())
            dietary = list(brief.dietary_preferences)
            traveler_prefs = list(brief.dietary_preferences) + list(brief.accessibility_requirements)
            start_date_val = brief.start_date
            if brief.target_budget is not None:
                # If budget is provided as Money
                budget_tier = "BUDGET" if getattr(brief.target_budget, "amount_minor", 0) < 500000 else "MODERATE"
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
            raw_prefs = brief.get("traveler_preferences", [])
            if isinstance(raw_prefs, list):
                traveler_prefs = [str(x).strip() for x in raw_prefs if x and str(x).strip()]
            start_date_val = brief.get("start_date")
            budget_tier = str(brief.get("budget_tier") or "MODERATE").upper()

        primary_dest = destinations[0] if destinations else "Unknown Destination"

        # Date context for seasonal/stay relevance
        date_context = ""
        if start_date_val is not None:
            if hasattr(start_date_val, "strftime"):
                date_context = f" {start_date_val.strftime('%B')}"
            elif isinstance(start_date_val, str) and len(start_date_val) >= 7:
                try:
                    from datetime import date as dt_date
                    parsed_d = dt_date.fromisoformat(start_date_val[:10])
                    date_context = f" {parsed_d.strftime('%B')}"
                except Exception:
                    pass

        # Food preferences context
        diet_context = ""
        if dietary:
            diet_context = f" {dietary[0]}"
        elif traveler_prefs:
            food_terms = [p for p in traveler_prefs if any(k in p.lower() for k in ("food", "dining", "cuisine", "street", "sweet", "veg"))]
            if food_terms:
                diet_context = f" {food_terms[0]}"

        # Budget tier context for accommodation
        hotel_tier = "budget "
        if budget_tier == "LUXURY":
            hotel_tier = "luxury resorts "
        elif budget_tier == "MODERATE":
            hotel_tier = "budget "

        # Formulate ordered candidate queries
        query_candidates: list[str] = [
            f"{primary_dest} top attractions sightseeing places to visit",
            f"{primary_dest}{diet_context} authentic local food restaurants",
            f"{primary_dest} {hotel_tier}hotels affordable accommodation{date_context}".strip(),
        ]

        if origin:
            query_candidates.append(
                f"{origin} to {primary_dest} road travel advisory route conditions"
            )
        else:
            query_candidates.append(f"{primary_dest} travel advisory weather road alerts")

        # If multiple destinations exist and room permits, add secondary destination query
        if len(destinations) > 1 and len(query_candidates) < ceiling:
            sec_dest = destinations[1]
            query_candidates.append(f"{sec_dest} top attractions places to visit")

        return query_candidates[:ceiling]

    @classmethod
    async def plan_queries_with_llm(
        cls,
        brief: dict[str, Any] | TripBrief,
        llm_client: Any | None = None,
        max_queries: int = DEFAULT_MAX_QUERIES,
    ) -> list[str]:
        """Plans queries using configured LLM client with fallback to deterministic templates."""
        if llm_client is not None and hasattr(llm_client, "generate_search_queries"):
            try:
                from contracts.llm import ResearchRequest

                origin = ""
                destinations: list[str] = []
                prefs: list[str] = []
                budget_tier = "MODERATE"
                mode = "car_petrol"
                if isinstance(brief, TripBrief):
                    origin = brief.origin_name
                    destinations = [d.name for d in brief.destinations if d.name]
                    prefs = list(brief.dietary_preferences) + list(brief.accessibility_requirements)
                    budget_tier = str(getattr(brief, "budget_tier", "MODERATE"))
                elif isinstance(brief, dict):
                    origin = str(brief.get("origin_name") or "")
                    for d in brief.get("destinations", []):
                        if isinstance(d, dict) and d.get("name"):
                            destinations.append(str(d["name"]))
                        elif isinstance(d, str) and d.strip():
                            destinations.append(d.strip())
                        elif hasattr(d, "name") and d.name:
                            destinations.append(str(d.name))
                    if not destinations and brief.get("destination_name"):
                        destinations.append(str(brief["destination_name"]).strip())
                    prefs = list(brief.get("dietary_preferences", [])) + list(
                        brief.get("accessibility_requirements", [])
                    )
                    if "traveler_preferences" in brief and isinstance(
                        brief["traveler_preferences"], list
                    ):
                        prefs.extend(str(p) for p in brief["traveler_preferences"])
                    budget_tier = str(brief.get("budget_tier") or "MODERATE")

                req = ResearchRequest(
                    brief_summary=f"Trip from {origin} to {', '.join(destinations)}",
                    origin_name=origin or "Origin",
                    destination_names=destinations or ["Destination"],
                    traveler_preferences=prefs,
                    budget_tier=budget_tier,
                    transport_mode=mode,
                )
                queries = await llm_client.generate_search_queries(req)
                if queries:
                    queries_list: list[str] = [str(q) for q in queries]
                    return queries_list[: min(max_queries, DEFAULT_MAX_QUERIES)]
            except Exception as exc:
                logger.warning("LLM query planning failed (%s); using deterministic fallback", exc)

        return cls.plan_destination_queries(brief, max_queries)

    @classmethod
    def calculate_collector_budget(
        cls,
        queries: list[str],
        cost_per_call_minor: int = DEFAULT_COST_PER_CALL_MINOR,
        currency: str = DEFAULT_CURRENCY,
        extra_tool_calls: int = 0,
    ) -> dict[str, Any]:
        """Calculates durable collector budget matching planned queries and extraction headroom.

        Conforms to durable collector schema:
        remaining_tool_calls: count of planned queries plus extraction headroom
        cost_per_call_minor: cost per query in minor currency units
        remaining_cost_minor: total reserved cost
        currency: ISO currency code
        """
        count = len(queries) + max(0, extra_tool_calls)
        return {
            "remaining_tool_calls": count,
            "remaining_cost_minor": count * cost_per_call_minor,
            "cost_per_call_minor": cost_per_call_minor,
            "currency": currency,
        }
