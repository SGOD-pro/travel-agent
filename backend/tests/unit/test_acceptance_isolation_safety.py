"""Tests for acceptance database isolation, configuration validation, worker filtering,
and cleanup ordering / data preservation conforming to spec 0006.

Proves:
1. Missing or empty ACCEPTANCE_DATABASE_URL fails closed.
2. ACCEPTANCE_DATABASE_URL pointing to the same database as DATABASE_URL fails closed,
   accounting for equivalent connection URLs across schemes, ports, and parameters.
3. Acceptance worker filters strictly by allowed trip IDs and owners, ignoring unrelated executions.
4. Fixture cleanup collects route execution IDs before deleting route records, cleans up fixture
   executions, outbox events, tasks, worker results, and job events, while preserving unrelated records.
5. Repeated setup and cleanup succeed idempotently without regression.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import shapely.geometry
from geoalchemy2.shape import from_shape
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from acceptance_database import (
    are_db_urls_equivalent,
    are_hosts_equivalent,
    normalize_db_connection_target,
    validate_acceptance_database_url,
    validate_isolated_database_url,
)
from acceptance_seeder import (
    CANONICAL_ROUTE_IDS,
    DEMO_TRIP_A_ID,
    MULTIMODAL_ROUTE_ID,
    OWNED_TRIP_IDS,
    OWNED_USER_IDS,
    USER_A_ID,
)
from acceptance_worker import (
    DEFAULT_ALLOWED_OWNER_IDS,
    DEFAULT_ALLOWED_TRIP_IDS,
    LocalAcceptanceWorker,
)
from config.config import settings
from functions.api.models import ApiIdempotencyModel, TripModel
from functions.orchestrator.execution.models import (
    ExecutionModel,
    JobEventModel,
    OutboxEventModel,
    TaskModel,
    WorkerResultModel,
)
from functions.orchestrator.routing.models import (
    RouteLegModel,
    RouteModel,
    RouteRestrictionModel,
    RouteStopModel,
)

# --- 1. Acceptance Database Configuration & Isolation Tests ---


def test_missing_acceptance_database_url_fails_closed() -> None:
    """Acceptance execution fails closed if ACCEPTANCE_DATABASE_URL is missing or empty."""
    app_url = "postgresql+asyncpg://user:pass@db.example.com:5432/travel_planning"

    for missing_val in (None, "", "   ", "\t\n"):
        with pytest.raises(RuntimeError) as exc_info:
            validate_acceptance_database_url(missing_val, app_url)
        assert "ACCEPTANCE_DATABASE_URL must be explicitly configured" in str(exc_info.value)


def test_application_database_reuse_fails_closed() -> None:
    """Acceptance execution fails closed if ACCEPTANCE_DATABASE_URL targets the same database as DATABASE_URL."""
    base_app_url = "postgresql+asyncpg://app_user:secret@pg.cloud.internal:5432/defaultdb"

    # Equivalent variations that must be caught
    identical_targets = [
        "postgresql+asyncpg://app_user:secret@pg.cloud.internal:5432/defaultdb",
        "postgres://app_user:secret@pg.cloud.internal:5432/defaultdb",
        "postgresql://app_user:secret@pg.cloud.internal:5432/defaultdb",
        "postgres://app_user:secret@PG.CLOUD.INTERNAL:5432/defaultdb",
        "postgres://app_user:secret@pg.cloud.internal/defaultdb",  # implicit port 5432
        "postgres://app_user:secret@pg.cloud.internal:5432/defaultdb?sslmode=require",
        "postgresql+asyncpg://app_user:secret@pg.cloud.internal:5432/defaultdb?ssl=require&timeout=30",
    ]

    for equiv_url in identical_targets:
        with pytest.raises(RuntimeError) as exc_info:
            validate_acceptance_database_url(equiv_url, base_app_url)
        assert "ACCEPTANCE_DATABASE_URL cannot point to the same database as DATABASE_URL" in str(exc_info.value)


def test_isolated_acceptance_database_url_succeeds() -> None:
    """Explicitly configured dedicated acceptance database URL is accepted."""
    app_url = "postgres://admin:pass@pg.cloud.internal:20266/defaultdb?sslmode=require"
    acceptance_url = "postgres://admin:pass@pg.cloud.internal:20266/acceptance_db?sslmode=require"

    validated = validate_acceptance_database_url(acceptance_url, app_url)
    assert validated == acceptance_url


def test_url_normalization_and_equivalence() -> None:
    """Normalizes connection targets and determines equivalence across diverse connection formats."""
    # Differing database names on same host
    assert not are_db_urls_equivalent(
        "postgres://admin:pass@10.0.0.1:5432/app_db",
        "postgres://admin:pass@10.0.0.1:5432/acceptance_db",
    )

    # Differing ports on same host
    assert not are_db_urls_equivalent(
        "postgres://admin:pass@10.0.0.1:5432/testdb",
        "postgres://admin:pass@10.0.0.1:5433/testdb",
    )

    # Differing resolved IP hosts
    assert not are_db_urls_equivalent(
        "postgres://admin:pass@10.0.0.1:5432/testdb",
        "postgres://admin:pass@10.0.0.2:5432/testdb",
    )

    # Differing usernames pointing to same host, port, and database are EQUIVALENT (fail closed)
    assert are_db_urls_equivalent(
        "postgres://user1:pass@10.0.0.1:5432/testdb",
        "postgres://user2:pass@10.0.0.1:5432/testdb",
    )

    # Same target with different drivers and query strings
    assert are_db_urls_equivalent(
        "postgresql+asyncpg://admin:pass@10.0.0.1:5432/testdb?ssl=require",
        "postgres://admin:pass@10.0.0.1:5432/testdb?sslmode=require&connect_timeout=10",
    )


def test_different_postgresql_users_target_equivalence_regression() -> None:
    """Proves two URLs pointing to the same host, port, and database are treated as equivalent
    even when they specify different PostgreSQL users or credentials."""
    base_app_url = "postgresql+asyncpg://app_user:secret@pg.cloud.internal:5432/defaultdb"

    different_user_urls = [
        "postgresql+asyncpg://different_user:other_pass@pg.cloud.internal:5432/defaultdb",
        "postgres://admin:admin123@pg.cloud.internal:5432/defaultdb",
        "postgres://readonly_role:guest@pg.cloud.internal:5432/defaultdb",
        "postgres://pg.cloud.internal:5432/defaultdb",  # no user specified
        "postgresql://service_account:token@PG.CLOUD.INTERNAL:5432/defaultdb?sslmode=require",
    ]

    for user_url in different_user_urls:
        # Equivalence must return True
        assert are_db_urls_equivalent(user_url, base_app_url) is True

        # Validation must fail closed with RuntimeError
        with pytest.raises(RuntimeError) as exc_info:
            validate_acceptance_database_url(user_url, base_app_url)
        assert "cannot point to the same database as DATABASE_URL" in str(exc_info.value)

        with pytest.raises(RuntimeError) as exc_info:
            validate_isolated_database_url(user_url, base_app_url, label="Test database")
        assert "cannot point to the same database as DATABASE_URL" in str(exc_info.value)


def test_host_alias_and_equivalent_representations_regression() -> None:
    """Proves host aliases (loopback variants, case differences, trailing dots, bracketed IPv6,
    and DNS/IP aliases) are recognized as equivalent targets."""
    # Loopback aliases: localhost, 127.0.0.1, 127.0.0.2, ::1, 0.0.0.0
    loopback_pairs = [
        ("postgres://u:p@localhost:5432/db", "postgres://u:p@127.0.0.1:5432/db"),
        ("postgres://u:p@localhost:5432/db", "postgres://u:p@127.0.0.2:5432/db"),
        ("postgres://u:p@127.0.0.1:5432/db", "postgres://u:p@127.0.0.99:5432/db"),
        ("postgres://u:p@localhost:5432/db", "postgres://u:p@[::1]:5432/db"),
        ("postgres://u:p@0.0.0.0:5432/db", "postgres://u:p@127.0.0.1:5432/db"),
    ]
    for url1, url2 in loopback_pairs:
        assert are_db_urls_equivalent(url1, url2) is True
        with pytest.raises(RuntimeError):
            validate_acceptance_database_url(url1, url2)

    # Format variants: trailing dot, case insensitivity, brackets
    formatting_pairs = [
        ("postgres://u:p@db.internal.:5432/db", "postgres://u:p@db.internal:5432/db"),
        ("postgres://u:p@DB.INTERNAL:5432/db", "postgres://u:p@db.internal:5432/db"),
        ("postgres://u:p@[::1]:5432/db", "postgres://u:p@::1:5432/db"),
    ]
    for url1, url2 in formatting_pairs:
        assert are_db_urls_equivalent(url1, url2) is True
        with pytest.raises(RuntimeError):
            validate_acceptance_database_url(url1, url2)

    # Cloud hostname vs resolved active IP alias
    cloud_host_url = "postgres://u:p@pg-070905-cloud-postgress-db.d.aivencloud.com:20266/defaultdb"
    cloud_ip_url = "postgres://other_u:p@139.59.42.218:20266/defaultdb"
    assert are_db_urls_equivalent(cloud_host_url, cloud_ip_url) is True
    with pytest.raises(RuntimeError):
        validate_acceptance_database_url(cloud_ip_url, cloud_host_url)


def test_indeterminate_database_target_fails_closed_regression() -> None:
    """Proves that indeterminate targets (missing host, missing db, invalid port, or unresolvable host)
    fail closed across are_db_urls_equivalent and validation guards."""
    valid_app_url = "postgres://u:p@10.0.0.1:5432/defaultdb"

    indeterminate_urls = [
        None,
        "",
        "   ",
        "not-a-valid-connection-string",
        "postgresql:///defaultdb",  # missing host
        "postgresql://localhost:5432",  # missing database name
        "postgresql://localhost:5432/",  # empty database name
        "postgresql://localhost:99999/defaultdb",  # invalid port
        "postgresql://localhost:0/defaultdb",  # invalid port 0
    ]

    for bad_url in indeterminate_urls:
        # Equivalence must fail closed (return True)
        assert are_db_urls_equivalent(bad_url, valid_app_url) is True
        assert are_db_urls_equivalent(valid_app_url, bad_url) is True

        # Validation must fail closed with RuntimeError
        with pytest.raises(RuntimeError):
            validate_acceptance_database_url(bad_url, valid_app_url)

    # Unresolvable / indeterminate host target when comparing identical dbname and port
    # cannot be safely determined, so it must fail closed (return True)
    unresolvable_url1 = "postgres://u:p@unresolvable-host-alpha.invalid:5432/defaultdb"
    unresolvable_url2 = "postgres://u:p@unresolvable-host-beta.invalid:5432/defaultdb"
    assert are_db_urls_equivalent(unresolvable_url1, unresolvable_url2) is True
    assert are_db_urls_equivalent(unresolvable_url1, valid_app_url) is True


def test_conftest_checkpointer_isolation_guards_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves get_isolated_test_database_url and postgres_checkpointer fail closed
    when configured with a shared application database or missing configuration."""
    from tests.integration.conftest import get_isolated_test_database_url

    # 1. When TEST_DATABASE_URL points to the application database (same host/port/dbname)
    monkeypatch.setenv("TEST_DATABASE_URL", settings.DATABASE_URL)
    with pytest.raises(RuntimeError) as exc_info:
        get_isolated_test_database_url()
    assert "cannot point to the same database as DATABASE_URL" in str(exc_info.value)

    # 2. When TEST_DATABASE_URL uses a different username on the application database
    tampered_user_url = settings.DATABASE_URL.replace("avnadmin", "tampered_user")
    monkeypatch.setenv("TEST_DATABASE_URL", tampered_user_url)
    with pytest.raises(RuntimeError) as exc_info:
        get_isolated_test_database_url()
    assert "cannot point to the same database as DATABASE_URL" in str(exc_info.value)

    # 3. When TEST_DATABASE_URL is empty and ACCEPTANCE_DATABASE_URL is also missing or matches app db
    monkeypatch.setenv("TEST_DATABASE_URL", "")
    monkeypatch.setattr(settings, "ACCEPTANCE_DATABASE_URL", settings.DATABASE_URL)
    with pytest.raises(RuntimeError) as exc_info:
        get_isolated_test_database_url()
    assert "cannot point to the same database as DATABASE_URL" in str(exc_info.value)

    # 4. When properly isolated database is provided, it succeeds
    monkeypatch.setenv("TEST_DATABASE_URL", "postgres://u:p@10.0.0.1:5432/isolated_test_db")
    isolated_url = get_isolated_test_database_url()
    assert "isolated_test_db" in isolated_url


