"""Idempotent test fixture seeder for browser acceptance testing.

Conforms strictly to specification 0006:
- Dynamic route generation through actual platform API -> graph -> worker -> reconciliation pipeline.
- Seeds input places (canonical_places with verified parking), road closure claims, and trips.
- Strict data isolation: cleanup restricted solely to owned fixtures (no database-wide outbox or leg updates).
"""

from __future__ import annotations

import logging
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import shapely.geometry
from geoalchemy2.shape import from_shape
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Resilient DNS caching for PostgreSQL connectivity
_orig_getaddrinfo = socket.getaddrinfo
_dns_cache: dict[tuple[Any, ...], Any] = {
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", 20266): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("165.232.185.214", 20266),
        )
    ],
    ("pg-070905-cloud-postgress-db.d.aivencloud.com", "20266"): [
        (
            socket.AddressFamily.AF_INET,
            socket.SocketKind.SOCK_STREAM,
            6,
            "",
            ("165.232.185.214", 20266),
        )
    ],
}


def _resilient_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    key = (host, port)
    if key in _dns_cache:
        return _dns_cache[key]
    try:
        res = _orig_getaddrinfo(host, port, *args, **kwargs)
        _dns_cache[key] = res
        return res
    except socket.gaierror:
        for cached_key, cached_val in _dns_cache.items():
            if cached_key[0] == host:
                return cached_val
        raise


socket.getaddrinfo = _resilient_getaddrinfo

from acceptance_database import ensure_acceptance_schema, validate_acceptance_database_url
from acceptance_worker import LocalAcceptanceWorker
from contracts.routing import Point, RouteRequest, RouteStopInput, StopType
from contracts.trips import TransportMode
from functions.api.models import ApiIdempotencyModel, TripModel
from functions.api.routes.optimization import submit_route_optimization
from functions.orchestrator.evidence.models import ClaimModel
from functions.orchestrator.execution.models import (
    ExecutionModel,
    JobEventModel,
    OutboxEventModel,
    TaskModel,
    WorkerResultModel,
)
from functions.orchestrator.places.models import CanonicalPlaceModel
from functions.orchestrator.routing.models import (
    RouteLegModel,
    RouteModel,
    RouteRestrictionModel,
    RouteStopModel,
)
from middleware.auth import AuthenticatedUser

logger = logging.getLogger("acceptance_seeder")

USER_A_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_B_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")

DEMO_TRIP_A_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEMO_TRIP_MM_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
DEMO_TRIP_PARTIAL_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")
DEMO_TRIP_INFEASIBLE_ID = uuid.UUID("60000000-0000-0000-0000-000000000001")
DEMO_TRIP_B_ID = uuid.UUID("99999999-0000-0000-0000-000000000001")

MULTIMODAL_ROUTE_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
PARTIAL_ROUTE_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
INFEASIBLE_ROUTE_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
USER_B_ROUTE_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")

PARKING_HUB_ID = uuid.UUID("40000000-0000-0000-0000-000000000002")
ROAD_CLOSURE_CLAIM_ID = uuid.UUID("40000000-0000-0000-0000-000000000003")

OWNED_TRIP_IDS = [
    DEMO_TRIP_A_ID,
    DEMO_TRIP_MM_ID,
    DEMO_TRIP_PARTIAL_ID,
    DEMO_TRIP_INFEASIBLE_ID,
    DEMO_TRIP_B_ID,
]
CANONICAL_ROUTE_IDS = [
    MULTIMODAL_ROUTE_ID,
    PARTIAL_ROUTE_ID,
    INFEASIBLE_ROUTE_ID,
    USER_B_ROUTE_ID,
]
OWNED_USER_IDS = [USER_A_ID, USER_B_ID]


