from __future__ import annotations

from datetime import datetime

import pytest

from functions.optimizer.routing import get_route_geometry, get_transit_matrix
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
    assert route.stops[0].stop_type == "ORIGIN"
    assert route.stops[1].stop_type == "DESTINATION"
    assert route.stops[2].stop_type == "ORIGIN"
    assert route.legs[0].mode == "car"
    assert route.legs[1].mode == "car"


def test_parking_and_walking_behavior() -> None:
    """Verifies that a destination requiring parking injects parking hubs, walking legs, and preserves vehicle continuity."""
    route = ItineraryScheduler.solve_schedule(
        origin_name="Bengaluru",
        origin_lat=12.9716,
        origin_lng=77.5946,
        stops=[
            {
                "name": "Mysuru Palace",
                "lat": 12.3051,
                "lng": 76.6551,
                "stay_minutes": 120,
                "requires_parking": True,
                "parking_name": "Mysuru Palace South Parking Hub",
                "parking_lat": 12.3025,
                "parking_lng": 76.6560,
            }
        ],
        start_datetime=datetime.fromisoformat("2026-11-06T09:00:00+05:30"),
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

    stop_names = [s.name for s in route.stops]
    assert stop_names == [
        "Bengaluru",
        "Mysuru Palace South Parking Hub",
        "Mysuru Palace",
        "Mysuru Palace South Parking Hub",
        "Bengaluru",
    ]

    # Verify stop types
    assert route.stops[0].stop_type == "ORIGIN"
    assert route.stops[1].stop_type == "PARKING_HUB"
    assert route.stops[2].stop_type == "DESTINATION"
    assert route.stops[3].stop_type == "PARKING_HUB"
    assert route.stops[4].stop_type == "ORIGIN"

    # Verify legs and modes
    # Leg 0: Bengaluru -> Parking Hub (car)
    assert route.legs[0].from_stop == "Bengaluru"
    assert route.legs[0].to_stop == "Mysuru Palace South Parking Hub"
    assert route.legs[0].mode == "car"
    assert route.legs[0].fuel_cost_inr > 0

    # Leg 1: Parking Hub -> Mysuru Palace (walk)
    assert route.legs[1].from_stop == "Mysuru Palace South Parking Hub"
    assert route.legs[1].to_stop == "Mysuru Palace"
    assert route.legs[1].mode == "walk"
    assert route.legs[1].fuel_cost_inr == 0.0
    assert route.legs[1].toll_cost_inr == 0.0

    # Leg 2: Mysuru Palace -> Parking Hub (walk back to car)
    assert route.legs[2].from_stop == "Mysuru Palace"
    assert route.legs[2].to_stop == "Mysuru Palace South Parking Hub"
    assert route.legs[2].mode == "walk"
    assert route.legs[2].fuel_cost_inr == 0.0
    assert route.legs[2].toll_cost_inr == 0.0

    # Leg 3: Parking Hub -> Bengaluru (car return home)
    assert route.legs[3].from_stop == "Mysuru Palace South Parking Hub"
    assert route.legs[3].to_stop == "Bengaluru"
    assert route.legs[3].mode == "car"
    assert route.legs[3].fuel_cost_inr > 0


@pytest.mark.asyncio
async def test_osrm_transit_matrix_and_route_geometry() -> None:
    """Verifies that transit matrix and GeoJSON geometry use OpenStreetMap/OSRM."""
    coords = [(12.9716, 77.5946), (12.2958, 76.6394)]  # Bengaluru to Mysuru
    time_mat, dist_mat = await get_transit_matrix(coords)

    # Bengaluru to Mysuru is ~140-150 km, ~110-130 mins via OSRM
    assert len(time_mat) == 2
    assert len(dist_mat) == 2
    assert 130.0 < dist_mat[0][1] < 160.0
    assert 90 < time_mat[0][1] < 150

    # Inspect returned map GeoJSON
    geojson = await get_route_geometry(coords)
    assert geojson["type"] == "Feature"
    assert geojson["properties"]["provider"] == "osrm"
    assert geojson["properties"]["routing_basis"] == "openstreetmap"
    assert geojson["geometry"]["type"] == "LineString"
    coordinates = geojson["geometry"]["coordinates"]
    assert len(coordinates) > 100  # Detailed turn-by-turn geometry

    # GeoJSON coordinates format is [longitude, latitude]
    for pt in coordinates[:5]:
        assert len(pt) == 2
        # Bengaluru longitude is ~77.5-77.6, latitude is ~12.9-13.0
        assert 70.0 < pt[0] < 85.0
        assert 10.0 < pt[1] < 20.0


@pytest.mark.asyncio
async def test_changed_road_condition_rerouting() -> None:
    """Verifies that changing a road condition recalculates the itinerary and GeoJSON via OSRM."""
    # Baseline direct route: Bengaluru -> Mysuru (via expressway NH 275)
    direct_coords = [(12.9716, 77.5946), (12.2958, 76.6394)]
    direct_time_mat, direct_dist_mat = await get_transit_matrix(direct_coords)
    direct_dist = direct_dist_mat[0][1]
    direct_geojson = await get_route_geometry(direct_coords)

    # Changed road condition: highway closed, detour via Kanakapura (12.5463, 77.4208)
    rerouted_coords = [(12.9716, 77.5946), (12.5463, 77.4208), (12.2958, 76.6394)]
    rerouted_time_mat, rerouted_dist_mat = await get_transit_matrix(rerouted_coords)
    rerouted_dist = rerouted_dist_mat[0][1] + rerouted_dist_mat[1][2]
    rerouted_geojson = await get_route_geometry(rerouted_coords)

    # The detour route must be longer than the direct route
    assert rerouted_dist > direct_dist + 20.0  # At least 20 km longer
    assert len(rerouted_geojson["geometry"]["coordinates"]) > 100
    assert rerouted_geojson["geometry"]["coordinates"] != direct_geojson["geometry"]["coordinates"]
    assert rerouted_geojson["properties"]["provider"] == "osrm"