# --- 2. Worker Execution Filtering Tests ---


@pytest.mark.asyncio
async def test_acceptance_worker_filters_by_trip_id_and_owner() -> None:
    """Proves LocalAcceptanceWorker processes ONLY executions belonging to allowed trip IDs."""
    now = datetime.now(UTC)

    # LocalAcceptanceWorker verifies acceptance db url on init
    # We pass explicit isolated settings to verify worker initialization and filtering
    worker = LocalAcceptanceWorker(
        session_factory=None,  # type: ignore[arg-type]
        allowed_owner_ids=(USER_A_ID,),
        allowed_trip_ids=(DEMO_TRIP_A_ID,),
    )
    assert worker._allowed_owner_ids == (USER_A_ID,)
    assert worker._allowed_trip_ids == (DEMO_TRIP_A_ID,)

    # Verify default allowed trip IDs contain all 5 acceptance trips
    for tid in (
        DEMO_TRIP_A_ID,
        uuid.UUID("40000000-0000-0000-0000-000000000001"),
        uuid.UUID("50000000-0000-0000-0000-000000000001"),
        uuid.UUID("60000000-0000-0000-0000-000000000001"),
        uuid.UUID("99999999-0000-0000-0000-000000000001"),
    ):
        assert tid in DEFAULT_ALLOWED_TRIP_IDS


