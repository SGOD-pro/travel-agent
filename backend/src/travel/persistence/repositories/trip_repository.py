"""SQLAlchemy implementation of TripRepositoryPort."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from travel.application.ports.repository import TripRepositoryPort, TripVersionConflictError
from travel.domain.trips import Trip, TripBrief, TripState, TripVersion
from travel.persistence.models import TripModel, TripVersionModel


class SqlAlchemyTripRepository(TripRepositoryPort):
    """PostgreSQL-backed trip repository with optimistic concurrency control."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, trip_id: uuid.UUID) -> Trip | None:
        stmt = select(TripModel).where(TripModel.id == trip_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if not model:
            return None
        return Trip(
            id=model.id,
            owner_id=model.owner_id,
            current_version=model.current_version,
            state=TripState(model.state),
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def save(self, trip: Trip) -> None:
        model = await self._session.get(TripModel, trip.id)
        if not model:
            model = TripModel(
                id=trip.id,
                owner_id=trip.owner_id,
                current_version=trip.current_version,
                state=trip.state.value,
                created_at=trip.created_at,
                updated_at=trip.updated_at,
            )
            self._session.add(model)
        else:
            model.current_version = trip.current_version
            model.state = trip.state.value
            model.updated_at = trip.updated_at

    async def commit_version(
        self,
        trip_id: uuid.UUID,
        expected_base_version: int,
        brief: TripBrief,
        command_id: uuid.UUID,
    ) -> TripVersion:
        # 1. Idempotency check: Has this command_id already produced a version for this trip?
        idempotency_stmt = select(TripVersionModel).where(
            TripVersionModel.trip_id == trip_id,
            TripVersionModel.command_id == command_id,
        )
        existing = (await self._session.execute(idempotency_stmt)).scalar_one_or_none()
        if existing:
            return TripVersion(
                trip_id=existing.trip_id,
                version=existing.version,
                brief=TripBrief.model_validate(existing.brief_json),
                command_id=existing.command_id,
                schema_version=existing.schema_version,
                created_at=existing.created_at,
            )

        new_version = expected_base_version + 1
        now = datetime.now(UTC)

        # 2. Atomic optimistic locking update
        update_stmt = (
            update(TripModel)
            .where(TripModel.id == trip_id, TripModel.current_version == expected_base_version)
            .values(current_version=new_version, updated_at=now)
        )
        res = await self._session.execute(update_stmt)
        assert isinstance(res, CursorResult)
        if res.rowcount == 0:
            # Conflict: fetch actual current version to produce informative error
            trip = await self.get_by_id(trip_id)
            actual_version = trip.current_version if trip else 0
            raise TripVersionConflictError(
                trip_id=trip_id,
                current_version=actual_version,
                expected_version=expected_base_version,
            )

        # 3. Persist new immutable version snapshot
        version_model = TripVersionModel(
            trip_id=trip_id,
            version=new_version,
            brief_json=brief.model_dump(mode="json"),
            command_id=command_id,
            schema_version=1,
            created_at=now,
        )
        self._session.add(version_model)

        return TripVersion(
            trip_id=trip_id,
            version=new_version,
            brief=brief,
            command_id=command_id,
            schema_version=1,
            created_at=now,
        )

    async def get_version(self, trip_id: uuid.UUID, version: int) -> TripVersion | None:
        stmt = select(TripVersionModel).where(
            TripVersionModel.trip_id == trip_id,
            TripVersionModel.version == version,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        if not model:
            return None
        return TripVersion(
            trip_id=model.trip_id,
            version=model.version,
            brief=TripBrief.model_validate(model.brief_json),
            command_id=model.command_id,
            schema_version=model.schema_version,
            created_at=model.created_at,
        )
