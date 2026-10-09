"""Transaction-bound checkpointer for LangGraph execution.

Buffers LangGraph state writes in memory and flushes them atomically with
SQLAlchemy sessions to prevent state divergence on concurrent runner crashes.
Provides durable reads from PostgreSQL for fresh processes and worker restarts.
"""

from __future__ import annotations

import collections
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    SerializerProtocol,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import LargeBinary, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class TransactionBoundCheckpointer(BaseCheckpointSaver):
    """LangGraph checkpointer that buffers writes and flushes atomically.

    Inherits from BaseCheckpointSaver to implement the LangGraph checkpoint API.
    State is buffered in memory during Phase 2 (LangGraph execution) and flushed
    to PostgreSQL in Phase 3 within the orchestrator's transaction boundaries.
    Reads check in-memory uncommitted buffers first, then query PostgreSQL.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde or JsonPlusSerializer())
        self._session_factory = session_factory
        # Invocation-scoped in-memory buffers mapped by thread_id
        self._checkpoints: dict[str, dict[str, Checkpoint]] = collections.defaultdict(dict)
        self._metadata: dict[str, dict[str, CheckpointMetadata]] = collections.defaultdict(dict)
        self._writes: dict[str, dict[str, list[tuple[str, str, Any]]]] = collections.defaultdict(
            lambda: collections.defaultdict(list)
        )
        self._pending_flushes: dict[str, list[Any]] = collections.defaultdict(list)

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession] | None:
        return self._session_factory

    @session_factory.setter
    def session_factory(self, factory: async_sessionmaker[AsyncSession] | None) -> None:
        self._session_factory = factory

    async def setup(self) -> None:
        """Called before use. Handled by schema migrations, no-op here."""
        pass

    def discard(self, thread_id: str) -> None:
        """Discards all buffered uncommitted checkpoints and pending writes for thread_id."""
        self._checkpoints.pop(thread_id, None)
        self._metadata.pop(thread_id, None)
        self._writes.pop(thread_id, None)
        self._pending_flushes.pop(thread_id, None)

    async def aget_tuple(
        self,
        config: Any,
    ) -> CheckpointTuple | None:
        """Gets a checkpoint tuple asynchronously, querying PostgreSQL when not in memory."""
        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)

        # 1. Check in-memory uncommitted buffer for current invocation
        if checkpoint_id:
            if checkpoint := self._checkpoints.get(thread_id, {}).get(checkpoint_id):
                metadata = self._metadata.get(thread_id, {}).get(checkpoint_id, {})
                writes = self._writes.get(thread_id, {}).get(checkpoint_id, [])
                parent_config = None
                if checkpoint.get("parent_checkpoint_id"):
                    parent_config = {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": checkpoint["parent_checkpoint_id"],
                        }
                    }
                return CheckpointTuple(
                    config,
                    checkpoint,
                    metadata,
                    parent_config,
                    writes,
                )
        else:
            if thread_id in self._checkpoints and self._checkpoints[thread_id]:
                checkpoints = self._checkpoints[thread_id]
                latest_id = max(checkpoints.keys())
                checkpoint = checkpoints[latest_id]
                metadata = self._metadata.get(thread_id, {}).get(latest_id, {})
                writes = self._writes.get(thread_id, {}).get(latest_id, [])
                parent_config = None
                if checkpoint.get("parent_checkpoint_id"):
                    parent_config = {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": checkpoint["parent_checkpoint_id"],
                        }
                    }
                return CheckpointTuple(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": latest_id,
                        }
                    },
                    checkpoint,
                    metadata,
                    parent_config,
                    writes,
                )

        # 2. Query PostgreSQL if session_factory is available
        if self._session_factory is not None:
            async with self._session_factory() as session:
                return await self._get_tuple_from_db(
                    session, thread_id, checkpoint_ns, checkpoint_id
                )

        return None

    def get_tuple(self, config: Any) -> CheckpointTuple | None:
        """Gets a checkpoint tuple from in-memory buffer."""
        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        if checkpoint_id := get_checkpoint_id(config):
            if checkpoint := self._checkpoints.get(thread_id, {}).get(checkpoint_id):
                parent_config = None
                if checkpoint.get("parent_checkpoint_id"):
                    parent_config = {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": checkpoint["parent_checkpoint_id"],
                        }
                    }
                return CheckpointTuple(
                    config,
                    checkpoint,
                    self._metadata.get(thread_id, {}).get(checkpoint_id, {}),
                    parent_config,
                    self._writes.get(thread_id, {}).get(checkpoint_id, []),
                )
        else:
            if thread_id in self._checkpoints and self._checkpoints[thread_id]:
                checkpoints = self._checkpoints[thread_id]
                latest_id = max(checkpoints.keys())
                checkpoint = checkpoints[latest_id]
                parent_config = None
                if checkpoint.get("parent_checkpoint_id"):
                    parent_config = {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": checkpoint["parent_checkpoint_id"],
                        }
                    }
                return CheckpointTuple(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": latest_id,
                        }
                    },
                    checkpoint,
                    self._metadata.get(thread_id, {}).get(latest_id, {}),
                    parent_config,
                    self._writes.get(thread_id, {}).get(latest_id, []),
                )
        return None

    async def _get_tuple_from_db(
        self,
        session: AsyncSession,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None,
    ) -> CheckpointTuple | None:
        if checkpoint_id:
            stmt = text("""
                SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata
                FROM checkpoints
                WHERE thread_id = :thread_id AND checkpoint_ns = :checkpoint_ns AND checkpoint_id = :checkpoint_id
                LIMIT 1;
            """)
            params = {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        else:
            stmt = text("""
                SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata
                FROM checkpoints
                WHERE thread_id = :thread_id AND checkpoint_ns = :checkpoint_ns
                ORDER BY checkpoint_id DESC
                LIMIT 1;
            """)
            params = {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
            }

        res = await session.execute(stmt, params)
        row = res.mappings().one_or_none()
        if row is None:
            return None

        chk_id = row["checkpoint_id"]
        chk_type = row["type"] or "msgpack"
        chk_bytes = bytes(row["checkpoint"])
        meta_bytes = bytes(row["metadata"])

        checkpoint = self.serde.loads_typed((chk_type, chk_bytes))
        metadata = self.serde.loads_typed(("msgpack", meta_bytes)) if meta_bytes else {}

        # Fetch pending writes from checkpoint_writes table
        writes_stmt = text("""
            SELECT task_id, channel, type, value
            FROM checkpoint_writes
            WHERE thread_id = :thread_id AND checkpoint_ns = :checkpoint_ns AND checkpoint_id = :checkpoint_id
            ORDER BY idx ASC;
        """)
        writes_res = await session.execute(
            writes_stmt,
            {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": chk_id,
            },
        )
        pending_writes = []
        for w_row in writes_res.mappings().all():
            w_type = w_row["type"] or "msgpack"
            w_val = self.serde.loads_typed((w_type, bytes(w_row["value"])))
            pending_writes.append((w_row["task_id"], w_row["channel"], w_val))

        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": chk_id,
            }
        }
        parent_config = None
        if row["parent_checkpoint_id"]:
            parent_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": row["parent_checkpoint_id"],
                }
            }

        return CheckpointTuple(
            config,
            checkpoint,
            metadata,
            parent_config,
            pending_writes,
        )

    async def alist(
        self,
        config: Any | None,
        *,
        filter: dict[str, Any] | None = None,
        before: Any | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """Lists checkpoints matching criteria from PostgreSQL."""
        if not config or self._session_factory is None:
            return

        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        before_id = get_checkpoint_id(before) if before else None

        query = """
            SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata
            FROM checkpoints
            WHERE thread_id = :thread_id AND checkpoint_ns = :checkpoint_ns
        """
        params: dict[str, Any] = {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
        }
        if before_id:
            query += " AND checkpoint_id < :before_id"
            params["before_id"] = before_id

        query += " ORDER BY checkpoint_id DESC"
        if limit:
            query += " LIMIT :limit"
            params["limit"] = limit

        async with self._session_factory() as session:
            res = await session.execute(text(query), params)
            rows = res.mappings().all()
            for row in rows:
                chk_id = row["checkpoint_id"]
                chk_type = row["type"] or "msgpack"
                chk_bytes = bytes(row["checkpoint"])
                meta_bytes = bytes(row["metadata"])
                checkpoint = self.serde.loads_typed((chk_type, chk_bytes))
                metadata = self.serde.loads_typed(("msgpack", meta_bytes)) if meta_bytes else {}

                writes_stmt = text("""
                    SELECT task_id, channel, type, value
                    FROM checkpoint_writes
                    WHERE thread_id = :thread_id AND checkpoint_ns = :checkpoint_ns AND checkpoint_id = :checkpoint_id
                    ORDER BY idx ASC;
                """)
                writes_res = await session.execute(
                    writes_stmt,
                    {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": chk_id,
                    },
                )
                pending_writes = []
                for w_row in writes_res.mappings().all():
                    w_type = w_row["type"] or "msgpack"
                    w_val = self.serde.loads_typed((w_type, bytes(w_row["value"])))
                    pending_writes.append((w_row["task_id"], w_row["channel"], w_val))

                tuple_config = {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": chk_id,
                    }
                }
                parent_config = None
                if row["parent_checkpoint_id"]:
                    parent_config = {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": row["parent_checkpoint_id"],
                        }
                    }

                yield CheckpointTuple(
                    tuple_config,
                    checkpoint,
                    metadata,
                    parent_config,
                    pending_writes,
                )

    async def aput(
        self,
        config: Any,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Any,
    ) -> Any:
        return self.put(config, checkpoint, metadata, new_versions)

    def put(
        self,
        config: Any,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Any,
    ) -> Any:
        """Buffers a checkpoint write in memory."""
        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        self._checkpoints[thread_id][checkpoint["id"]] = checkpoint
        self._metadata[thread_id][checkpoint["id"]] = metadata

        t_chk, b_chk = self.serde.dumps_typed(checkpoint)
        _, b_meta = self.serde.dumps_typed(metadata)

        # Buffer for atomic flush
        self._pending_flushes[thread_id].append({
            "type": "checkpoint",
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint["id"],
            "parent_checkpoint_id": config["configurable"].get("checkpoint_id"),
            "checkpoint_type": t_chk,
            "checkpoint_data": b_chk,
            "metadata_data": b_meta,
        })

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def aput_writes(
        self,
        config: Any,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
    ) -> None:
        self.put_writes(config, writes, task_id)

    def put_writes(
        self,
        config: Any,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
    ) -> None:
        """Buffers pending channel writes."""
        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        if not checkpoint_id:
            if thread_id in self._checkpoints and self._checkpoints[thread_id]:
                checkpoint_id = max(self._checkpoints[thread_id].keys())
            else:
                checkpoint_id = "1"
        for idx, (channel, value) in enumerate(writes):
            self._writes[thread_id][checkpoint_id].append((task_id, channel, value))

            t_val, b_val = self.serde.dumps_typed(value)
            self._pending_flushes[thread_id].append({
                "type": "write",
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
                "task_id": task_id,
                "idx": idx,
                "channel": channel,
                "write_type": t_val,
                "value_data": b_val,
            })

    async def flush(self, session: AsyncSession, thread_id: str) -> None:
        """Flushes buffered writes for the given thread_id atomically via SQLAlchemy session.

        Pending writes are only removed from the buffer AFTER all writes succeed.
        If any write raises, the buffer is left intact so the enclosing transaction
        can roll back and a subsequent retry sees the full pending set.

        Raises RuntimeError if session_factory is None and there are buffered items
        that would otherwise be silently discarded.
        """
        if self._session_factory is None and session.bind is not None:
            self._session_factory = async_sessionmaker(
                bind=session.bind, expire_on_commit=False, class_=AsyncSession
            )

        # Peek without popping: only clear the buffer after every write succeeds.
        pending = self._pending_flushes.get(thread_id, [])

        if not pending:
            # Nothing to flush; clear stale in-memory caches so subsequent
            # reads query PostgreSQL (they were already persisted previously).
            if self._session_factory is not None:
                self._checkpoints.pop(thread_id, None)
                self._metadata.pop(thread_id, None)
                self._writes.pop(thread_id, None)
            return

        if self._session_factory is None:
            raise RuntimeError(
                f"TransactionBoundCheckpointer.flush() called with {len(pending)} pending "
                "writes but session_factory is None. Configure session_factory before "
                "flushing or the writes will be silently lost."
            )

        for item in pending:
            if item["type"] == "checkpoint":
                stmt = text("""
                    INSERT INTO checkpoints
                    (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata)
                    VALUES
                    (:thread_id, :checkpoint_ns, :checkpoint_id, :parent_checkpoint_id, :type, :checkpoint, :metadata)
                    ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) DO UPDATE SET
                    checkpoint = EXCLUDED.checkpoint,
                    metadata = EXCLUDED.metadata;
                """).bindparams(
                    bindparam("checkpoint", type_=LargeBinary),
                    bindparam("metadata", type_=LargeBinary),
                )
                await session.execute(
                    stmt,
                    {
                        "thread_id": item["thread_id"],
                        "checkpoint_ns": item["checkpoint_ns"],
                        "checkpoint_id": item["checkpoint_id"],
                        "parent_checkpoint_id": item["parent_checkpoint_id"],
                        "type": item["checkpoint_type"],
                        "checkpoint": item["checkpoint_data"],
                        "metadata": item["metadata_data"],
                    },
                )
            elif item["type"] == "write":
                stmt = text("""
                    INSERT INTO checkpoint_writes
                    (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value)
                    VALUES
                    (:thread_id, :checkpoint_ns, :checkpoint_id, :task_id, :idx, :channel, :type, :value)
                    ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id, task_id, idx) DO NOTHING;
                """).bindparams(
                    bindparam("value", type_=LargeBinary),
                )
                await session.execute(
                    stmt,
                    {
                        "thread_id": item["thread_id"],
                        "checkpoint_ns": item["checkpoint_ns"],
                        "checkpoint_id": item["checkpoint_id"],
                        "task_id": item["task_id"],
                        "idx": item["idx"],
                        "channel": item["channel"],
                        "type": item["write_type"],
                        "value": item["value_data"],
                    },
                )

        # All writes succeeded — now safe to clear the buffer and in-memory
        # caches.  Subsequent reads will go to PostgreSQL.
        del self._pending_flushes[thread_id]
        self._checkpoints.pop(thread_id, None)
        self._metadata.pop(thread_id, None)
        self._writes.pop(thread_id, None)



def get_checkpoint_id(config: Any) -> str | None:
    """Helper to extract checkpoint_id from config."""
    return config.get("configurable", {}).get("checkpoint_id")
