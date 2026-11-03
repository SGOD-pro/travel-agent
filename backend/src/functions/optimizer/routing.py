"""Google Maps driving-matrix adapter for route optimization."""

from __future__ import annotations

import json

import httpx

from config.config import get_settings

_ELEMENTS_PER_REQUEST = 100


def _avoid_options(request: dict[str, object] | None) -> str | None:
    if not request:
        return None
    options = [
        name
        for enabled, name in (
            (request.get("avoid_tolls"), "tolls"),
            (request.get("avoid_highways"), "highways"),
            (request.get("avoid_ferries"), "ferries"),
        )
        if enabled
    ]
    return "|".join(options) if options else None


def _route_modifiers(request: dict[str, object] | None) -> dict[str, bool]:
    request = request or {}
    return {
        "avoidTolls": bool(request.get("avoid_tolls")),
        "avoidHighways": bool(request.get("avoid_highways")),
        "avoidFerries": bool(request.get("avoid_ferries")),
    }


def _waypoint(coordinate: tuple[float, float]) -> dict[str, object]:
    return {
        "waypoint": {
            "location": {
                "latLng": {
                    "latitude": coordinate[0],
                    "longitude": coordinate[1],
                }
            }
        }
    }


def _duration_seconds(value: object) -> int:
    if not isinstance(value, str) or not value.endswith("s"):
        raise RuntimeError("Google Routes returned an invalid duration.")
    try:
        return int(float(value[:-1]))
    except ValueError as exc:
        raise RuntimeError("Google Routes returned an invalid duration.") from exc


def _provider_http_error(response: httpx.Response, product: str) -> RuntimeError:
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = f"HTTP {response.status_code}"
    return RuntimeError(f"Google Routes {product} request failed: {message}")


