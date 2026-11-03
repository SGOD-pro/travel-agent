from types import SimpleNamespace

from functions.api.routes.jobs import _extract_trip_plan, _safe_failure_message


def test_extract_trip_plan_returns_only_the_proposal() -> None:
    proposal = {
        "schedule": {"stops": [{"name": "Mysuru"}]},
        "budget_summary": {"known_total": {"amount": "5000", "currency": "INR"}},
    }
    state = SimpleNamespace(
        values={
            "product_outputs": {
                "media": {"analyzed": {"full_transcript": "private caption text"}},
                "travel": {"proposal": proposal},
            }
        }
    )

    assert _extract_trip_plan(state) == {
        "travel_plan": proposal,
        "destination_source": "trip_brief",
    }


def test_extract_trip_plan_is_none_when_travel_was_not_requested() -> None:
    state = SimpleNamespace(values={"product_outputs": {"media": {"verified_places": []}}})

    assert _extract_trip_plan(state) is None


def test_extract_trip_plan_exposes_route_only_composition_results() -> None:
    route = {
        "stops": [{"name": "Bengaluru"}],
        "legs": [],
        "geometry_geojson": {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[77.5, 12.9], [77.6, 13.0]]},
        },
    }

    assert _extract_trip_plan(
        SimpleNamespace(values={"product_outputs": {"route": {"optimized_route": route}}})
    ) == {"route": route}


def test_extract_trip_plan_reads_standalone_travel_workflow_state() -> None:
    proposal = {"schedule": {"stops": [{"name": "Mysuru"}]}}

    assert _extract_trip_plan(SimpleNamespace(values={"proposal": proposal})) == {
        "travel_plan": proposal,
        "destination_source": "trip_brief",
    }


def test_safe_failure_message_only_exposes_known_user_actionable_errors() -> None:
    safe_event = SimpleNamespace(
        event_type="EXECUTION_FAILED",
        safe_payload={"reason": "This YouTube video has no publicly accessible captions."},
    )
    unsafe_event = SimpleNamespace(
        event_type="EXECUTION_FAILED",
        safe_payload={"reason": "Database password leaked in exception"},
    )

    assert _safe_failure_message([safe_event]) == safe_event.safe_payload["reason"]
    assert _safe_failure_message([unsafe_event]) is None
