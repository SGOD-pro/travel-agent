"""Deterministic query planning service for evidence research tasks.

Conforms to spec 0007 AC-13:
1. Formulates bounded research queries distributed across trip brief destinations.
2. Covers destination attractions, authentic local dining, affordable accommodation, and travel advisories.
3. Carries explicit query purpose and destination association through dispatch to the collector.
4. Routes road/advisory queries to web search (engine="google") and venue discovery to place search (engine="google_maps").
5. Enforces a strict ceiling of 4 queries per collection batch.
6. Maps query limits to durable collector budgets (4 tool calls, 200 minor units INR).
7. Records uncovered destinations and explicit budget reasons when destinations exceed budget.
"""

from __future__ import annotations

import logging
from datetime import date as dt_date
from typing import Any

from contracts.trips import TripBrief

logger = logging.getLogger(__name__)

DEFAULT_MAX_QUERIES: int = 4
DEFAULT_COST_PER_CALL_MINOR: int = 50
DEFAULT_CURRENCY: str = "INR"


class PlannedQuery(dict[str, Any]):
    """Planned research query carrying explicit purpose, destination, and target engine."""

    def __init__(
        self,
        query: str,
        *,
        purpose: str = "attractions",
        destination: str = "",
        engine: str | None = None,
        **kwargs: Any,
    ) -> None:
        clean_purpose = str(purpose or "attractions").strip().lower()
        if clean_purpose not in ("attractions", "hours", "dining", "lodging", "advisory"):
            clean_purpose = "attractions"

        # Explicit purpose determines permitted search engine
        if clean_purpose in ("advisory", "hours"):
            target_engine = "google"
        else:
            target_engine = "google_maps"

        super().__init__(
            query=query,
            purpose=clean_purpose,
            destination=str(destination or "").strip(),
            engine=target_engine,
            **kwargs,
        )

    @property
    def query(self) -> str:
        return str(self.get("query", ""))

    @property
    def purpose(self) -> str:
        return str(self.get("purpose", "attractions"))

    @property
    def destination(self) -> str:
        return str(self.get("destination", ""))

    @property
    def engine(self) -> str:
        return str(self.get("engine", "google_maps"))

    def __str__(self) -> str:
        return str(self.get("query", ""))

    def mentions(self, text: str) -> bool:
        """Check whether text appears in query string (case insensitive)."""
        return text.lower() in str(self.get("query", "")).lower()


class PlannedQueryList(list[PlannedQuery]):
    """List of planned research queries with coverage metadata."""

    def __init__(
        self,
        queries: list[PlannedQuery] | None = None,
        *,
        covered_destinations: list[str] | None = None,
        uncovered_destinations: list[str] | None = None,
        coverage_reason: str | None = None,
        is_complete_coverage: bool | None = None,
    ) -> None:
        super().__init__(queries or [])
        self.covered_destinations: list[str] = list(covered_destinations or [])
        self.uncovered_destinations: list[str] = list(uncovered_destinations or [])
        self.coverage_reason: str | None = coverage_reason
        if is_complete_coverage is not None:
            self.is_complete_coverage: bool = is_complete_coverage
        else:
            self.is_complete_coverage = (
                len(self.covered_destinations) > 0 and len(self.uncovered_destinations) == 0
            )

    def __getitem__(self, item: Any) -> Any:
        res = super().__getitem__(item)
        if isinstance(item, slice):
            return PlannedQueryList(
                res,
                covered_destinations=self.covered_destinations,
                uncovered_destinations=self.uncovered_destinations,
                coverage_reason=self.coverage_reason,
                is_complete_coverage=self.is_complete_coverage,
            )
        return res


