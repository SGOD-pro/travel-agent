"""PostgreSQL workflow regressions for hidden-place claim ingestion, eligibility, and scheduling."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest

from functions.api.models import TripModel
from functions.evidence_collector.models import SourceObservationModel
from functions.orchestrator.execution.models import ExecutionModel
from functions.orchestrator.graphs.trip_planner.graph import (
    TripPlanningState,
    dispatch_route_optimizer_node,
    evaluate_candidates_node,
    synthesize_proposal_node,
)


async def _seed_execution(session: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    owner_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    now = datetime.now(UTC)

    session.add(
        TripModel(
            id=trip_id,
            owner_id=owner_id,
            current_version=1,
            state="draft",
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    session.add(
        ExecutionModel(
            id=exec_id,
            owner_id=owner_id,
            trip_id=trip_id,
            workflow="TRAVEL",
            status="RUNNING",
            context_version=1,
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    return exec_id, owner_id, trip_id


class TestSourceBackedHiddenPlacesPostgres:
    """End-to-end workflow tests from hidden-place observations through claim gate to optimizer."""

    @pytest.mark.asyncio
    async def test_verified_hidden_place_reaches_optimizer_while_restricted_and_unverified_stay_excluded(
        self, session_factory: Any
    ) -> None:
        async with session_factory() as session:
            exec_id, owner_id, trip_id = await _seed_execution(session)
            now = datetime.now(UTC)

            # 1. Traveler must-visit: Mysuru Palace (with fetched hours)
            obs_palace = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://mysurupalace.karnataka.gov.in/timings",
                canonical_url="https://mysurupalace.karnataka.gov.in/timings",
                publisher="mysurupalace.karnataka.gov.in",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                extraction_method="SCRAPLING_HTTP",
                excerpt_text="Mysuru Palace in Mysuru is open daily from 10:00 AM to 5:30 PM.",
                excerpt_locator="body",
                artifact_ref={
                    "title": "Mysuru Palace",
                    "address": "Sayyaji Rao Rd, Mysuru, Karnataka",
                    "gps_coordinates": {"latitude": 12.3051, "longitude": 76.6551},
                    "query_purpose": "hours",
                    "destination": "Mysuru",
                },
            )
            # 2. Verified hidden place: Melody World Wax Museum (fetched SCRAPLING_HTTP, explicit public access, coords)
            obs_melody = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://karnatakatourism.org/melody-world-mysuru",
                canonical_url="https://karnatakatourism.org/melody-world-mysuru",
                publisher="karnatakatourism.org",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                extraction_method="SCRAPLING_HTTP",
                excerpt_text=(
                    "Melody World Wax Museum in Mysuru is an offbeat wax and musical instrument "
                    "museum open to visitors daily."
                ),
                excerpt_locator="body",
                artifact_ref={
                    "title": "Melody World Wax Museum",
                    "address": "Kurubarahalli, Mysuru, Karnataka",
                    "gps_coordinates": {"latitude": 12.2974, "longitude": 76.6852},
                    "query_purpose": "hidden_places",
                    "destination": "Mysuru",
                },
            )
            # Duplicate Melody World observation from attractions search to test deduplication
            obs_melody_dup = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://mysurutourism.example/melody-world",
                canonical_url="https://mysurutourism.example/melody-world",
                publisher="mysurutourism.example",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                extraction_method="SCRAPLING_HTTP",
                excerpt_text="Melody World Wax Museum in Mysuru is open to visitors daily.",
                excerpt_locator="body",
                artifact_ref={
                    "title": "Melody World Wax Museum",
                    "address": "Kurubarahalli, Mysuru, Karnataka",
                    "gps_coordinates": {"latitude": 12.2974, "longitude": 76.6852},
                    "query_purpose": "attractions",
                    "destination": "Mysuru",
                },
            )
            # 3. Restricted hidden place: Heritage Bungalow (even with SCRAPLING_HTTP and coords, private/no public access)
            obs_restricted = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://mysuruhistory.example/heritage-bungalow",
                canonical_url="https://mysuruhistory.example/heritage-bungalow",
                publisher="mysuruhistory.example",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                extraction_method="SCRAPLING_HTTP",
                excerpt_text="Heritage Bungalow in Mysuru is a private property with no public access.",
                excerpt_locator="body",
                artifact_ref={
                    "title": "Heritage Bungalow",
                    "address": "Nazarbad, Mysuru, Karnataka",
                    "gps_coordinates": {"latitude": 12.3100, "longitude": 76.6650},
                    "query_purpose": "hidden_places",
                    "destination": "Mysuru",
                },
            )
            # 4. Snippet-only hidden place: Secret Stepwell
            obs_snippet = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://blog.example/secret-stepwell",
                canonical_url="https://blog.example/secret-stepwell",
                publisher="blog.example",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                extraction_method="SERPAPI_WEB_SEARCH",
                excerpt_text="Secret Stepwell in Mysuru is open to the public.",
                excerpt_locator="snippet",
                artifact_ref={
                    "title": "Secret Stepwell",
                    "address": "Mysuru, Karnataka",
                    "gps_coordinates": {"latitude": 12.3010, "longitude": 76.6410},
                    "query_purpose": "hidden_places",
                    "destination": "Mysuru",
                },
            )
            # 5. Unknown-access fetched hidden place: Whispering Courtyard
            obs_unknown = SourceObservationModel(
                id=uuid.uuid4(),
                execution_id=exec_id,
                source_url="https://mysuruwalk.example/whispering-courtyard",
                canonical_url="https://mysuruwalk.example/whispering-courtyard",
                publisher="mysuruwalk.example",
                content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                retrieved_at=now,
                extraction_method="SCRAPLING_HTTP",
                excerpt_text="Whispering Courtyard in Mysuru is a quiet 19th century courtyard.",
                excerpt_locator="body",
                artifact_ref={
                    "title": "Whispering Courtyard",
                    "address": "Mysuru, Karnataka",
                    "gps_coordinates": {"latitude": 12.3020, "longitude": 76.6520},
                    "query_purpose": "hidden_places",
                    "destination": "Mysuru",
                },
            )

            session.add_all(
                [obs_palace, obs_melody, obs_melody_dup, obs_restricted, obs_snippet, obs_unknown]
            )
            await session.flush()

            brief = {
                "origin_name": "Mysuru Junction",
                "origin_lat": 12.3119,
                "origin_lng": 76.6454,
                "destinations": [{"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394}],
                "must_visit_places": ["Mysuru Palace"],
                "start_date": date(2026, 12, 10).isoformat(),
                "end_date": date(2026, 12, 10).isoformat(),
                "include_hidden_places": True,
                "timezone": "Asia/Kolkata",
            }
            obs_dicts = [
                {
                    "id": str(o.id),
                    "source_url": o.source_url,
                    "canonical_url": o.canonical_url,
                    "publisher": o.publisher,
                    "extraction_method": o.extraction_method,
                    "content_hash": o.content_hash,
                    "retrieved_at": o.retrieved_at.isoformat(),
                    "excerpt_text": o.excerpt_text,
                    "artifact_ref": o.artifact_ref,
                }
                for o in (obs_palace, obs_melody, obs_melody_dup, obs_restricted, obs_snippet, obs_unknown)
            ]

            state: TripPlanningState = cast(
                TripPlanningState,
                {
                    "execution_id": str(exec_id),
                    "owner_id": str(owner_id),
                    "trip_id": str(trip_id),
                    "brief": brief,
                    "observations": obs_dicts,
                    "task_outputs": {"collect-1": {"observations": obs_dicts}},
                    "_db_session": session,
                },
            )

            eval_res = await evaluate_candidates_node(state)
            eligible_names = [s["primary_name"] for s in eval_res["eligible_stops"]]

            # Deduplicated Melody World Wax Museum qualifies as optional stop; Palace stays must-visit
            assert eligible_names.count("Melody World Wax Museum") == 1
            melody_stop = next(s for s in eval_res["eligible_stops"] if s["primary_name"] == "Melody World Wax Museum")
            assert melody_stop["is_must_visit"] is False

            palace_stop = next(s for s in eval_res["eligible_stops"] if s["primary_name"] == "Mysuru Palace")
            assert palace_stop["is_must_visit"] is True

            # Restricted, snippet-only, and unknown-access hidden places never enter eligible_stops
            assert "Heritage Bungalow" not in eligible_names
            assert "Secret Stepwell" not in eligible_names
            assert "Whispering Courtyard" not in eligible_names

            # Feed eligible_stops into dispatch_route_optimizer_node
            solve_state = cast(
                TripPlanningState,
                {
                    **state,
                    **eval_res,
                    "_offline_fixture_mode": True,
                },
            )
            solve_res = dispatch_route_optimizer_node(solve_state)

            # Verify Melody World was offered to optimizer dispatch as optional stop (is_must_visit=False)
            # while Mysuru Palace stays mandatory and restricted/unverified places stay out
            opt_stops = solve_res["pending_tasks"][0]["payload"]["stops"]
            opt_by_name = {s["name"]: s for s in opt_stops}
            assert "Mysuru Palace" in opt_by_name
            assert opt_by_name["Mysuru Palace"]["is_must_visit"] is True
            assert opt_by_name["Mysuru Palace"]["window_start"] is not None
            assert opt_by_name["Mysuru Palace"]["window_end"] is not None
            assert "Melody World Wax Museum" in opt_by_name
            assert opt_by_name["Melody World Wax Museum"]["is_must_visit"] is False
            assert "Heritage Bungalow" not in opt_by_name
            assert "Secret Stepwell" not in opt_by_name
            assert "Whispering Courtyard" not in opt_by_name

            # Verify retention when routing is PARTIAL / ROUTING_UNAVAILABLE
            partial_state = cast(
                TripPlanningState,
                {
                    **solve_state,
                    **solve_res,
                    "routing_status": "ROUTING_UNAVAILABLE",
                    "route_status": "PARTIAL",
                },
            )
            synth_res = synthesize_proposal_node(partial_state)
            hpr = synth_res["proposal"]["hidden_place_research"]
            assert any(v["name"] == "Melody World Wax Museum" for v in hpr["verified_optional_stops"])
            assert any(r["name"] == "Heritage Bungalow" for r in hpr["restricted_or_closed"])
            assert synth_res["proposal"]["status"] == "partial"
