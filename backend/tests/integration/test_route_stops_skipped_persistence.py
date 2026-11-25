"""PostgreSQL regression tests for persisting routes with multiple skipped stops."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from contracts.money import Currency
from contracts.routing import (
    OptimizedRoute,
    Point,
    RouteObjective,
    RouteStatus,
    ScheduledStopOutput,
    SolverStatus,
    StopType,
    TrafficBasis,
    TransportMode,
)
from functions.api.models import TripModel
from functions.orchestrator.execution.models import ExecutionModel
from functions.orchestrator.routing.repository import RouteRepository


async def _seed_trip_and_execution(session: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
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
    return owner_id, trip_id, exec_id


class TestSkippedStopsPersistence:
    """Reproduces and verifies persistence of multiple skipped stops on PostgreSQL."""

    @pytest.mark.asyncio
    async def test_save_and_readback_multiple_skipped_stops_with_minus_one_order(
        self, session_factory: Any
    ) -> None:
        now = datetime.now(UTC)
        origin_id = uuid.uuid4()
        dest_id = uuid.uuid4()
        skip_a_id = uuid.uuid4()
        skip_b_id = uuid.uuid4()
        skip_c_id = uuid.uuid4()

        async with session_factory() as session:
            owner_id, trip_id, exec_id = await _seed_trip_and_execution(session)
            repo = RouteRepository(session)

            route = OptimizedRoute(
                owner_id=owner_id,
                trip_id=trip_id,
                execution_id=exec_id,
                route_version=1,
                status=RouteStatus.PARTIAL,
                solver_status=SolverStatus.OPTIMAL,
                objective=RouteObjective.FASTEST,
                transport_mode=TransportMode.CAR_PETROL,
                departure_at=now,
                return_deadline=now + timedelta(hours=10),
                total_distance_m=15000,
                total_travel_seconds=1800,
                total_dwell_seconds=3600,
                total_wait_seconds=0,
                total_elapsed_seconds=5400,
                driving_distance_m=15000,
                walking_distance_m=0,
                fuel_amount=Decimal("120.00"),
                fuel_currency=Currency.INR,
                traffic_basis=TrafficBasis.HISTORICAL_SPEEDS,
                stops=[
                    ScheduledStopOutput(
                        id=origin_id,
                        stop_id=origin_id,
                        stop_order=0,
                        name="Bengaluru",
                        location=Point(latitude=12.9716, longitude=77.5946),
                        stop_type=StopType.ORIGIN,
                        is_must_visit=True,
                        is_skipped=False,
                        dwell_seconds=0,
                        planned_arrival_at=now,
                        planned_departure_at=now,
                    ),
                    ScheduledStopOutput(
                        id=dest_id,
                        stop_id=dest_id,
                        stop_order=1,
                        name="Mysuru Palace",
                        location=Point(latitude=12.3052, longitude=76.6552),
                        stop_type=StopType.DESTINATION,
                        is_must_visit=True,
                        is_skipped=False,
                        dwell_seconds=3600,
                        planned_arrival_at=now + timedelta(seconds=1800),
                        planned_departure_at=now + timedelta(seconds=5400),
                    ),
                ],
                dropped_stops=[
                    ScheduledStopOutput(
                        id=skip_a_id,
                        stop_id=skip_a_id,
                        stop_order=-1,
                        name="Skipped Museum A",
                        location=Point(latitude=12.3100, longitude=76.6600),
                        stop_type=StopType.DESTINATION,
                        is_must_visit=False,
                        is_skipped=True,
                        skip_reason="TIME_WINDOW_EXCEEDED",
                        dwell_seconds=3600,
                    ),
                    ScheduledStopOutput(
                        id=skip_b_id,
                        stop_id=skip_b_id,
                        stop_order=-1,
                        name="Skipped Gallery B",
                        location=Point(latitude=12.3120, longitude=76.6620),
                        stop_type=StopType.DESTINATION,
                        is_must_visit=False,
                        is_skipped=True,
                        skip_reason="UNREACHABLE_TRANSITION",
                        dwell_seconds=3600,
                    ),
                    ScheduledStopOutput(
                        id=skip_c_id,
                        stop_id=skip_c_id,
                        stop_order=-1,
                        name="Skipped Garden C",
                        location=Point(latitude=12.3140, longitude=76.6640),
                        stop_type=StopType.DESTINATION,
                        is_must_visit=False,
                        is_skipped=True,
                        skip_reason="TIME_WINDOW_EXCEEDED",
                        dwell_seconds=3600,
                    ),
                ],
                legs=[],
            )

            persisted = await repo.save_route(route, owner_id=owner_id)
            await session.commit()
            route_id = persisted.id

        async with session_factory() as read_session:
            read_repo = RouteRepository(read_session)
            loaded = await read_repo.get_route_by_id(route_id, owner_id=owner_id)
            assert loaded is not None
            out = loaded.to_optimized_route()

            assert [s.name for s in out.stops] == ["Bengaluru", "Mysuru Palace"]
            assert [s.stop_order for s in out.stops] == [0, 1]
            assert [s.name for s in out.dropped_stops] == [
                "Skipped Museum A",
                "Skipped Gallery B",
                "Skipped Garden C",
            ]
            assert [s.skip_reason for s in out.dropped_stops] == [
                "TIME_WINDOW_EXCEEDED",
                "UNREACHABLE_TRANSITION",
                "TIME_WINDOW_EXCEEDED",
            ]
            assert all(s.is_skipped for s in out.dropped_stops)
            assert len({s.stop_order for s in loaded.stops}) == 5