class QueryPlanningService:
    """Service that deterministically plans research queries and collector budgets from a trip brief."""

    @classmethod
    def plan_destination_queries(
        cls,
        brief: dict[str, Any] | TripBrief,
        max_queries: int = DEFAULT_MAX_QUERIES,
    ) -> PlannedQueryList:
        """Formulates bounded research queries distributed across trip brief destinations.

        Generates queries covering:
        1. Destination attractions and sightseeing activities (engine="google_maps")
        2. Local authentic dining and regional food specialties (engine="google_maps")
        3. Affordable accommodation and budget lodging (engine="google_maps")
        4. Route travel advisories, road conditions, or highway alerts (engine="google")

        Guarantees that total queries never exceed the specified ceiling.
        When destinations exceed available slots, records uncovered destinations with explicit reason.
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
            traveler_prefs = list(brief.dietary_preferences) + list(
                brief.accessibility_requirements
            )
            start_date_val = brief.start_date
            if brief.target_budget is not None:
                budget_tier = (
                    "BUDGET"
                    if getattr(brief.target_budget, "amount_minor", 0) < 500000
                    else "MODERATE"
                )
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

        # Date context for seasonal/stay relevance
        date_context = ""
        if start_date_val is not None:
            if hasattr(start_date_val, "strftime"):
                date_context = f" {start_date_val.strftime('%B')}"
            elif isinstance(start_date_val, str) and len(start_date_val) >= 7:
                try:
                    parsed_d = dt_date.fromisoformat(start_date_val[:10])
                    date_context = f" {parsed_d.strftime('%B')}"
                except Exception:
                    pass

        # Food preferences context
        diet_context = ""
        if dietary:
            diet_context = f" {dietary[0]}"
        elif traveler_prefs:
            food_terms = [
                p
                for p in traveler_prefs
                if any(
                    k in p.lower() for k in ("food", "dining", "cuisine", "street", "sweet", "veg")
                )
            ]
            if food_terms:
                diet_context = f" {food_terms[0]}"

        # Budget tier context for accommodation
        hotel_tier = "budget "
        if budget_tier == "LUXURY":
            hotel_tier = "luxury resorts "
        elif budget_tier == "MODERATE":
            hotel_tier = "budget "

        query_candidates: list[PlannedQuery] = []
        covered_destinations: list[str] = []
        uncovered_destinations: list[str] = []
        coverage_reason: str | None = None

        dest_count = len(destinations)

        if dest_count == 0:
            primary = "Unknown Destination"
            coverage_reason = "No destinations provided in trip brief."
            query_candidates.append(
                PlannedQuery(
                    f"{primary} top attractions sightseeing places to visit",
                    purpose="attractions",
                    destination=primary,
                    engine="google_maps",
                )
            )
            query_candidates.append(
                PlannedQuery(
                    f"{primary}{diet_context} authentic local food restaurants",
                    purpose="dining",
                    destination=primary,
                    engine="google_maps",
                )
            )
            query_candidates.append(
                PlannedQuery(
                    f"{primary} {hotel_tier}hotels affordable accommodation{date_context}".strip(),
                    purpose="lodging",
                    destination=primary,
                    engine="google_maps",
                )
            )
            query_candidates.append(
                PlannedQuery(
                    f"{primary} road travel advisory route conditions weather warnings road closures{date_context}".strip(),
                    purpose="advisory",
                    destination=primary,
                    engine="google",
                )
            )

        elif dest_count == 1:
            primary = destinations[0]
            covered_destinations.append(primary)
            query_candidates.append(
                PlannedQuery(
                    f"{primary} top attractions sightseeing places to visit",
                    purpose="attractions",
                    destination=primary,
                    engine="google_maps",
                )
            )
            query_candidates.append(
                PlannedQuery(
                    f"{primary}{diet_context} authentic local food restaurants",
                    purpose="dining",
                    destination=primary,
                    engine="google_maps",
                )
            )
            query_candidates.append(
                PlannedQuery(
                    f"{primary} {hotel_tier}hotels affordable accommodation{date_context}".strip(),
                    purpose="lodging",
                    destination=primary,
                    engine="google_maps",
                )
            )
            if origin:
                adv_query = f"{origin} to {primary} road travel advisory route conditions weather road closures{date_context}".strip()
            else:
                adv_query = f"{primary} road travel advisory route conditions weather warnings road closures{date_context}".strip()
            query_candidates.append(
                PlannedQuery(
                    adv_query,
                    purpose="advisory",
                    destination=primary,
                    engine="google",
                )
            )

        elif dest_count == 2:
            d0, d1 = destinations[0], destinations[1]
            covered_destinations.extend([d0, d1])
            # Slot 1: Destination 0 attractions
            query_candidates.append(
                PlannedQuery(
                    f"{d0} top attractions sightseeing places to visit",
                    purpose="attractions",
                    destination=d0,
                    engine="google_maps",
                )
            )
            # Slot 2: Destination 1 attractions
            query_candidates.append(
                PlannedQuery(
                    f"{d1} top attractions sightseeing places to visit",
                    purpose="attractions",
                    destination=d1,
                    engine="google_maps",
                )
            )
            # Slot 3: Food / dining for primary destination
            query_candidates.append(
                PlannedQuery(
                    f"{d0}{diet_context} authentic local food restaurants",
                    purpose="dining",
                    destination=d0,
                    engine="google_maps",
                )
            )
            # Slot 4: Road travel advisory covering the route
            if origin:
                adv_query = f"{origin} to {d0} and {d1} road travel advisory route conditions weather road closures{date_context}".strip()
            else:
                adv_query = f"{d0} to {d1} road travel advisory route conditions weather road closures{date_context}".strip()
            query_candidates.append(
                PlannedQuery(
                    adv_query,
                    purpose="advisory",
                    destination=f"{d0}, {d1}",
                    engine="google",
                )
            )

        else:
            # dest_count >= 3: Allocate available destination slots (max 3 for ceiling 4) + 1 advisory slot
            dest_slots = max(1, ceiling - 1)
            for dest_name_str in destinations[:dest_slots]:
                covered_destinations.append(dest_name_str)
                query_candidates.append(
                    PlannedQuery(
                        f"{dest_name_str} top attractions sightseeing places to visit",
                        purpose="attractions",
                        destination=dest_name_str,
                        engine="google_maps",
                    )
                )
            if dest_count > dest_slots:
                uncovered_destinations = list(destinations[dest_slots:])
                coverage_reason = (
                    f"Destination count ({dest_count}) exceeds available research query slots ({dest_slots}). "
                    f"Uncovered destinations: {', '.join(uncovered_destinations)} due to collector query ceiling ({ceiling})."
                )
            if origin:
                adv_query = f"{origin} to {', '.join(destinations[:dest_slots])} road travel advisory route conditions weather road closures{date_context}".strip()
            else:
                adv_query = f"{' to '.join(destinations[:dest_slots])} road travel advisory route conditions weather road closures{date_context}".strip()
            query_candidates.append(
                PlannedQuery(
                    adv_query,
                    purpose="advisory",
                    destination=", ".join(destinations[:dest_slots]),
                    engine="google",
                )
            )

        final_queries = query_candidates[:ceiling]
        if dest_count > 0:
            covered_destinations = [
                d
                for d in destinations
                if any(
                    q.destination == d or d in [x.strip() for x in q.destination.split(",")]
                    for q in final_queries
                    if q.purpose != "advisory"
                )
            ]
            uncovered_destinations = [d for d in destinations if d not in covered_destinations]
            if uncovered_destinations and not coverage_reason:
                dest_slots = len([q for q in final_queries if q.purpose != "advisory"])
                coverage_reason = (
                    f"Destination count ({dest_count}) exceeds available research query slots ({dest_slots}). "
                    f"Uncovered destinations: {', '.join(uncovered_destinations)} due to collector query ceiling ({ceiling})."
                )
        return PlannedQueryList(
            final_queries,
            covered_destinations=covered_destinations,
            uncovered_destinations=uncovered_destinations,
            coverage_reason=coverage_reason,
        )

    @classmethod
    def plan_attractions_and_hours_queries(
        cls,
        brief: dict[str, Any] | TripBrief,
        max_queries: int = DEFAULT_MAX_QUERIES,
    ) -> PlannedQueryList:
        """Plans bounded attractions and visit hours queries across destinations and must visits."""
        ceiling = min(max(1, max_queries), DEFAULT_MAX_QUERIES)
        destinations: list[str] = []
        must_visits: list[str] = []
        start_date_val: Any = None

        if isinstance(brief, TripBrief):
            destinations = [d.name.strip() for d in brief.destinations if d.name and d.name.strip()]
            must_visits = [p.strip() for p in brief.must_visit_places if p and p.strip()]
            start_date_val = brief.start_date
        elif isinstance(brief, dict):
            raw_dests = brief.get("destinations", [])
            if isinstance(raw_dests, list):
                for item in raw_dests:
                    if isinstance(item, dict) and item.get("name"):
                        destinations.append(str(item["name"]).strip())
                    elif hasattr(item, "name") and item.name:
                        destinations.append(str(item.name).strip())
                    elif isinstance(item, str) and item.strip():
                        destinations.append(item.strip())
            if not destinations and brief.get("destination_name"):
                destinations.append(str(brief["destination_name"]).strip())
            raw_mv = brief.get("must_visit_places", [])
            if isinstance(raw_mv, list):
                must_visits = [str(p).strip() for p in raw_mv if p and str(p).strip()]
            start_date_val = brief.get("start_date")

        date_context = ""
        if start_date_val is not None:
            if hasattr(start_date_val, "strftime"):
                date_context = f" {start_date_val.strftime('%B')}"
            elif isinstance(start_date_val, str) and len(start_date_val) >= 7:
                try:
                    date_context = f" {dt_date.fromisoformat(start_date_val[:10]).strftime('%B')}"
                except Exception:
                    pass

        primary = destinations[0] if destinations else "Destination"
        query_candidates: list[PlannedQuery] = []

        # Prioritize explicit traveler must visit hours within query budget
        for mv_place in must_visits:
            if len(query_candidates) >= max(1, ceiling // 2):
                break
            query_candidates.append(
                PlannedQuery(
                    f"{mv_place} {primary} opening hours timings last entry official",
                    purpose="hours",
                    destination=primary,
                    engine="google",
                )
            )

        # Cover primary destination attractions and official visiting hours
        if destinations:
            d0 = destinations[0]
            query_candidates.append(
                PlannedQuery(
                    f"{d0} top attractions sightseeing places to visit",
                    purpose="attractions",
                    destination=d0,
                    engine="google_maps",
                )
            )
            query_candidates.append(
                PlannedQuery(
                    f"{d0} tourist attractions opening hours timings closed day{date_context}".strip(),
                    purpose="hours",
                    destination=d0,
                    engine="google",
                )
            )
            for extra_dest in destinations[1:]:
                if len(query_candidates) >= ceiling:
                    break
                query_candidates.append(
                    PlannedQuery(
                        f"{extra_dest} attractions opening hours visit timings",
                        purpose="attractions",
                        destination=extra_dest,
                        engine="google_maps",
                    )
                )

            # Fill remaining budget slot with food research if traveler expressed culinary interest
            if len(query_candidates) < ceiling and destinations:
                raw_prefs = (
                    list(brief.dietary_preferences) + list(brief.traveler_preferences)
                    if isinstance(brief, TripBrief)
                    else list(brief.get("dietary_preferences", []))
                    + list(brief.get("traveler_preferences", []))
                )
                food_kw = next(
                    (
                        str(p)
                        for p in raw_prefs
                        if any(
                            k in str(p).lower()
                            for k in ("food", "dining", "cuisine", "veg", "sweet")
                        )
                    ),
                    None,
                )
                if food_kw:
                    d0 = destinations[0]
                    query_candidates.append(
                        PlannedQuery(
                            f"{d0} {food_kw} restaurants authentic local food",
                            purpose="dining",
                            destination=d0,
                            engine="google_maps",
                        )
                    )

        final_queries = query_candidates[:ceiling]
        covered = [
            d
            for d in destinations
            if any(
                q.destination == d or d in [x.strip() for x in q.destination.split(",")]
                for q in final_queries
            )
        ]
        uncovered = [d for d in destinations if d not in covered]
        coverage_reason: str | None = None
        if uncovered:
            coverage_reason = (
                f"Destination count ({len(destinations)}) exceeds available attractions and hours "
                f"query slots ({len(final_queries)}). Uncovered destinations: {', '.join(uncovered)} "
                f"due to collector query ceiling ({ceiling})."
            )
        elif not destinations:
            coverage_reason = "No destinations provided in trip brief."

        return PlannedQueryList(
            final_queries,
            covered_destinations=covered,
            uncovered_destinations=uncovered,
            coverage_reason=coverage_reason,
        )

    @classmethod
    def has_explicit_food_intent(cls, brief: dict[str, Any] | TripBrief) -> bool:
        """Determines whether a trip brief explicitly requests local food or dining research."""
        if isinstance(brief, TripBrief):
            raw_prefs = list(brief.dietary_preferences) + list(brief.traveler_preferences)
        elif isinstance(brief, dict):
            raw_prefs = list(brief.get("dietary_preferences") or []) + list(
                brief.get("traveler_preferences") or []
            )
        else:
            raw_prefs = []
        food_keywords = (
            "food",
            "cuisine",
            "dining",
            "culinary",
            "restaurant",
            "dish",
            "street food",
            "local food",
        )
        return any(any(kw in str(p).lower() for kw in food_keywords) for p in raw_prefs)

    @classmethod
    def plan_food_queries(
        cls,
        brief: dict[str, Any] | TripBrief,
        max_queries: int = DEFAULT_MAX_QUERIES,
    ) -> PlannedQueryList:
        """Plans bounded local food, regional dish, and authentic dining queries across destinations.

        Enforces:
        1. Query ceiling constraint (at most max_queries, capped at DEFAULT_MAX_QUERIES = 4).
        2. Formulates queries for regional culinary specialties and authentic dining venues.
        3. Incorporates traveler dietary preferences and culinary keywords from brief.
        4. Explicit destination coverage: tracks covered destinations, uncovered destinations,
           and records an explicit coverage reason when destination count exceeds available slots.
        """
        ceiling = min(max(1, max_queries), DEFAULT_MAX_QUERIES)
        destinations: list[str] = []
        dietary: list[str] = []
        traveler_prefs: list[str] = []

        if isinstance(brief, TripBrief):
            destinations = [d.name.strip() for d in brief.destinations if d.name and d.name.strip()]
            dietary = list(brief.dietary_preferences)
            traveler_prefs = list(brief.dietary_preferences) + list(brief.traveler_preferences)
        elif isinstance(brief, dict):
            raw_dests = brief.get("destinations", [])
            if isinstance(raw_dests, list):
                for item in raw_dests:
                    if isinstance(item, dict) and item.get("name"):
                        destinations.append(str(item["name"]).strip())
                    elif hasattr(item, "name") and item.name:
                        destinations.append(str(item.name).strip())
                    elif isinstance(item, str) and item.strip():
                        destinations.append(item.strip())
            if not destinations and brief.get("destination_name"):
                destinations.append(str(brief["destination_name"]).strip())
            raw_dietary = brief.get("dietary_preferences", [])
            if isinstance(raw_dietary, list):
                dietary = [str(x).strip() for x in raw_dietary if x and str(x).strip()]
            raw_prefs = brief.get("traveler_preferences", [])
            if isinstance(raw_prefs, list):
                traveler_prefs = [str(x).strip() for x in raw_prefs if x and str(x).strip()]

        # Food preferences context
        diet_context = ""
        if dietary:
            diet_context = f" {dietary[0]}"
        elif traveler_prefs:
            food_terms = [
                p
                for p in traveler_prefs
                if any(
                    k in str(p).lower()
                    for k in ("food", "dining", "cuisine", "street", "sweet", "veg", "dish")
                )
            ]
            if food_terms:
                diet_context = f" {food_terms[0]}"

        query_candidates: list[PlannedQuery] = []
        dest_count = len(destinations)

        if dest_count == 0:
            primary = "Unknown Destination"
            query_candidates.append(
                PlannedQuery(
                    f"{primary}{diet_context} famous local dishes authentic regional food specialties",
                    purpose="dining",
                    destination=primary,
                    engine="google_maps",
                )
            )
            query_candidates.append(
                PlannedQuery(
                    f"{primary}{diet_context} authentic local food restaurants iconic eateries",
                    purpose="dining",
                    destination=primary,
                    engine="google_maps",
                )
            )
        elif dest_count == 1:
            d0 = destinations[0]
            query_candidates.append(
                PlannedQuery(
                    f"{d0}{diet_context} famous local dishes authentic regional food specialties",
                    purpose="dining",
                    destination=d0,
                    engine="google_maps",
                )
            )
            query_candidates.append(
                PlannedQuery(
                    f"{d0}{diet_context} authentic local food restaurants iconic eateries",
                    purpose="dining",
                    destination=d0,
                    engine="google_maps",
                )
            )
            query_candidates.append(
                PlannedQuery(
                    f"{d0}{diet_context} street food sweet shops traditional cuisine",
                    purpose="dining",
                    destination=d0,
                    engine="google_maps",
                )
            )
        elif dest_count == 2:
            d0, d1 = destinations[0], destinations[1]
            query_candidates.append(
                PlannedQuery(
                    f"{d0}{diet_context} famous local dishes authentic regional food specialties",
                    purpose="dining",
                    destination=d0,
                    engine="google_maps",
                )
            )
            query_candidates.append(
                PlannedQuery(
                    f"{d0}{diet_context} authentic local food restaurants iconic eateries",
                    purpose="dining",
                    destination=d0,
                    engine="google_maps",
                )
            )
            query_candidates.append(
                PlannedQuery(
                    f"{d1}{diet_context} famous local dishes authentic regional food specialties",
                    purpose="dining",
                    destination=d1,
                    engine="google_maps",
                )
            )
            query_candidates.append(
                PlannedQuery(
                    f"{d1}{diet_context} authentic local food restaurants iconic eateries",
                    purpose="dining",
                    destination=d1,
                    engine="google_maps",
                )
            )
        else:
            # dest_count >= 3: Distribute queries across destinations up to ceiling
            for dest_name_str in destinations[:ceiling]:
                query_candidates.append(
                    PlannedQuery(
                        f"{dest_name_str}{diet_context} authentic local food restaurants regional dishes",
                        purpose="dining",
                        destination=dest_name_str,
                        engine="google_maps",
                    )
                )

        final_queries = query_candidates[:ceiling]
        covered = [
            d
            for d in destinations
            if any(
                q.destination == d or d in [x.strip() for x in q.destination.split(",")]
                for q in final_queries
            )
        ]
        uncovered = [d for d in destinations if d not in covered]
        coverage_reason: str | None = None
        if uncovered:
            coverage_reason = (
                f"Destination count ({len(destinations)}) exceeds available food research "
                f"query slots ({len(final_queries)}). Uncovered destinations: {', '.join(uncovered)} "
                f"due to collector query ceiling ({ceiling})."
            )
        elif not destinations:
            coverage_reason = "No destinations provided in trip brief."

        return PlannedQueryList(
            final_queries,
            covered_destinations=covered,
            uncovered_destinations=uncovered,
            coverage_reason=coverage_reason,
        )

    @classmethod
    def structure_and_validate_queries(
        cls,
        queries: list[Any],
        destinations: list[str],
        origin: str = "",
        max_queries: int = DEFAULT_MAX_QUERIES,
    ) -> PlannedQueryList:
        """Validates and structures raw or LLM-generated search queries.

        Enforces:
        1. Query ceiling constraint (at most max_queries).
        2. Road advisory queries route to web search (engine="google").
        3. Venue discovery queries route to place search (engine="google_maps").
        4. Destination coverage: checks that brief destinations are represented up to budget.
        5. Records uncovered destinations and explicit budget reason if destination count > slots.
        """
        ceiling = min(max_queries, DEFAULT_MAX_QUERIES)
        clean_dests = [d.strip() for d in destinations if d and d.strip()]
        dest_count = len(clean_dests)
        dest_slots = max(1, ceiling - 1) if dest_count > 0 else ceiling

        structured: list[PlannedQuery] = []
        covered_set: set[str] = set()

        for q_item in queries:
            if len(structured) >= ceiling:
                break

            q_text = str(q_item.get("query") if isinstance(q_item, dict) else q_item).strip()
            if not q_text:
                continue

            q_lower = q_text.lower()
            explicit_purpose = q_item.get("purpose") if isinstance(q_item, dict) else None
            explicit_dest = q_item.get("destination") if isinstance(q_item, dict) else None

            # 1. Purpose determination:
            # Explicit purpose is authoritative. Keyword inference must NOT be the authority.
            if explicit_purpose is not None:
                norm_purpose = str(explicit_purpose).strip().lower()
                if norm_purpose in ("attractions", "hours", "dining", "lodging", "advisory"):
                    purpose = norm_purpose
                else:
                    # Unsupported purpose value -> safely normalize to attractions
                    purpose = "attractions"
            else:
                # Legacy plain string fallback (no explicit purpose metadata):
                # Only if the query explicitly indicates road conditions/advisories does it fallback to advisory;
                # otherwise safe default fallback is attractions (venue discovery).
                if any(
                    phrase in q_lower
                    for phrase in (
                        "road travel advisory",
                        "road condition",
                        "road closures",
                        "highway alert",
                        "traffic advisory",
                        "route condition",
                    )
                ):
                    purpose = "advisory"
                else:
                    purpose = "attractions"

            # 2. Explicit purpose determines permitted search engine:
            if purpose in ("advisory", "hours"):
                engine = "google"
            else:
                engine = "google_maps"

            # 3. Destination determination:
            if explicit_dest:
                dest_str = str(explicit_dest).strip()
            else:
                matched_dests = [d for d in clean_dests if d.lower() in q_lower]
                if purpose == "advisory":
                    dest_str = (
                        ", ".join(matched_dests)
                        if matched_dests
                        else (", ".join(clean_dests) if clean_dests else "corridor")
                    )
                else:
                    dest_str = (
                        matched_dests[0]
                        if matched_dests
                        else (clean_dests[0] if clean_dests else "")
                    )

            if purpose != "advisory" and dest_str in clean_dests:
                covered_set.add(dest_str)

            structured.append(
                PlannedQuery(
                    q_text,
                    purpose=purpose,
                    destination=dest_str,
                    engine=engine,
                )
            )

        # Coverage enforcement: check if any destination in clean_dests up to dest_slots is missing
        missing_dests = [d for d in clean_dests[:dest_slots] if d not in covered_set]
        if missing_dests:
            for missing_d in missing_dests:
                new_q = PlannedQuery(
                    f"{missing_d} top attractions sightseeing places to visit",
                    purpose="attractions",
                    destination=missing_d,
                    engine="google_maps",
                )
                if len(structured) < ceiling:
                    structured.append(new_q)
                    covered_set.add(missing_d)
                else:
                    # Replace a duplicate query from an already covered destination
                    replaced = False
                    for idx, existing_q in enumerate(structured):
                        if (
                            existing_q.purpose != "advisory"
                            and existing_q.destination in covered_set
                            and existing_q.destination != missing_d
                        ):
                            dup_count = sum(
                                1
                                for x in structured
                                if x.destination == existing_q.destination
                                and x.purpose != "advisory"
                            )
                            if dup_count > 1:
                                structured[idx] = new_q
                                covered_set.add(missing_d)
                                replaced = True
                                break
                    if not replaced:
                        # Replace the last non-advisory query
                        for idx in range(len(structured) - 1, -1, -1):
                            if structured[idx].purpose != "advisory":
                                structured[idx] = new_q
                                covered_set.add(missing_d)
                                break

        final_queries = structured[:ceiling]
        covered_destinations = [
            d
            for d in clean_dests
            if any(
                q.destination == d or d in [x.strip() for x in q.destination.split(",")]
                for q in final_queries
                if q.purpose != "advisory"
            )
        ]
        uncovered_destinations = [d for d in clean_dests if d not in covered_destinations]
        coverage_reason = None
        if uncovered_destinations:
            coverage_reason = (
                f"Destination count ({dest_count}) exceeds available research query slots ({dest_slots}). "
                f"Uncovered destinations: {', '.join(uncovered_destinations)} due to collector query ceiling ({ceiling})."
            )
        elif dest_count == 0:
            coverage_reason = "No destinations provided in trip brief."

        return PlannedQueryList(
            final_queries,
            covered_destinations=covered_destinations,
            uncovered_destinations=uncovered_destinations,
            coverage_reason=coverage_reason,
        )

    @classmethod
    async def plan_queries_with_llm(
        cls,
        brief: dict[str, Any] | TripBrief,
        llm_client: Any | None = None,
        max_queries: int = DEFAULT_MAX_QUERIES,
    ) -> PlannedQueryList:
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
                queries = await llm_client.generate_search_queries(req, max_queries=max_queries)
                if queries:
                    return cls.structure_and_validate_queries(
                        queries=list(queries),
                        destinations=destinations,
                        origin=origin,
                        max_queries=max_queries,
                    )
            except Exception as exc:
                logger.warning("LLM query planning failed (%s); using deterministic fallback", exc)

        return cls.plan_destination_queries(brief, max_queries)

    @classmethod
    def calculate_collector_budget(
        cls,
        queries: list[Any],
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
