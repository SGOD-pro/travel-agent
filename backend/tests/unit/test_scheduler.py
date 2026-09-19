"""Unit tests for OR-Tools itinerary scheduling solver."""

from datetime import UTC, datetime

from travel.application.services.scheduler import ItineraryScheduler


def test_ortools_schedule_solver_ordering() -> None:
    start_dt = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    stops = [
        {"name": "Mysuru Palace", "lat": 12.3052, "lng": 76.6552, "stay_minutes": 120},
        {"name": "Chamundi Hills", "lat": 12.2741, "lng": 76.6710, "stay_minutes": 90},
        {"name": "Brindavan Gardens", "lat": 12.4239, "lng": 76.5724, "stay_minutes": 150},
    ]

    schedule = ItineraryScheduler.solve_schedule(
        origin_name="Bengaluru",
        origin_lat=12.9716,
        origin_lng=77.5946,
        stops=stops,
        start_datetime=start_dt,
    )

    # 1 origin + 3 stops = 4 scheduled stops
    assert len(schedule.stops) == 4
    assert len(schedule.legs) == 3
    assert schedule.stops[0].name == "Bengaluru"

    # Verify timestamps advance strictly monotonically
    for i in range(len(schedule.stops) - 1):
        assert schedule.stops[i].departure_time <= schedule.stops[i + 1].arrival_time
        assert schedule.stops[i + 1].arrival_time <= schedule.stops[i + 1].departure_time