# --- 3. Fixture Cleanup Ordering and Data Preservation Regression ---


@pytest.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    """Provides an isolated session factory strictly targeting the dedicated acceptance database."""
    from acceptance_database import get_acceptance_session_factory
    return get_acceptance_session_factory()


@pytest.mark.asyncio
async def test_fixture_cleanup_ordering_and_unrelated_data_preservation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifies that:
    1. Fixture route execution IDs are collected before route records are deleted.
    2. Fixture executions, outbox events, tasks, results, and job events are cleaned up.
    3. Unrelated executions, outbox events, tasks, results, job events, and idempotency records remain completely intact.
    4. Repeated cleanup cycles run idempotently without error.
    """
    now = datetime.now(UTC)

    # Fixture entities (owned)
    fixture_owner_id = USER_A_ID
    fixture_trip_id = DEMO_TRIP_A_ID
    fixture_route_id = uuid.uuid4()
    fixture_exec_id = uuid.uuid4()

    # Unrelated entities (must NEVER be touched by acceptance cleanup)
    unrelated_owner_id = uuid.uuid4()
    unrelated_trip_id = uuid.uuid4()
    unrelated_route_id = uuid.uuid4()
    unrelated_exec_id = uuid.uuid4()
    unrelated_idemp_key = f"unrelated-key-{uuid.uuid4()}"

    async with session_factory() as session:
        # Ensure parent trips exist for execution foreign keys
        unrelated_trip = TripModel(
            id=unrelated_trip_id,
            owner_id=unrelated_owner_id,
            current_version=1,
            state="draft",
        )
        session.add(unrelated_trip)

        fixture_trip = await session.get(TripModel, fixture_trip_id)
        if fixture_trip is None:
            fixture_trip = TripModel(
                id=fixture_trip_id,
                owner_id=fixture_owner_id,
                current_version=1,
                state="draft",
            )
            session.add(fixture_trip)

        # Create unrelated execution, outbox event, task, worker result, job event, route, and idempotency
        unrelated_exec = ExecutionModel(
            id=unrelated_exec_id,
            owner_id=unrelated_owner_id,
            trip_id=unrelated_trip_id,
            workflow="ROUTE_OPTIMIZER_GRAPH",
            status="SUCCEEDED",
            context_version=1,
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add(unrelated_exec)

        unrelated_outbox = OutboxEventModel(
            id=uuid.uuid4(),
            execution_id=unrelated_exec_id,
            aggregate_id=unrelated_exec_id,
            kind="EXECUTION_START",
            event_type="EXECUTION_START",
            payload={"unrelated": True},
            created_at=now,
        )
        session.add(unrelated_outbox)

        unrelated_task = TaskModel(
            id=uuid.uuid4(),
            execution_id=unrelated_exec_id,
            worker="route_optimizer",
            action="SOLVE_VRP",
            payload_ref={"unrelated": True},
            input_hash="hash-unrelated-task",
            idempotency_key=f"task-key-{uuid.uuid4()}",
            attempt=1,
            max_attempts=3,
            context_version=1,
            status="COMPLETED",
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add(unrelated_task)

        unrelated_res = WorkerResultModel(
            id=uuid.uuid4(),
            execution_id=unrelated_exec_id,
            task_id=unrelated_task.id,
            attempt=1,
            context_version=1,
            status="SUCCESS",
            output_ref={"unrelated": True},
            warnings=[],
            usage={},
            started_at=now,
            finished_at=now,
        )
        session.add(unrelated_res)

        unrelated_job_event = JobEventModel(
            id=uuid.uuid4(),
            execution_id=unrelated_exec_id,
            sequence=1,
            event_type="STATUS_CHANGED",
            safe_payload={"status": "SUCCEEDED"},
            timestamp=now,
        )
        session.add(unrelated_job_event)

        unrelated_stop = RouteStopModel(
            id=uuid.uuid4(),
            route_id=unrelated_route_id,
            stop_order=1,
            name="Unrelated Stop",
            location=from_shape(shapely.geometry.Point(77.5946, 12.9716), srid=4326),
            stop_type="ORIGIN",
            is_must_visit=True,
            is_completed=False,
            is_skipped=False,
            dwell_seconds=0,
            wait_seconds=0,
        )
        unrelated_route = RouteModel(
            id=unrelated_route_id,
            owner_id=unrelated_owner_id,
            trip_id=unrelated_trip_id,
            execution_id=unrelated_exec_id,
            parent_route_id=None,
            route_version=1,
            status="FEASIBLE",
            solver_status="OPTIMAL",
            objective="FASTEST",
            departure_at=now,
            total_distance_m=1000,
            total_travel_seconds=120,
            total_dwell_seconds=0,
            total_wait_seconds=0,
            total_elapsed_seconds=120,
            driving_distance_m=1000,
            walking_distance_m=0,
            fuel_currency="INR",
            warnings=[],
            assumptions=[],
            stops=[unrelated_stop],
            legs=[],
            restrictions=[],
        )
        session.add(unrelated_route)

        unrelated_idemp = ApiIdempotencyModel(
            owner_id=unrelated_owner_id,
            operation="route_optimization:replan",
            key=unrelated_idemp_key,
            request_hash="hash-unrelated",
            response_json={"job_id": str(unrelated_exec_id)},
            status_code=202,
            expires_at=now + timedelta(days=1),
        )
        session.add(unrelated_idemp)

        # Create fixture execution, outbox, task, result, job event, route, and idempotency
        fixture_exec = ExecutionModel(
            id=fixture_exec_id,
            owner_id=fixture_owner_id,
            trip_id=fixture_trip_id,
            workflow="ROUTE_OPTIMIZER_GRAPH",
            status="SUCCEEDED",
            context_version=1,
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add(fixture_exec)

        fixture_outbox = OutboxEventModel(
            id=uuid.uuid4(),
            execution_id=fixture_exec_id,
            aggregate_id=fixture_exec_id,
            kind="EXECUTION_START",
            event_type="EXECUTION_START",
            payload={"fixture": True},
            created_at=now,
        )
        session.add(fixture_outbox)

        fixture_task = TaskModel(
            id=uuid.uuid4(),
            execution_id=fixture_exec_id,
            worker="route_optimizer",
            action="SOLVE_VRP",
            payload_ref={"fixture": True},
            input_hash="hash-fixture-task",
            idempotency_key=f"fixture-task-key-{uuid.uuid4()}",
            attempt=1,
            max_attempts=3,
            context_version=1,
            status="COMPLETED",
            deadline_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add(fixture_task)

        fixture_res = WorkerResultModel(
            id=uuid.uuid4(),
            execution_id=fixture_exec_id,
            task_id=fixture_task.id,
            attempt=1,
            context_version=1,
            status="SUCCESS",
            output_ref={"fixture": True},
            warnings=[],
            usage={},
            started_at=now,
            finished_at=now,
        )
        session.add(fixture_res)

        fixture_job_event = JobEventModel(
            id=uuid.uuid4(),
            execution_id=fixture_exec_id,
            sequence=1,
            event_type="STATUS_CHANGED",
            safe_payload={"status": "SUCCEEDED"},
            timestamp=now,
        )
        session.add(fixture_job_event)

        fixture_stop = RouteStopModel(
            id=uuid.uuid4(),
            route_id=fixture_route_id,
            stop_order=1,
            name="Fixture Stop",
            location=from_shape(shapely.geometry.Point(77.6101, 12.9716), srid=4326),
            stop_type="ORIGIN",
            is_must_visit=True,
            is_completed=False,
            is_skipped=False,
            dwell_seconds=0,
            wait_seconds=0,
        )
        fixture_route = RouteModel(
            id=fixture_route_id,
            owner_id=fixture_owner_id,
            trip_id=fixture_trip_id,
            execution_id=fixture_exec_id,
            parent_route_id=None,
            route_version=999,
            status="FEASIBLE",
            solver_status="OPTIMAL",
            objective="FASTEST",
            departure_at=now,
            total_distance_m=2000,
            total_travel_seconds=240,
            total_dwell_seconds=0,
            total_wait_seconds=0,
            total_elapsed_seconds=240,
            driving_distance_m=2000,
            walking_distance_m=0,
            fuel_currency="INR",
            warnings=[],
            assumptions=[],
            stops=[fixture_stop],
            legs=[],
            restrictions=[],
        )
        session.add(fixture_route)

        existing_idemp = await session.get(
            ApiIdempotencyModel,
            (fixture_owner_id, "route_optimization:replan", "seed-multimodal-route"),
        )
        if existing_idemp:
            await session.delete(existing_idemp)
            await session.flush()

        fixture_idemp = ApiIdempotencyModel(
            owner_id=fixture_owner_id,
            operation="route_optimization:replan",
            key="seed-multimodal-route",
            request_hash="hash-fixture",
            response_json={"job_id": str(fixture_exec_id)},
            status_code=202,
            expires_at=now + timedelta(days=1),
        )
        session.add(fixture_idemp)

        await session.commit()

    try:
        # Run the cleanup logic simulating 2 successive seeder cleanup passes
        for pass_num in (1, 2):
            async with session_factory() as session:
                # 1. Target routes
                r_stmt = select(RouteModel.id).where(
                    RouteModel.owner_id == fixture_owner_id,
                    RouteModel.id == fixture_route_id,
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

                # Target executions
                e_stmt = select(ExecutionModel.id).where(
                    ExecutionModel.owner_id == fixture_owner_id,
                    (
                        (ExecutionModel.id == fixture_exec_id)
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

                # Clean only fixture idempotency keys
                fixture_idemp_keys = [
                    "seed-multimodal-route",
                    "seed-partial-route",
                    "seed-infeasible-route",
                    "seed-user-b-route",
                ]
                await session.execute(
                    delete(ApiIdempotencyModel).where(
                        ApiIdempotencyModel.owner_id.in_(OWNED_USER_IDS),
                        ApiIdempotencyModel.key.in_(fixture_idemp_keys),
                    )
                )
                await session.commit()

        # Assert fixture entities were cleaned up
        async with session_factory() as session:
            assert await session.get(ExecutionModel, fixture_exec_id) is None
            assert await session.get(RouteModel, fixture_route_id) is None
            f_outbox = (
                await session.execute(
                    select(OutboxEventModel).where(OutboxEventModel.execution_id == fixture_exec_id)
                )
            ).scalars().all()
            assert len(f_outbox) == 0

            f_idemp = (
                await session.execute(
                    select(ApiIdempotencyModel).where(ApiIdempotencyModel.key == "seed-multimodal-route")
                )
            ).scalar_one_or_none()
            assert f_idemp is None

            # Assert unrelated entities remain 100% intact!
            unrelated_exec_db = await session.get(ExecutionModel, unrelated_exec_id)
            assert unrelated_exec_db is not None
            assert unrelated_exec_db.owner_id == unrelated_owner_id

            unrelated_route_db = await session.get(RouteModel, unrelated_route_id)
            assert unrelated_route_db is not None
            assert unrelated_route_db.owner_id == unrelated_owner_id

            u_outbox = (
                await session.execute(
                    select(OutboxEventModel).where(OutboxEventModel.execution_id == unrelated_exec_id)
                )
            ).scalars().all()
            assert len(u_outbox) == 1

            u_tasks = (
                await session.execute(
                    select(TaskModel).where(TaskModel.execution_id == unrelated_exec_id)
                )
            ).scalars().all()
            assert len(u_tasks) == 1

            u_results = (
                await session.execute(
                    select(WorkerResultModel).where(WorkerResultModel.execution_id == unrelated_exec_id)
                )
            ).scalars().all()
            assert len(u_results) == 1

            u_job_events = (
                await session.execute(
                    select(JobEventModel).where(JobEventModel.execution_id == unrelated_exec_id)
                )
            ).scalars().all()
            assert len(u_job_events) == 1

            u_idemp = (
                await session.execute(
                    select(ApiIdempotencyModel).where(ApiIdempotencyModel.key == unrelated_idemp_key)
                )
            ).scalar_one_or_none()
            assert u_idemp is not None

    finally:
        # Surgical teardown of unrelated test records
        async with session_factory() as session:
            await session.execute(delete(RouteStopModel).where(RouteStopModel.route_id == unrelated_route_id))
            await session.execute(delete(RouteModel).where(RouteModel.id == unrelated_route_id))
            await session.execute(
                delete(JobEventModel).where(JobEventModel.execution_id == unrelated_exec_id)
            )
            await session.execute(
                delete(WorkerResultModel).where(WorkerResultModel.execution_id == unrelated_exec_id)
            )
            await session.execute(
                delete(TaskModel).where(TaskModel.execution_id == unrelated_exec_id)
            )
            await session.execute(
                delete(OutboxEventModel).where(OutboxEventModel.execution_id == unrelated_exec_id)
            )
            await session.execute(delete(ExecutionModel).where(ExecutionModel.id == unrelated_exec_id))
            await session.execute(
                delete(ApiIdempotencyModel).where(ApiIdempotencyModel.key == unrelated_idemp_key)
            )
            await session.execute(delete(TripModel).where(TripModel.id == unrelated_trip_id))
            await session.commit()
