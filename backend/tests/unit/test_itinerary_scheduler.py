from datetime import UTC, datetime

import pytest

from functions.optimizer.solver import ItineraryScheduler


def test_solver_keeps_requested_media_endpoint_as_final_stop() -> None:
    schedule = ItineraryScheduler.solve_schedule(
        origin_name="Bengaluru",
        origin_lat=12.9716,
        origin_lng=77.5946,
        stops=[
            {"name": "Media stop", "lat": 12.0, "lng": 77.0},
            {"name": "Final media destination", "lat": 11.0, "lng": 76.0},
        ],
        start_datetime=datetime(2026, 11, 1, 9, tzinfo=UTC),
        transit_matrix=[
            [0, 1, 2],
            [1, 0, 10],
            [2, 1, 0],
        ],
        distance_matrix=[
            [0.0, 1.0, 2.0],
            [1.0, 0.0, 10.0],
            [2.0, 1.0, 0.0],
        ],
        end_node_index=2,
    )

    assert [stop.name for stop in schedule.stops][-1] == "Final media destination"


def test_solver_rejects_an_invalid_final_destination_index() -> None:
    with pytest.raises(ValueError, match="route endpoint"):
        ItineraryScheduler.solve_schedule(
            origin_name="Bengaluru",
            origin_lat=12.9716,
            origin_lng=77.5946,
            stops=[{"name": "Mysuru", "lat": 12.2958, "lng": 76.6394}],
            start_datetime=datetime(2026, 11, 1, 9, tzinfo=UTC),
            end_node_index=2,
        )