async def seed_acceptance_fixtures(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Idempotently cleans owned test entities, seeds inputs, and generates routes through the pipeline."""
    from config.config import settings

    if getattr(settings, "ENVIRONMENT", "local") == "production":
        raise RuntimeError("Acceptance fixture seeding is strictly forbidden in production")

    validate_acceptance_database_url(
        settings.ACCEPTANCE_DATABASE_URL,
        settings.DATABASE_URL,
    )
    await ensure_acceptance_schema(session_factory)

    now = datetime.now(UTC)

    # 1. Surgical cleanup strictly restricted to owned test entities
    async with session_factory() as session:
        # Find all routes associated with owned trips, canonical IDs, or child replans
        r_stmt = select(RouteModel.id).where(
            RouteModel.owner_id.in_(OWNED_USER_IDS),
            (
                (RouteModel.trip_id.in_(OWNED_TRIP_IDS))
                | (RouteModel.id.in_(CANONICAL_ROUTE_IDS))
                | (RouteModel.parent_route_id.in_(CANONICAL_ROUTE_IDS))
            ),
        )
        target_rids = list((await session.execute(r_stmt)).scalars().all())

        # Collect route execution IDs BEFORE deleting route records
        route_exec_ids = []
        if target_rids:
            route_exec_stmt = select(RouteModel.execution_id).where(
                RouteModel.id.in_(target_rids),
                RouteModel.execution_id.is_not(None),
            )
            route_exec_ids = list((await session.execute(route_exec_stmt)).scalars().all())

            await session.execute(delete(RouteLegModel).where(RouteLegModel.route_id.in_(target_rids)))
            await session.execute(delete(RouteStopModel).where(RouteStopModel.route_id.in_(target_rids)))
            await session.execute(
                delete(RouteRestrictionModel).where(RouteRestrictionModel.route_id.in_(target_rids))
            )
            await session.execute(delete(RouteModel).where(RouteModel.id.in_(target_rids)))

        e_stmt = select(ExecutionModel.id).where(
            ExecutionModel.owner_id.in_(OWNED_USER_IDS),
            (
                ExecutionModel.trip_id.in_(OWNED_TRIP_IDS)
                | ExecutionModel.id.in_(route_exec_ids)
            ),
        )
        target_eids = list((await session.execute(e_stmt)).scalars().all())

        if target_eids:
            await session.execute(
                delete(JobEventModel).where(JobEventModel.execution_id.in_(target_eids))
            )
            await session.execute(
                delete(OutboxEventModel).where(OutboxEventModel.execution_id.in_(target_eids))
            )
            await session.execute(delete(TaskModel).where(TaskModel.execution_id.in_(target_eids)))
            await session.execute(
                delete(WorkerResultModel).where(WorkerResultModel.execution_id.in_(target_eids))
            )
            await session.execute(delete(ExecutionModel).where(ExecutionModel.id.in_(target_eids)))

        # Clean only demonstrably fixture-owned idempotency records
        fixture_idemp_keys = [
            "seed-multimodal-route",
            "seed-partial-route",
            "seed-infeasible-route",
            "seed-user-b-route",
        ]
        canonical_route_key_strs = [str(r) for r in CANONICAL_ROUTE_IDS]
        await session.execute(
            delete(ApiIdempotencyModel).where(
                ApiIdempotencyModel.owner_id.in_(OWNED_USER_IDS),
                (
                    ApiIdempotencyModel.key.in_(fixture_idemp_keys)
                    | (
                        (ApiIdempotencyModel.operation == "route_optimization:replan_reservation")
                        & (ApiIdempotencyModel.key.in_(canonical_route_key_strs))
                    )
                ),
            )
        )

        # Clean owned places and claims
        await session.execute(
            delete(CanonicalPlaceModel).where(CanonicalPlaceModel.id == PARKING_HUB_ID)
        )
        await session.execute(
            delete(ClaimModel).where(ClaimModel.id == ROAD_CLOSURE_CLAIM_ID)
        )

        # 2. Seed Trip records for each test scenario
        trips_to_seed = [
            (DEMO_TRIP_A_ID, USER_A_ID),
            (DEMO_TRIP_MM_ID, USER_A_ID),
            (DEMO_TRIP_PARTIAL_ID, USER_A_ID),
            (DEMO_TRIP_INFEASIBLE_ID, USER_A_ID),
            (DEMO_TRIP_B_ID, USER_B_ID),
        ]
        for t_id, o_id in trips_to_seed:
            trip_rec = await session.get(TripModel, t_id)
            if trip_rec is None:
                trip_rec = TripModel(
                    id=t_id,
                    owner_id=o_id,
                    current_version=1,
                    state="draft",
                    created_at=now,
                    updated_at=now,
                )
                session.add(trip_rec)

        # 3. Seed Canonical Place input (Verified Parking Hub in precinct)
        parking_point = from_shape(shapely.geometry.Point(77.5855, 12.9794), srid=4326)
        parking_hub = CanonicalPlaceModel(
            id=PARKING_HUB_ID,
            primary_name="Cubbon Park Verified Parking Hub",
            locality="Bengaluru",
            point=parking_point,
            provider_ids={"category": "parking", "amenity": "parking"},
            is_candidate=False,
            access_status="VERIFIED",
            names=["Cubbon Park Verified Parking Hub", "Cubbon Park Hub"],
            created_at=now,
            updated_at=now,
        )
        session.add(parking_hub)

        # 4. Seed Verified Road Closure Claim input (between Depot and Parking Hub)
        closure_polygon = {
            "type": "Polygon",
            "coordinates": [
                [
                    [77.5950, 12.9740],
                    [77.6010, 12.9740],
                    [77.6010, 12.9770],
                    [77.5950, 12.9770],
                    [77.5950, 12.9740],
                ]
            ],
        }
        closure_claim = ClaimModel(
            id=ROAD_CLOSURE_CLAIM_ID,
            subject_id=f"place:{PARKING_HUB_ID}",
            place_id=PARKING_HUB_ID,
            category="ROAD_CLOSURE",
            verification="VERIFIED",
            availability="ACTIVE",
            freshness="VERIFIED",
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=7),
            geographic_scope=closure_polygon,
            typed_value={
                "closure_type": "EMERGENCY_BRIDGE_REPAIR",
                "geometry": closure_polygon,
            },
            policy_version="1.0",
        )
        session.add(closure_claim)

        await session.commit()
        logger.info("Cleaned owned fixtures and seeded input places and road closure claims")

    # 5. Generate route outcomes via actual API -> graph -> worker -> reconciliation pipeline
    worker = LocalAcceptanceWorker(session_factory, allowed_owner_ids=tuple(OWNED_USER_IDS))
    user_a = AuthenticatedUser(id=USER_A_ID, email="traveler@swyra.internal")
    user_b = AuthenticatedUser(id=USER_B_ID, email="attacker@unauthorized.internal")

    # A. Multimodal Route (Parking Hub + Walking Legs + Vehicle Return + Depot Return = 3.3 km)
    mm_req = RouteRequest(
        owner_id=USER_A_ID,
        trip_id=DEMO_TRIP_MM_ID,
        route_id=MULTIMODAL_ROUTE_ID,
        transport_mode=TransportMode.CAR_PETROL,
        is_round_trip=True,
        departure_at=now,
        stops=[
            RouteStopInput(
                name="Bengaluru MG Road Depot",
                location=Point(latitude=12.9716, longitude=77.5946),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
                dwell_seconds=0,
            ),
            RouteStopInput(
                name="Botanical Glass House Sanctuary",
                location=Point(latitude=12.9822, longitude=77.5843),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
                is_pedestrian_only=True,
                dwell_seconds=1800,
            ),
        ],
    )
    await _execute_route_pipeline(
        session_factory=session_factory,
        request=mm_req,
        current_user=user_a,
        idempotency_key="seed-multimodal-route",
        worker=worker,
    )
    logger.info("Generated multimodal route %s via live execution pipeline", MULTIMODAL_ROUTE_ID)

    # B. PARTIAL Outcome Route (Dropped Optional Stop due to window breach)
    partial_req = RouteRequest(
        owner_id=USER_A_ID,
        trip_id=DEMO_TRIP_PARTIAL_ID,
        route_id=PARTIAL_ROUTE_ID,
        transport_mode=TransportMode.CAR_PETROL,
        is_round_trip=False,
        departure_at=now,
        stops=[
            RouteStopInput(
                name="Bengaluru MG Road Origin",
                location=Point(latitude=12.9716, longitude=77.5946),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="Mysuru Palace Heritage",
                location=Point(latitude=12.3051, longitude=76.6551),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
                dwell_seconds=3600,
            ),
            RouteStopInput(
                name="Tiger Hill Summit",
                location=Point(latitude=12.4244, longitude=75.7382),
                stop_type=StopType.DESTINATION,
                is_must_visit=False,
                window_start=now + timedelta(seconds=60),
                window_end=now + timedelta(seconds=120),
                dwell_seconds=1800,
            ),
        ],
    )
    await _execute_route_pipeline(
        session_factory=session_factory,
        request=partial_req,
        current_user=user_a,
        idempotency_key="seed-partial-route",
        worker=worker,
    )
    logger.info("Generated PARTIAL route %s via live execution pipeline", PARTIAL_ROUTE_ID)

    # C. INFEASIBLE Hard Constraint Outcome Route (Pedestrian precinct with zero parking places)
    infeasible_req = RouteRequest(
        owner_id=USER_A_ID,
        trip_id=DEMO_TRIP_INFEASIBLE_ID,
        route_id=INFEASIBLE_ROUTE_ID,
        transport_mode=TransportMode.CAR_PETROL,
        is_round_trip=False,
        departure_at=now,
        stops=[
            RouteStopInput(
                name="Forest Base Camp",
                location=Point(latitude=22.0000, longitude=88.8500),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="Mangrove Boardwalk Sanctuary",
                location=Point(latitude=21.9497, longitude=88.9000),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
                is_pedestrian_only=True,
            ),
        ],
    )
    await _execute_route_pipeline(
        session_factory=session_factory,
        request=infeasible_req,
        current_user=user_a,
        idempotency_key="seed-infeasible-route",
        worker=worker,
    )
    logger.info("Generated INFEASIBLE route %s via live execution pipeline", INFEASIBLE_ROUTE_ID)

    # D. User B Foreign Route (For Tenant Isolation verification)
    user_b_req = RouteRequest(
        owner_id=USER_B_ID,
        trip_id=DEMO_TRIP_B_ID,
        route_id=USER_B_ROUTE_ID,
        transport_mode=TransportMode.CAR_PETROL,
        is_round_trip=False,
        departure_at=now,
        stops=[
            RouteStopInput(
                name="User B Private Depot",
                location=Point(latitude=12.9716, longitude=77.5946),
                stop_type=StopType.ORIGIN,
                is_must_visit=True,
            ),
            RouteStopInput(
                name="User B Private Warehouse",
                location=Point(latitude=12.9800, longitude=77.6000),
                stop_type=StopType.DESTINATION,
                is_must_visit=True,
            ),
        ],
    )
    await _execute_route_pipeline(
        session_factory=session_factory,
        request=user_b_req,
        current_user=user_b,
        idempotency_key="seed-user-b-route",
        worker=worker,
    )
    logger.info("Generated User B route %s via live execution pipeline", USER_B_ROUTE_ID)


async def _execute_route_pipeline(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    request: RouteRequest,
    current_user: AuthenticatedUser,
    idempotency_key: str,
    worker: LocalAcceptanceWorker,
    max_steps: int = 10,
) -> None:
    """Submits request via API route handler and drives graph/worker/reconciler outbox pipeline."""
    async with session_factory() as session:
        await submit_route_optimization(
            request=request,
            session=session,
            current_user=current_user,
            idempotency_key=idempotency_key,
        )

    for _ in range(max_steps):
        processed = await worker.process_pending_events_once()
        if processed == 0:
            break
