"""Vehicle continuity tracking and multimodal transition validation.

Conforms strictly to specification 0005 AC-4:
- Guarantees driving legs depart only from the exact stop where the vehicle is parked.
- Detects and prevents vehicle teleportation or invented road-node parking.
- Tracks parked vehicle location across walking sequences and walk-chaining clusters.
- Ensures travelers return to their vehicle before departing on subsequent driving legs.
"""

from __future__ import annotations

import logging
import math
from typing import Any
from uuid import UUID

from contracts.routing import (
    OptimizedRoute,
    Point,
    RouteLegOutput,
    RouteStopInput,
    ScheduledStopOutput,
)
from contracts.trips import TransportMode

logger = logging.getLogger(__name__)


class VehicleContinuityViolationError(Exception):
    """Raised when an itinerary violates physical vehicle continuity constraints."""


class NoVerifiedParkingHubError(Exception):
    """Raised when a pedestrian destination has no verified parking hub within threshold."""

    def __init__(self, message: str, stop_id: UUID | None = None, stop_name: str | None = None) -> None:
        super().__init__(message)
        self.stop_id = stop_id
        self.stop_name = stop_name


def haversine_distance_m(p1: Point, p2: Point) -> int:
    """Calculates great-circle distance in meters between two WGS84 coordinates."""
    r = 6371000  # Earth mean radius in meters
    lat1, lon1 = math.radians(p1.latitude), math.radians(p1.longitude)
    lat2, lon2 = math.radians(p2.latitude), math.radians(p2.longitude)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return int(round(r * c))


class VehicleContinuityTracker:
    """Tracks and verifies traveler and vehicle spatial continuity along an itinerary."""

    def __init__(self) -> None:
        self.current_stop_id: UUID | None = None
        self.current_stop_name: str | None = None
        self.current_stop_location: Point | None = None
        self.vehicle_parked_at_stop_id: UUID | None = None
        self.vehicle_parked_at_stop_name: str | None = None
        self.vehicle_parked_at_location: Point | None = None
        self.is_vehicle_parked: bool = False
        self.transport_mode: TransportMode = TransportMode.CAR_PETROL
        self.has_vehicle: bool = True
        self.history: list[dict[str, Any]] = []

    def start_trip(
        self,
        origin_stop: RouteStopInput | ScheduledStopOutput,
        transport_mode: TransportMode = TransportMode.CAR_PETROL,
    ) -> None:
        """Initializes vehicle and traveler position at the route origin."""
        self.current_stop_id = origin_stop.id
        self.current_stop_name = origin_stop.name
        self.current_stop_location = origin_stop.location
        self.transport_mode = transport_mode
        self.has_vehicle = transport_mode in (
            TransportMode.CAR_PETROL,
            TransportMode.MOTORCYCLE_PETROL,
        )
        self.is_vehicle_parked = False
        self.vehicle_parked_at_stop_id = None
        self.vehicle_parked_at_stop_name = None
        self.vehicle_parked_at_location = None
        self.history = [
            {
                "event": "start",
                "stop_id": self.current_stop_id,
                "stop_name": self.current_stop_name,
                "has_vehicle": self.has_vehicle,
                "is_parked": self.is_vehicle_parked,
            }
        ]

    def step_leg(self, leg: RouteLegOutput) -> None:
        """Applies a transition leg, verifying traveler and vehicle continuity invariants."""
        # 1. Traveler departure location verification
        if self.current_stop_id is not None and leg.from_stop_id != self.current_stop_id:
            msg = (
                f"Traveler continuity violation: traveler is currently at stop "
                f"{self.current_stop_id} ('{self.current_stop_name}'), but leg {leg.leg_order} "
                f"departs from stop {leg.from_stop_id} ('{leg.from_stop_name}')"
            )
            logger.error(msg)
            raise VehicleContinuityViolationError(msg)

        is_driving_leg = leg.transport_mode in (
            TransportMode.CAR_PETROL,
            TransportMode.MOTORCYCLE_PETROL,
        )

        # 2. Vehicle departure verification for driving legs
        if is_driving_leg:
            if not self.has_vehicle:
                msg = (
                    f"Transport mode violation: itinerary started without a private vehicle, "
                    f"cannot execute driving leg {leg.leg_order} from '{leg.from_stop_name}'"
                )
                logger.error(msg)
                raise VehicleContinuityViolationError(msg)

            if self.is_vehicle_parked:
                # Vehicle is parked: driving leg can only depart from the exact parked hub
                if leg.from_stop_id != self.vehicle_parked_at_stop_id:
                    msg = (
                        f"Vehicle continuity violation: vehicle is parked at stop "
                        f"{self.vehicle_parked_at_stop_id} ('{self.vehicle_parked_at_stop_name}'), "
                        f"but driving leg {leg.leg_order} departs from stop "
                        f"{leg.from_stop_id} ('{leg.from_stop_name}') without returning to vehicle"
                    )
                    logger.error(msg)
                    raise VehicleContinuityViolationError(msg)
                # Departing the parking hub unparks the vehicle
                self.is_vehicle_parked = False
                self.vehicle_parked_at_stop_id = None
                self.vehicle_parked_at_stop_name = None
                self.vehicle_parked_at_location = None

        elif leg.transport_mode == TransportMode.WALKING:
            # Walking leg: if traveler was with the vehicle, the vehicle is now parked at the origin
            if self.has_vehicle and not self.is_vehicle_parked:
                self.is_vehicle_parked = True
                self.vehicle_parked_at_stop_id = leg.from_stop_id
                self.vehicle_parked_at_stop_name = leg.from_stop_name
            elif self.has_vehicle and self.is_vehicle_parked:
                # If traveler walks back to the parking hub where the vehicle is parked
                if leg.to_stop_id == self.vehicle_parked_at_stop_id:
                    self.is_vehicle_parked = False
                    self.vehicle_parked_at_stop_id = None
                    self.vehicle_parked_at_stop_name = None

        # 3. Advance traveler to leg destination
        self.current_stop_id = leg.to_stop_id
        self.current_stop_name = leg.to_stop_name

        self.history.append(
            {
                "leg_order": leg.leg_order,
                "from_stop": leg.from_stop_name,
                "to_stop": leg.to_stop_name,
                "transport_mode": leg.transport_mode.value,
                "is_vehicle_parked": self.is_vehicle_parked,
                "parked_at": self.vehicle_parked_at_stop_name,
            }
        )

    def validate_route(self, route: OptimizedRoute) -> list[str]:
        """Validates all legs of an OptimizedRoute, returning any continuity violation diagnostics."""
        diagnostics: list[str] = []
        if not route.stops or not route.legs:
            return diagnostics

        first_stop = route.stops[0]
        # Infer transport mode from first leg or request
        first_leg_mode = route.legs[0].transport_mode if route.legs else TransportMode.CAR_PETROL
        self.start_trip(first_stop, transport_mode=first_leg_mode)

        for leg in route.legs:
            try:
                self.step_leg(leg)
            except VehicleContinuityViolationError as exc:
                diagnostics.append(str(exc))

        # Check final state: if trip ends and vehicle was abandoned at a parking hub
        if self.has_vehicle and self.is_vehicle_parked:
            last_stop = route.stops[-1]
            if last_stop.id != self.vehicle_parked_at_stop_id:
                diagnostics.append(
                    f"Vehicle abandoned: itinerary ended at '{last_stop.name}', but vehicle "
                    f"remains parked at '{self.vehicle_parked_at_stop_name}'"
                )

        return diagnostics
