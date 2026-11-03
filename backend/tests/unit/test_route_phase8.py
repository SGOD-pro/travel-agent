from __future__ import annotations

from datetime import datetime

from functions.optimizer.solver import ItineraryScheduler


def test_return_to_origin_adds_a_final_origin_stop_and_leg() -> None:
    route = ItineraryScheduler.solve_schedule(
        origin_name="Bengaluru",
        origin_lat=12.9716,
        origin_lng=77.5946,
        stops=[
            {
                "name": "Mysuru",
                "lat": 12.2958,
                "lng": 76.6394,
                "stay_minutes": 30,
            }
        ],
        start_datetime=datetime.fromisoformat("2026-11-02T09:00:00+05:30"),
        transit_matrix=[
            [0, 150, 150],
            [150, 0, 150],
            [150, 150, 0],
        ],
        distance_matrix=[
            [0.0, 145.0, 145.0],
            [145.0, 0.0, 145.0],
            [145.0, 145.0, 0.0],
        ],
        return_to_origin=True,
    )

    assert [stop.name for stop in route.stops] == ["Bengaluru", "Mysuru", "Bengaluru"]
    assert route.return_to_origin is True
    assert len(route.legs) == 2
