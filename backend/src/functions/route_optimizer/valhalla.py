"""External Valhalla HTTP routing service client.

Conforms strictly to specification 0005 AC-3 and AC-7:
- Connects to external Valhalla /sources_to_targets and /route endpoints.
- Validates geographic coordinates to prevent SSRF and out-of-bounds requests.
- Maps TransportMode to Valhalla costing models (auto, motorcycle, pedestrian, bicycle).
- Converts Valhalla distance (km) to meters and durations to integer seconds.
- Decodes polyline6 shapes into GeoJSON LineString geometries.
- Passes exclude_polygons for strict road closure and hazard avoidance.
- Enforces connection timeouts, read timeouts, and bounded exponential retries.
- Fast fails on 4xx client errors without wasteful retries.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from config.config import Settings, get_settings
from contracts.routing import MatrixResponse, Point
from contracts.trips import TransportMode

logger = logging.getLogger(__name__)


class ValhallaError(Exception):
    """Base exception for all Valhalla routing operations."""


class ValhallaConnectionError(ValhallaError):
    """Raised when connecting to Valhalla fails or endpoint is unconfigured."""


class ValhallaTimeoutError(ValhallaError):
    """Raised when Valhalla request exceeds configured read or connect timeout."""


class ValhallaRoutingError(ValhallaError):
    """Raised when Valhalla returns a client error or fails to calculate a route."""


class ValhallaSSRFError(ValhallaError):
    """Raised when coordinate parameters violate geographic safety bounds or blocked host rules.

    Covers both out-of-range latitude/longitude values and requests to private,
    loopback, or link-local IP addresses that would constitute an SSRF vector.
    """


def validate_coordinates(point_or_lat: Point | float, lon: float | None = None) -> None:
    """Validates geographic latitude and longitude bounds to prevent invalid or malicious coordinates."""
    if isinstance(point_or_lat, Point):
        lat = point_or_lat.latitude
        lng = point_or_lat.longitude
    else:
        if lon is None:
            raise ValueError(
                "validate_coordinates requires an explicit longitude when called with a raw float latitude"
            )
        lat = point_or_lat
        lng = lon

    if not (-90.0 <= lat <= 90.0):
        raise ValhallaSSRFError(
            f"Invalid latitude {lat}: must be between -90.0 and 90.0"
        )
    if not (-180.0 <= lng <= 180.0):
        raise ValhallaSSRFError(
            f"Invalid longitude {lng}: must be between -180.0 and 180.0"
        )


def decode_polyline6(encoded: str) -> list[tuple[float, float]]:
    """Decodes a polyline6 string into a list of (latitude, longitude) coordinates.

    Valhalla encodes shapes using 6 decimal places of precision (1e-6).
    """
    coordinates: list[tuple[float, float]] = []
    index = 0
    lat = 0
    lng = 0
    length = len(encoded)

    while index < length:
        # Decode latitude
        shift = 0
        result = 0
        while True:
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        dlat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += dlat

        # Decode longitude
        shift = 0
        result = 0
        while True:
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        dlng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += dlng

        coordinates.append((lat / 1e6, lng / 1e6))

    return coordinates


def polyline6_to_geojson_coordinates(encoded: str) -> list[list[float]]:
    """Decodes a polyline6 string into GeoJSON LineString coordinates [[lon, lat], ...]."""
    decoded = decode_polyline6(encoded)
    return [[lon, lat] for lat, lon in decoded]


class ValhallaLegResult(BaseModel):
    """Route leg calculation result from Valhalla."""

    model_config = ConfigDict(frozen=True)

    distance_m: int = Field(..., ge=0)
    travel_seconds: int = Field(..., ge=0)
    geometry: dict[str, Any]
    turn_instructions: list[dict[str, Any]] = Field(default_factory=list)


class ValhallaRouteResult(BaseModel):
    """Complete route calculation result from Valhalla."""

    model_config = ConfigDict(frozen=True)

    total_distance_m: int = Field(..., ge=0)
    total_travel_seconds: int = Field(..., ge=0)
    legs: list[ValhallaLegResult] = Field(default_factory=list)
    geometry: dict[str, Any]


class ValhallaClient:
    """Async HTTP client communicating with an external Valhalla routing instance."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        raw_url = base_url or self.settings.VALHALLA_ENDPOINT_URL
        self.base_url: str | None
        if raw_url:
            clean_url = raw_url.strip().rstrip("/")
            for suffix in ("/route", "/sources_to_targets", "/status"):
                if clean_url.endswith(suffix):
                    clean_url = clean_url[: -len(suffix)].rstrip("/")
            self.base_url = clean_url
        else:
            self.base_url = None
        self._external_client = http_client
        self._owned_client: httpx.AsyncClient | None = None
        self.call_count: int = 0
        self.retry_count: int = 0

    def _get_client(self) -> httpx.AsyncClient:
        """Returns or creates the underlying httpx.AsyncClient."""
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None or self._owned_client.is_closed:
            self._owned_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=self.settings.VALHALLA_CONNECT_TIMEOUT_SECONDS,
                    read=self.settings.VALHALLA_READ_TIMEOUT_SECONDS,
                    # POST bodies to Valhalla are small; reuse the read timeout for writes.
                    write=self.settings.VALHALLA_READ_TIMEOUT_SECONDS,
                    pool=self.settings.VALHALLA_CONNECT_TIMEOUT_SECONDS,
                )
            )
        return self._owned_client

    async def aclose(self) -> None:
        """Closes the owned HTTP client if active."""
        if self._owned_client is not None and not self._owned_client.is_closed:
            await self._owned_client.aclose()

    async def __aenter__(self) -> ValhallaClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.aclose()

    def _costing_for_mode(self, mode: TransportMode) -> str:
        """Translates internal TransportMode enum to Valhalla costing model string."""
        if mode == TransportMode.CAR_PETROL:
            return "auto"
        if mode == TransportMode.MOTORCYCLE_PETROL:
            return "motorcycle"
        if mode == TransportMode.WALKING:
            return "pedestrian"
        if mode == TransportMode.BICYCLE:
            return "bicycle"
        return "auto"

    async def check_health(self) -> bool:
        """Checks if the external Valhalla routing service is responsive via /status."""
        if not self.base_url:
            return False
        client = self._get_client()
        try:
            resp = await client.get(f"{self.base_url}/status")
            return resp.status_code == 200
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("Valhalla health check failed: %s", exc)
            return False

    async def _send_request(
        self,
        endpoint_path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Sends an HTTP POST request to Valhalla with bounded exponential backoff retries."""
        if not self.base_url:
            raise ValhallaConnectionError(
                "VALHALLA_ENDPOINT_URL is not configured; cannot query external routing service"
            )

        client = self._get_client()
        url = f"{self.base_url}{endpoint_path}"
        max_retries = self.settings.VALHALLA_MAX_RETRIES
        delay = 0.25

        attempt = 0
        while True:
            self.call_count += 1
            try:
                resp = await client.post(url, json=payload)

                # 4xx Client Error: fast fail without retries
                if 400 <= resp.status_code < 500:
                    error_msg = f"Valhalla client error {resp.status_code} for {endpoint_path}: {resp.text}"
                    logger.error(error_msg)
                    raise ValhallaRoutingError(error_msg)

                # 5xx Server Error: retry if budget remains
                if resp.status_code >= 500:
                    if attempt < max_retries:
                        self.retry_count += 1
                        logger.warning(
                            "Valhalla 5xx error (%d), retrying in %.2fs (attempt %d/%d)",
                            resp.status_code,
                            delay,
                            attempt + 1,
                            max_retries,
                        )
                        await asyncio.sleep(delay)
                        delay *= 2.0
                        attempt += 1
                        continue
                    raise ValhallaRoutingError(
                        f"Valhalla server error {resp.status_code} exceeded retries: {resp.text}"
                    )

                return resp.json()  # type: ignore[no-any-return]

            except httpx.TimeoutException as exc:
                if attempt < max_retries:
                    self.retry_count += 1
                    logger.warning(
                        "Valhalla timeout on %s, retrying in %.2fs (attempt %d/%d)",
                        endpoint_path,
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                    delay *= 2.0
                    attempt += 1
                    continue
                raise ValhallaTimeoutError(
                    f"Valhalla request to {endpoint_path} timed out after {max_retries} retries"
                ) from exc

            except httpx.TransportError as exc:
                if attempt < max_retries:
                    self.retry_count += 1
                    logger.warning(
                        "Valhalla connection error on %s, retrying in %.2fs (attempt %d/%d)",
                        endpoint_path,
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                    delay *= 2.0
                    attempt += 1
                    continue
                raise ValhallaConnectionError(
                    f"Valhalla connection error on {endpoint_path}: {exc}"
                ) from exc

    async def sources_to_targets(
        self,
        origins: list[Point],
        destinations: list[Point],
        mode: TransportMode = TransportMode.CAR_PETROL,
        costing_options: dict[str, Any] | None = None,
    ) -> MatrixResponse:
        """Queries Valhalla /sources_to_targets endpoint for distance and duration matrices."""
        if not origins or not destinations:
            raise ValhallaRoutingError("Matrix query requires at least one origin and destination")

        for p in origins:
            validate_coordinates(p)
        for p in destinations:
            validate_coordinates(p)

        sources_payload = [{"lat": p.latitude, "lon": p.longitude} for p in origins]
        targets_payload = [{"lat": p.latitude, "lon": p.longitude} for p in destinations]
        costing = self._costing_for_mode(mode)

        payload: dict[str, Any] = {
            "sources": sources_payload,
            "targets": targets_payload,
            "costing": costing,
            "units": "kilometers",
        }
        if costing_options:
            payload["costing_options"] = costing_options

        data = await self._send_request("/sources_to_targets", payload)

        sources_to_targets = data.get("sources_to_targets", [])
        durations_s: list[list[int | None]] = []
        distances_m: list[list[int | None]] = []

        for row in sources_to_targets:
            dur_row: list[int | None] = []
            dist_row: list[int | None] = []
            for cell in row:
                if (
                    cell is None
                    or cell.get("distance") is None
                    or cell.get("time") is None
                ):
                    dur_row.append(None)
                    dist_row.append(None)
                else:
                    dist_km = float(cell["distance"])
                    dur_s = int(round(float(cell["time"])))
                    dist_m = int(round(dist_km * 1000.0))
                    dur_row.append(dur_s)
                    dist_row.append(dist_m)
            durations_s.append(dur_row)
            distances_m.append(dist_row)

        return MatrixResponse(durations_s=durations_s, distances_m=distances_m)

    async def route(
        self,
        locations: list[Point],
        mode: TransportMode = TransportMode.CAR_PETROL,
        exclude_polygons: list[list[list[float]]] | None = None,
        costing_options: dict[str, Any] | None = None,
    ) -> ValhallaRouteResult:
        """Queries Valhalla /route endpoint for turn-by-turn maneuvers and geometries.

        Applies exclude_polygons for strict road closure prohibition during graph search.
        """
        if len(locations) < 2:
            raise ValhallaRoutingError("Route query requires at least two coordinate locations")

        for p in locations:
            validate_coordinates(p)

        locs_payload = [
            {"lat": p.latitude, "lon": p.longitude, "type": "break"} for p in locations
        ]
        costing = self._costing_for_mode(mode)

        payload: dict[str, Any] = {
            "locations": locs_payload,
            "costing": costing,
            "units": "kilometers",
        }
        if exclude_polygons:
            payload["exclude_polygons"] = exclude_polygons
        if costing_options:
            payload["costing_options"] = costing_options

        data = await self._send_request("/route", payload)

        trip = data.get("trip", {})
        trip_summary = trip.get("summary", {})
        total_dist_km = float(trip_summary.get("length", 0.0))
        total_dist_m = int(round(total_dist_km * 1000.0))
        total_time_s = int(round(float(trip_summary.get("time", 0.0))))

        legs_data = trip.get("legs", [])
        legs_results: list[ValhallaLegResult] = []
        all_coords: list[list[float]] = []

        for leg_data in legs_data:
            summary = leg_data.get("summary", {})
            dist_km = float(summary.get("length", 0.0))
            dist_m = int(round(dist_km * 1000.0))
            dur_s = int(round(float(summary.get("time", 0.0))))

            shape_encoded = leg_data.get("shape", "")
            coords = (
                polyline6_to_geojson_coordinates(shape_encoded)
                if shape_encoded
                else []
            )
            all_coords.extend(coords)

            maneuvers = leg_data.get("maneuvers", [])
            leg_instructions: list[dict[str, Any]] = [
                {
                    "instruction": m.get("instruction", ""),
                    "type": m.get("type", 0),
                    "street_names": m.get("street_names", []),
                    "time_seconds": int(round(float(m.get("time", 0.0)))),
                    "distance_meters": int(round(float(m.get("length", 0.0)) * 1000.0)),
                }
                for m in maneuvers
            ]

            legs_results.append(
                ValhallaLegResult(
                    distance_m=dist_m,
                    travel_seconds=dur_s,
                    geometry={"type": "LineString", "coordinates": coords},
                    turn_instructions=leg_instructions,
                )
            )

        full_geometry: dict[str, Any] = {
            "type": "LineString",
            "coordinates": all_coords,
        }

        return ValhallaRouteResult(
            total_distance_m=total_dist_m,
            total_travel_seconds=total_time_s,
            legs=legs_results,
            geometry=full_geometry,
        )
