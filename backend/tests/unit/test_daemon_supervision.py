"""Unit tests for BackgroundDaemon failure supervision and lifecycle management."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from functions.orchestrator.execution.daemon import (
    BackgroundDaemon,
    DaemonSupervisorError,
    SupervisorConfig,
    WorkerState,
)


@pytest.mark.asyncio
async def test_daemon_starts_and_reports_healthy() -> None:
    """Verifies that supervised workers start and report healthy status."""
    run_event = asyncio.Event()

    async def mock_worker_1() -> None:
        run_event.set()
        while True:
            await asyncio.sleep(0.05)

    async def mock_worker_2() -> None:
        while True:
            await asyncio.sleep(0.05)

    config = SupervisorConfig(monitor_interval_seconds=0.05)
    daemon = BackgroundDaemon(
        transport=MagicMock(),
        supervisor_config=config,
        custom_workers={"worker1": mock_worker_1, "worker2": mock_worker_2},
    )

    daemon_task = asyncio.create_task(daemon.start())
    await run_event.wait()
    await asyncio.sleep(0.1)

    assert daemon.is_healthy is True
    status = daemon.get_status()
    assert status["worker1"]["state"] == WorkerState.RUNNING.value
    assert status["worker2"]["state"] == WorkerState.RUNNING.value
    assert status["worker1"]["restart_count"] == 0

    await daemon.stop()
    daemon_task.cancel()
    try:
        await daemon_task
    except asyncio.CancelledError:
        pass

    assert daemon.is_healthy is False
    status = daemon.get_status()
    assert status["worker1"]["state"] == WorkerState.STOPPED.value
    assert status["worker2"]["state"] == WorkerState.STOPPED.value


@pytest.mark.asyncio
async def test_daemon_restarts_failing_worker_with_backoff() -> None:
    """Verifies that a failing worker is caught and restarted while siblings continue."""
    worker1_attempts = 0
    worker2_running = True

    async def flaking_worker() -> None:
        nonlocal worker1_attempts
        worker1_attempts += 1
        if worker1_attempts == 1:
            raise RuntimeError("Transient crash in worker 1")
        # On second attempt, run normally
        while True:
            await asyncio.sleep(0.05)

    async def stable_worker() -> None:
        while worker2_running:
            await asyncio.sleep(0.05)

    config = SupervisorConfig(
        max_consecutive_failures=3,
        initial_backoff_seconds=0.1,
        backoff_multiplier=2.0,
        monitor_interval_seconds=0.05,
    )
    daemon = BackgroundDaemon(
        transport=MagicMock(),
        supervisor_config=config,
        custom_workers={"flaking": flaking_worker, "stable": stable_worker},
    )

    daemon_task = asyncio.create_task(daemon.start())

    # Wait for the first failure and restart to occur
    await asyncio.sleep(0.3)

    status = daemon.get_status()
    assert worker1_attempts >= 2, "Worker should have been restarted"
    assert status["flaking"]["restart_count"] >= 1
    assert "Transient crash in worker 1" in (status["flaking"]["last_error"] or "")
    assert status["stable"]["state"] == WorkerState.RUNNING.value
    assert status["stable"]["restart_count"] == 0
    assert daemon.is_healthy is True

    await daemon.stop()
    daemon_task.cancel()
    try:
        await daemon_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_daemon_halts_on_crash_loop_threshold() -> None:
    """Verifies that exceeding max consecutive failures halts the daemon safely."""
    failures = 0

    async def crash_loop_worker() -> None:
        nonlocal failures
        failures += 1
        raise ValueError(f"Crash loop error {failures}")

    config = SupervisorConfig(
        max_consecutive_failures=3,
        initial_backoff_seconds=0.05,
        backoff_multiplier=1.0,
        monitor_interval_seconds=0.02,
    )
    daemon = BackgroundDaemon(
        transport=MagicMock(),
        supervisor_config=config,
        custom_workers={"crasher": crash_loop_worker},
    )

    with pytest.raises(DaemonSupervisorError) as exc_info:
        await daemon.start()

    assert "crasher" in str(exc_info.value)
    assert daemon.is_healthy is False
    status = daemon.get_status()
    assert status["crasher"]["state"] == WorkerState.FAILED.value
    assert status["crasher"]["consecutive_failures"] >= 3


@pytest.mark.asyncio
async def test_daemon_resets_consecutive_failures_after_stability() -> None:
    """Verifies that consecutive failures reset to 0 after stable execution duration."""
    call_count = 0

    async def self_healing_worker() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Initial transient glitch")
        # Second attempt succeeds and runs stably
        while True:
            await asyncio.sleep(0.05)

    config = SupervisorConfig(
        max_consecutive_failures=3,
        initial_backoff_seconds=0.05,
        backoff_multiplier=1.0,
        consecutive_failure_reset_seconds=0.15,
        monitor_interval_seconds=0.03,
    )
    daemon = BackgroundDaemon(
        transport=MagicMock(),
        supervisor_config=config,
        custom_workers={"healer": self_healing_worker},
    )

    daemon_task = asyncio.create_task(daemon.start())

    # Wait past the recovery and stability threshold
    await asyncio.sleep(0.35)

    status = daemon.get_status()
    assert status["healer"]["restart_count"] >= 1
    assert status["healer"]["consecutive_failures"] == 0, "Should have reset consecutive failures after stable run"
    assert status["healer"]["state"] == WorkerState.RUNNING.value

    await daemon.stop()
    daemon_task.cancel()
    try:
        await daemon_task
    except asyncio.CancelledError:
        pass