async def get_transit_matrix(
    coordinates: list[tuple[float, float]],
    average_speed_kmh: float = 45.0,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    route_options: dict[str, object] | None = None,
) -> tuple[list[list[int]], list[list[float]]]:
    """Fetch driving durations and distances; never substitute straight-line estimates."""
    del average_speed_kmh
    settings = get_settings()
    if not settings.GOOGLE_MAPS_API_KEY:
        raise RuntimeError("GOOGLE_MAPS_API_KEY is required for route optimization.")
    if not coordinates:
        raise ValueError("At least one coordinate is required to build a route matrix.")

    size = len(coordinates)
    time_matrix = [[0] * size for _ in range(size)]
    distance_matrix = [[0.0] * size for _ in range(size)]
    chunk_size = int(_ELEMENTS_PER_REQUEST**0.5)
    async with httpx.AsyncClient(
        timeout=settings.HTTP_REQUEST_TIMEOUT_SECONDS,
        trust_env=False,
        transport=transport,
    ) as client:
        for origin_start in range(0, size, chunk_size):
            origin_end = min(origin_start + chunk_size, size)
            for destination_start in range(0, size, chunk_size):
                destination_end = min(destination_start + chunk_size, size)
                origins = coordinates[origin_start:origin_end]
                destinations = coordinates[destination_start:destination_end]
                if len(origins) * len(destinations) > _ELEMENTS_PER_REQUEST:
                    raise RuntimeError("Route matrix request exceeded the provider element limit.")
                try:
                    body: dict[str, object] = {
                        "origins": [_waypoint(coordinate) for coordinate in origins],
                        "destinations": [_waypoint(coordinate) for coordinate in destinations],
                        "travelMode": "DRIVE",
                        "routingPreference": "TRAFFIC_AWARE",
                    }
                    modifiers = _route_modifiers(route_options)
                    if any(modifiers.values()):
                        body["routeModifiers"] = modifiers
                    response = await client.post(
                        settings.GOOGLE_MAPS_MATRIX_URL,
                        headers={
                            "X-Goog-Api-Key": settings.GOOGLE_MAPS_API_KEY,
                            "X-Goog-FieldMask": (
                                "originIndex,destinationIndex,"
                                "condition,distanceMeters,duration"
                            ),
                            "Content-Type": "application/json",
                        },
                        json=body,
                    )
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise _provider_http_error(exc.response, "matrix") from None
                except httpx.RequestError:
                    raise RuntimeError("Google Maps route request failed.") from None
                entries = [
                    json.loads(line)
                    for line in response.text.splitlines()
                    if line.strip()
                ]
                expected = len(origins) * len(destinations)
                if len(entries) != expected:
                    raise RuntimeError("Google Routes returned an incomplete route matrix.")
                for entry in entries:
                    try:
                        local_origin = int(entry["originIndex"])
                        local_destination = int(entry["destinationIndex"])
                        distance = float(entry["distanceMeters"])
                        duration = _duration_seconds(entry["duration"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise RuntimeError(
                            "Google Routes returned invalid route duration or distance."
                        ) from exc
                    if entry.get("condition") != "ROUTE_EXISTS":
                        raise RuntimeError(
                            "Google Routes could not find a driving route for a destination pair."
                        )
                    origin_index = origin_start + local_origin
                    destination_index = destination_start + local_destination
                    if origin_index == destination_index:
                        continue
                    if duration <= 0 or distance <= 0:
                        raise RuntimeError("Google Routes returned a non-positive route leg.")
                    time_matrix[origin_index][destination_index] = max(1, duration // 60)
                    distance_matrix[origin_index][destination_index] = round(distance / 1000, 2)

    return time_matrix, distance_matrix


def _decode_polyline(encoded: str) -> list[list[float]]:
    points: list[list[float]] = []
    index = lat = lng = 0
    while index < len(encoded):
        result = shift = 0
        while True:
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        lat += ~(result >> 1) if result & 1 else result >> 1
        result = shift = 0
        while True:
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        lng += ~(result >> 1) if result & 1 else result >> 1
        points.append([lng / 1e5, lat / 1e5])
    return points


async def get_route_geometry(
    coordinates: list[tuple[float, float]],
    *,
    route_options: dict[str, object] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    """Fetch the selected route geometry as GeoJSON LineString coordinates."""
    settings = get_settings()
    if not settings.GOOGLE_MAPS_API_KEY:
        raise RuntimeError("GOOGLE_MAPS_API_KEY is required for route geometry.")
    if len(coordinates) < 2:
        raise ValueError("At least two coordinates are required for route geometry.")
    body: dict[str, object] = {
        "origin": _waypoint(coordinates[0])["waypoint"],
        "destination": _waypoint(coordinates[-1])["waypoint"],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "polylineQuality": "OVERVIEW",
    }
    if len(coordinates) > 2:
        body["intermediates"] = [
            _waypoint(coordinate)["waypoint"] for coordinate in coordinates[1:-1]
        ]
    modifiers = _route_modifiers(route_options)
    if any(modifiers.values()):
        body["routeModifiers"] = modifiers
    async with httpx.AsyncClient(
        timeout=settings.HTTP_REQUEST_TIMEOUT_SECONDS,
        trust_env=False,
        transport=transport,
    ) as client:
        try:
            response = await client.post(
                settings.GOOGLE_MAPS_DIRECTIONS_URL,
                headers={
                    "X-Goog-Api-Key": settings.GOOGLE_MAPS_API_KEY,
                    "X-Goog-FieldMask": "routes.distanceMeters,routes.duration,routes.polyline",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _provider_http_error(exc.response, "geometry") from None
        except httpx.RequestError:
            raise RuntimeError("Google Directions request failed.") from None
    try:
        encoded = response.json()["routes"][0]["polyline"]["encodedPolyline"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Google Directions returned no route geometry.") from exc
    coordinates_geojson = _decode_polyline(encoded)
    if len(coordinates_geojson) < 2:
        raise RuntimeError("Google Directions returned incomplete route geometry.")
    return {
        "type": "Feature",
        "properties": {"provider": "google"},
        "geometry": {"type": "LineString", "coordinates": coordinates_geojson},
    }
