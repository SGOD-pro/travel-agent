"""Continuous background daemon for outbox relay and task recovery with failure supervision."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
from typing import Any

from config.db import get_session_factory
from functions.orchestrator.execution.recovery import TaskRecoveryService
from functions.orchestrator.execution.relay import OutboxRelayService
from functions.orchestrator.execution.transport import build_sqs_transport

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class WorkerState(str, Enum):
    """Lifecycle state of a supervised worker loop."""

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    RESTARTING = "RESTARTING"
    FAILED = "FAILED"


@dataclass
class SupervisorConfig:
    """Supervision configuration for daemon worker tasks."""

    max_consecutive_failures: int = 5
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    backoff_multiplier: float = 2.0
    consecutive_failure_reset_seconds: float = 15.0
    monitor_interval_seconds: float = 0.2


@dataclass
class SupervisedWorkerInfo:
    """Status metadata for a supervised daemon worker."""

    name: str
    state: WorkerState = WorkerState.STOPPED
    restart_count: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    last_started_at: float | None = None
    last_failed_at: float | None = None


class DaemonSupervisorError(Exception):
    """Raised when daemon supervisor encounters unrecoverable failure."""


class BackgroundDaemon:
    """Manages continuous execution of relay and recovery loops with failure supervision."""

    def __init__(
        self,
        *,
        session_factory: Any | None = None,
        relay_service: OutboxRelayService | None = None,
        recovery_service: TaskRecoveryService | None = None,
        transport: Any | None = None,
        supervisor_config: SupervisorConfig | None = None,
        custom_workers: dict[str, Callable[[], Coroutine[Any, Any, None]]] | None = None,
    ) -> None:
        self.session_factory = session_factory or get_session_factory()
        self.relay_token = uuid.uuid4()
        self.supervisor_config = supervisor_config or SupervisorConfig()

        if transport is not None:
            self.transport = transport
        else:
            self.transport = build_sqs_transport()

        self.relay_service = relay_service or OutboxRelayService(
            session_factory=self.session_factory,
            transport=self.transport,
            batch_size=50,
        )
        self.recovery_service = recovery_service or TaskRecoveryService(
            session_factory=self.session_factory,
            batch_size=50,
        )

        self._running = False
        self._custom_workers = custom_workers
        self._worker_infos: dict[str, SupervisedWorkerInfo] = {}
        self._worker_tasks: dict[str, asyncio.Task[None]] = {}
        self._next_restart_at: dict[str, float] = {}
        self._supervisor_task: asyncio.Task[None] | None = None

    @property
    def is_healthy(self) -> bool:
        """Returns True if running and no supervised worker is in FAILED state."""
        if not self._running:
            return False
        return all(info.state != WorkerState.FAILED for info in self._worker_infos.values())

    def get_status(self) -> dict[str, dict[str, Any]]:
        """Returns current operational status of all supervised workers."""
        return {
            name: {
                "state": info.state.value,
                "restart_count": info.restart_count,
                "consecutive_failures": info.consecutive_failures,
                "last_error": info.last_error,
                "last_started_at": info.last_started_at,
                "last_failed_at": info.last_failed_at,
            }
            for name, info in self._worker_infos.items()
        }

    async def _relay_loop(self) -> None:
        """Continuously claims and publishes outbox events."""
        logger.info("Starting outbox relay loop")
        while self._running:
            try:
                messages = await self.relay_service.claim_due_batch(
                    relay_token=self.relay_token, batch_size=50
                )
                if messages:
                    logger.info("Relayed %d outbox events", len(messages))
                    for msg in messages:
                        try:
                            await self.transport.publish(msg)
                            await self.relay_service.confirm_delivery(
                                event_id=msg.event_id, relay_token=self.relay_token
                            )
                        except Exception as e:
                            logger.error("Failed to publish %s: %s", msg.event_id, e)
                            await self.relay_service.handle_delivery_failure(
                                event_id=msg.event_id,
                                relay_token=self.relay_token,
                                current_attempts=msg.attempts,
                                error_message=str(e),
                            )
                else:
                    await asyncio.sleep(1.0)
            except Exception as e:
                logger.exception("Error in relay loop: %s", e)
                await asyncio.sleep(5.0)

    async def _recovery_loop(self) -> None:
        """Continuously scans and recovers stalled tasks and executions."""
        logger.info("Starting task recovery loop")
        while self._running:
            try:
                result = await self.recovery_service.recover_stalled_tasks(
                    relay_token=self.relay_token, max_runtime_seconds=25.0
                )
                if result.total_recovered > 0 or result.total_exhausted > 0:
                    logger.info(
                        "Recovery run: %d recovered, %d exhausted",
                        result.total_recovered,
                        result.total_exhausted,
                    )
                await asyncio.sleep(10.0)
            except Exception as e:
                logger.exception("Error in recovery loop: %s", e)
                await asyncio.sleep(10.0)

    async def _run_supervised_loop(
        self, name: str, target: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        """Wrapper around target loop that reports failures to supervisor."""
        info = self._worker_infos[name]
        info.state = WorkerState.RUNNING
        info.last_started_at = time.monotonic()
        try:
            await target()
            if self._running:
                logger.warning("Supervised worker '%s' exited unexpectedly while running", name)
                info.state = WorkerState.RESTARTING
                info.consecutive_failures += 1
                info.restart_count += 1
                info.last_failed_at = time.monotonic()
                backoff = min(
                    self.supervisor_config.initial_backoff_seconds
                    * (self.supervisor_config.backoff_multiplier ** (info.consecutive_failures - 1)),
                    self.supervisor_config.max_backoff_seconds,
                )
                self._next_restart_at[name] = time.monotonic() + backoff
        except asyncio.CancelledError:
            info.state = WorkerState.STOPPED
            raise
        except Exception as exc:
            info.last_error = str(exc)
            info.last_failed_at = time.monotonic()
            info.consecutive_failures += 1
            info.restart_count += 1
            logger.exception(
                "Supervised worker '%s' crashed with error: %s (consecutive failures: %d)",
                name,
                exc,
                info.consecutive_failures,
            )
            if info.consecutive_failures >= self.supervisor_config.max_consecutive_failures:
                info.state = WorkerState.FAILED
                logger.critical(
                    "Worker '%s' exceeded max consecutive failures (%d). State set to FAILED.",
                    name,
                    self.supervisor_config.max_consecutive_failures,
                )
            else:
                info.state = WorkerState.RESTARTING
                backoff = min(
                    self.supervisor_config.initial_backoff_seconds
                    * (self.supervisor_config.backoff_multiplier ** (info.consecutive_failures - 1)),
                    self.supervisor_config.max_backoff_seconds,
                )
                self._next_restart_at[name] = time.monotonic() + backoff

    async def _supervision_monitor(
        self, workers: dict[str, Callable[[], Coroutine[Any, Any, None]]]
    ) -> None:
        """Supervises running workers, handles restarts with backoff, halts on crash-loop."""
        while self._running:
            now = time.monotonic()
            for name, target in workers.items():
                info = self._worker_infos[name]

                # Reset consecutive failures if healthy for long enough
                if (
                    info.state == WorkerState.RUNNING
                    and info.consecutive_failures > 0
                    and info.last_started_at
                    and (now - info.last_started_at)
                    >= self.supervisor_config.consecutive_failure_reset_seconds
                ):
                    info.consecutive_failures = 0

                # Check if restart due
                if info.state == WorkerState.RESTARTING:
                    restart_due = self._next_restart_at.get(name, 0.0)
                    if now >= restart_due:
                        logger.info("Supervisor restarting worker '%s' (restart #%d)", name, info.restart_count)
                        info.state = WorkerState.STARTING
                        self._worker_tasks[name] = asyncio.create_task(
                            self._run_supervised_loop(name, target)
                        )

                # Check if failed fatally
                if info.state == WorkerState.FAILED:
                    logger.critical(
                        "Supervisor halting daemon due to fatal failure in worker '%s'", name
                    )
                    self._running = False
                    raise DaemonSupervisorError(
                        f"Supervised worker '{name}' failed permanently: {info.last_error}"
                    )

            await asyncio.sleep(self.supervisor_config.monitor_interval_seconds)

    async def start(self) -> None:
        """Starts and supervises daemon loops."""
        self._running = True
        workers = (
            self._custom_workers
            if self._custom_workers is not None
            else {
                "relay": self._relay_loop,
                "recovery": self._recovery_loop,
            }
        )

        for name in workers:
            self._worker_infos[name] = SupervisedWorkerInfo(name=name, state=WorkerState.STARTING)

        for name, target in workers.items():
            self._worker_tasks[name] = asyncio.create_task(
                self._run_supervised_loop(name, target)
            )

        try:
            await self._supervision_monitor(workers)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stops the daemon and cancels all supervised tasks cleanly."""
        self._running = False
        for _name, task in list(self._worker_tasks.items()):
            if not task.done():
                task.cancel()
        if self._worker_tasks:
            tasks_to_wait = list(self._worker_tasks.values())
            # Give cancelled tasks a small bounded window to actually clean up
            _, pending = await asyncio.wait(tasks_to_wait, timeout=2.0)
            if pending:
                logger.warning("Daemon stop timed out waiting for %d tasks to cancel", len(pending))
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)
            self._worker_tasks.clear()
        for info in self._worker_infos.values():
            if info.state != WorkerState.FAILED:
                info.state = WorkerState.STOPPED


async def main() -> None:
    daemon = BackgroundDaemon()
    try:
        await daemon.start()
    except (KeyboardInterrupt, DaemonSupervisorError) as e:
        logger.info("Daemon terminated: %s", e)
        await daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())
