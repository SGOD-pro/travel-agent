"""Durable evidence budget reservation, reconciliation, and crash recovery.

Conforms to spec 0001 & spec 0002 (AC-14) and addresses backend audit finding 2:
1. Makes reservations identifiable by execution, task, and attempt.
2. Recovers abandoned reservations safely after lease expiry or worker crash.
3. Preserves uncertain provider spend: does not assume interrupted calls were free.
4. Prevents stale workers from reconciling or spending after lease loss.
5. Guarantees idempotent recovery and repeated reconciliation.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from functions.orchestrator.execution.models import ExecutionModel, TaskModel

logger = logging.getLogger(__name__)


class BudgetReservationStatus:
    ACTIVE = "ACTIVE"
    RECONCILED = "RECONCILED"
    ABANDONED_UNCERTAIN = "ABANDONED_UNCERTAIN"


def ensure_execution_budget_structure(budget: dict[str, Any] | None) -> dict[str, Any]:
    """Ensures standard budget fields exist on execution.budget dict."""
    b = dict(budget or {})
    b.setdefault("remaining_tool_calls", 5)
    b.setdefault("remaining_cost_minor", 500)
    b.setdefault("currency", "INR")
    b.setdefault("reserved_tool_calls", 0)
    b.setdefault("reserved_cost_minor", 0)
    b.setdefault("spent_tool_calls", 0)
    b.setdefault("spent_cost_minor", 0)
    b.setdefault("uncertain_tool_calls", 0)
    b.setdefault("uncertain_cost_minor", 0)
    b.setdefault("reservations", {})
    return b


def ensure_route_fallback_budget_structure(
    budget: dict[str, Any] | None, *, default_cap: int = 3
) -> dict[str, Any]:
    """Ensures dedicated route fallback budget namespace exists on execution.budget dict."""
    b = dict(budget or {})
    rf = dict(b.get("route_fallback") or {})
    rf.setdefault("remaining_attempts", default_cap)
    rf.setdefault("reserved_attempts", 0)
    rf.setdefault("spent_attempts", 0)
    rf.setdefault("uncertain_attempts", 0)
    rf.setdefault("reservations", {})
    rf.setdefault("probes", {})
    b["route_fallback"] = rf
    return b


async def reserve_route_fallback_attempt(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    probe_key: str,
    task_id: uuid.UUID | None = None,
    attempt: int | None = None,
    lease_token: uuid.UUID | None = None,
    reservation_id: str | None = None,
    max_attempts_cap: int = 3,
) -> tuple[str | None, str | None]:
    """Atomically reserves and commits 1 outbound /route attempt under parent-first locks.

    Returns (reservation_id, denial_reason). Rejects when lease, deadline, or attempt budget
    is invalid or exhausted without leaving open transactions.
    """
    exec_stmt = select(ExecutionModel).where(ExecutionModel.id == execution_id).with_for_update()
    exec_res = await session.execute(exec_stmt)
    execution = exec_res.scalar_one_or_none()
    if execution is None:
        return None, "execution_not_found"

    if execution.status == "CANCELLED" or execution.cancelled_at is not None:
        return None, "execution_cancelled"
    if execution.status in ("SUCCEEDED", "FAILED", "PARTIAL", "SUPERSEDED", "COMPLETED"):
        return None, "execution_terminal"

    now = datetime.now(UTC)
    if task_id is not None:
        task_stmt = select(TaskModel).where(TaskModel.id == task_id).with_for_update()
        task_res = await session.execute(task_stmt)
        task = task_res.scalar_one_or_none()
        now = datetime.now(UTC)
        if task is None:
            return None, "task_not_found"
        if execution.deadline_at and execution.deadline_at <= now:
            return None, "deadline_exceeded"
        if task.deadline_at and task.deadline_at <= now:
            return None, "deadline_exceeded"
        if lease_token is None or task.lease_token != lease_token:
            return None, "invalid_lease"
        if task.lease_expiry is None or task.lease_expiry <= now:
            return None, "lease_expired"
        if attempt is not None and task.attempt != attempt:
            return None, "stale_attempt"
        if task.status not in ("CLAIMED", "RUNNING"):
            return None, "task_not_active"
    else:
        if execution.deadline_at and execution.deadline_at <= now:
            return None, "deadline_exceeded"
        if lease_token is not None:
            return None, "invalid_lease"

    b = ensure_route_fallback_budget_structure(execution.budget, default_cap=max_attempts_cap)
    rf = dict(b["route_fallback"])
    remaining = int(rf.get("remaining_attempts", max_attempts_cap))
    reserved = int(rf.get("reserved_attempts", 0))
    if remaining - reserved < 1:
        return None, "budget_exhausted"

    res_id = reservation_id or str(uuid.uuid4())
    reservations = dict(rf.get("reservations") or {})
    reservations[res_id] = {
        "reservation_id": res_id,
        "task_id": str(task_id) if task_id else None,
        "attempt": attempt,
        "lease_token": str(lease_token) if lease_token else None,
        "probe_key": probe_key,
        "status": BudgetReservationStatus.ACTIVE,
        "created_at": now.isoformat(),
        "reconciled_at": None,
        "recovered_at": None,
        "settled_as": None,
    }
    rf["reservations"] = reservations
    rf["reserved_attempts"] = reserved + 1
    b["route_fallback"] = rf
    execution.budget = b
    flag_modified(execution, "budget")
    await session.commit()
    return res_id, None


async def reconcile_route_fallback_attempt(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    reservation_id: str,
    success: bool,
    task_id: uuid.UUID | None = None,
    attempt: int | None = None,
    lease_token: uuid.UUID | None = None,
    dispatched: bool = True,
    outcome: str | None = None,
    probe_key: str | None = None,
    probe_payload: dict[str, Any] | None = None,
) -> bool:
    """Reconciles a durable /route fallback attempt reservation and optionally caches probe outcome."""
    if not getattr(session, "is_active", True):
        return False

    exec_stmt = select(ExecutionModel).where(ExecutionModel.id == execution_id).with_for_update()
    exec_res = await session.execute(exec_stmt)
    execution = exec_res.scalar_one_or_none()
    if execution is None or not execution.budget:
        return False

    now = datetime.now(UTC)
    if task_id is not None:
        task_stmt = select(TaskModel).where(TaskModel.id == task_id).with_for_update()
        task_res = await session.execute(task_stmt)
        task = task_res.scalar_one_or_none()
        now = datetime.now(UTC)
        if task is None:
            return False
        if lease_token is not None and task.lease_token != lease_token:
            return False
        if attempt is not None and task.attempt != attempt:
            return False

    b = ensure_route_fallback_budget_structure(execution.budget)
    rf = dict(b["route_fallback"])
    reservations = dict(rf.get("reservations") or {})
    res_entry = reservations.get(reservation_id)
    if not isinstance(res_entry, dict):
        return False

    if res_entry.get("task_id") is not None and (
        task_id is None or str(task_id) != res_entry.get("task_id")
    ):
        return False
    if res_entry.get("attempt") is not None and (
        attempt is None or attempt != res_entry.get("attempt")
    ):
        return False
    if res_entry.get("lease_token") is not None and (
        lease_token is None or str(lease_token) != res_entry.get("lease_token")
    ):
        return False
    if task_id is None and lease_token is not None:
        return False

    if res_entry.get("status") != BudgetReservationStatus.ACTIVE:
        return True

    res_entry = dict(res_entry)
    res_entry["status"] = BudgetReservationStatus.RECONCILED
    res_entry["reconciled_at"] = now.isoformat()
    rf["reserved_attempts"] = max(0, int(rf.get("reserved_attempts", 0)) - 1)

    if success:
        res_entry["settled_as"] = "SUCCESS"
        rf["spent_attempts"] = int(rf.get("spent_attempts", 0)) + 1
        rf["remaining_attempts"] = max(0, int(rf.get("remaining_attempts", 0)) - 1)
    elif not dispatched or outcome == "NOT_DISPATCHED":
        res_entry["settled_as"] = "NOT_DISPATCHED"
    else:
        res_entry["settled_as"] = outcome or "UNCERTAIN"
        rf["uncertain_attempts"] = int(rf.get("uncertain_attempts", 0)) + 1
        rf["spent_attempts"] = int(rf.get("spent_attempts", 0)) + 1
        rf["remaining_attempts"] = max(0, int(rf.get("remaining_attempts", 0)) - 1)

    if probe_key and probe_payload is not None:
        probes = dict(rf.get("probes") or {})
        probes[probe_key] = probe_payload
        rf["probes"] = probes

    reservations[reservation_id] = res_entry
    rf["reservations"] = reservations
    b["route_fallback"] = rf
    execution.budget = b
    flag_modified(execution, "budget")
    await session.commit()
    return True


async def reserve_budget_entry(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    cost_minor: int,
    task_id: uuid.UUID | None = None,
    attempt: int | None = None,
    lease_token: uuid.UUID | None = None,
    reservation_id: str | None = None,
) -> str | None:
    """Atomically reserves budget against ExecutionModel with task and attempt identity.

    Enforces parent-first lock ordering (executions -> tasks).
    Re-evaluates current time after acquiring locks so expired leases cannot reserve after a lock wait.
    Validates execution status, cancellation, deadline, and task lease active fencing.
    Returns reservation_id on success, or None if budget exhausted or lease invalid.
    """
    # 1. Lock Parent Execution FIRST
    exec_stmt = select(ExecutionModel).where(ExecutionModel.id == execution_id).with_for_update()
    exec_res = await session.execute(exec_stmt)
    execution = exec_res.scalar_one_or_none()
    if not execution:
        return None

    now = datetime.now(UTC)
    if execution.status == "CANCELLED" or execution.cancelled_at is not None:
        return None
    if execution.deadline_at and execution.deadline_at <= now:
        return None

    # 2. Lock and Validate Child Task SECOND (if task_id provided)
    if task_id is not None:
        task_stmt = select(TaskModel).where(TaskModel.id == task_id).with_for_update()
        task_res = await session.execute(task_stmt)
        task = task_res.scalar_one_or_none()
        if not task:
            logger.warning("Task %s not found for budget reservation", task_id)
            return None

        # Re-evaluate current time after acquiring task lock (lock wait safety)
        now = datetime.now(UTC)

        # Verify task is active and caller's lease is valid
        if lease_token is not None and task.lease_token != lease_token:
            logger.warning(
                "Worker lease token %s is stale for task %s (active lease: %s)",
                lease_token,
                task_id,
                task.lease_token,
            )
            return None
        if task.lease_expiry is not None and task.lease_expiry <= now:
            logger.warning("Worker lease expired for task %s", task_id)
            return None
        if attempt is not None and task.attempt != attempt:
            logger.warning(
                "Worker attempt %s is stale for task %s (current attempt: %s)",
                attempt,
                task_id,
                task.attempt,
            )
            return None
        if task.status not in ("CLAIMED", "RUNNING"):
            logger.warning(
                "Task %s is not active (%s); refusing budget reservation",
                task_id,
                task.status,
            )
            return None

    # 3. Check and Reserve Budget Counters
    budget = ensure_execution_budget_structure(execution.budget)

    remaining_calls = int(budget.get("remaining_tool_calls", 999999))
    remaining_cost = int(budget.get("remaining_cost_minor", 999999999))
    reserved_calls = int(budget.get("reserved_tool_calls", 0))
    reserved_cost = int(budget.get("reserved_cost_minor", 0))

    available_calls = remaining_calls - reserved_calls
    available_cost = remaining_cost - reserved_cost

    if available_calls < 1 or available_cost < cost_minor:
        return None

    res_id = reservation_id or str(uuid.uuid4())
    reservations = dict(budget.get("reservations") or {})
    reservations[res_id] = {
        "reservation_id": res_id,
        "task_id": str(task_id) if task_id else None,
        "attempt": attempt,
        "lease_token": str(lease_token) if lease_token else None,
        "cost_minor": cost_minor,
        "status": BudgetReservationStatus.ACTIVE,
        "created_at": now.isoformat(),
        "reconciled_at": None,
        "recovered_at": None,
        "settled_as": None,
    }

    budget["reservations"] = reservations
    budget["reserved_tool_calls"] = reserved_calls + 1
    budget["reserved_cost_minor"] = reserved_cost + cost_minor

    execution.budget = budget
    flag_modified(execution, "budget")
    await session.commit()
    return res_id


async def reconcile_budget_entry(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    cost_minor: int,
    success: bool,
    reservation_id: str | None = None,
    task_id: uuid.UUID | None = None,
    attempt: int | None = None,
    lease_token: uuid.UUID | None = None,
    dispatched: bool = True,
    outcome: str | None = None,
) -> bool:
    """Atomically reconciles budget reservation.

    Enforces parent-first lock ordering (executions -> tasks).
    Re-evaluates current time after lock wait.
    Binds reconciliation to the stored reservation's execution, task, attempt,
    lease token, and reserved cost. Missing or mismatched reservations do not
    change counters.
    Preserves uncertain spend when a dispatched call times out or fails.
    Repeated valid reconciliation is idempotent.
    """
    if not getattr(session, "is_active", True):
        logger.warning("Session inactive; skipping budget reconciliation for %s", execution_id)
        return False

    # 1. Lock Parent Execution FIRST
    exec_stmt = select(ExecutionModel).where(ExecutionModel.id == execution_id).with_for_update()
    exec_res = await session.execute(exec_stmt)
    execution = exec_res.scalar_one_or_none()
    if not execution or not execution.budget:
        return False

    now = datetime.now(UTC)

    budget = ensure_execution_budget_structure(execution.budget)
    reservations = dict(budget.get("reservations") or {})

    # 2. Check task lease validity if task_id provided
    if task_id is not None:
        task_stmt = select(TaskModel).where(TaskModel.id == task_id).with_for_update()
        task_res = await session.execute(task_stmt)
        task = task_res.scalar_one_or_none()
        if not task:
            logger.warning("Task %s not found for reconciliation", task_id)
            return False

        # Re-evaluate current time after acquiring task lock (lock wait safety)
        now = datetime.now(UTC)

        if lease_token is not None and task.lease_token != lease_token:
            logger.warning(
                "Stale worker with lease %s attempted to reconcile task %s (active lease: %s)",
                lease_token,
                task_id,
                task.lease_token,
            )
            return False
        if attempt is not None and task.attempt != attempt:
            logger.warning(
                "Worker attempt %s is stale for task %s (current attempt: %s)",
                attempt,
                task_id,
                task.attempt,
            )
            return False
        if task.lease_expiry is not None and task.lease_expiry <= now:
            logger.warning("Worker lease expired for task %s after lock wait", task_id)
            return False

    # 3. Locate and strictly bind to the stored reservation entry
    if not reservation_id or reservation_id not in reservations:
        logger.warning(
            "Reservation %s is missing from execution %s; refusing reconciliation without modifying counters",
            reservation_id,
            execution_id,
        )
        return False

    res_entry = reservations[reservation_id]

    # Validate binding: task_id, attempt, lease_token, and reserved cost
    if res_entry.get("task_id") is not None:
        if task_id is None or str(task_id) != str(res_entry.get("task_id")):
            logger.warning(
                "Task id %s does not match reservation %s (%s); refusing reconciliation",
                task_id,
                reservation_id,
                res_entry.get("task_id"),
            )
            return False
    elif task_id is not None:
        logger.warning(
            "Task id %s provided but reservation %s has no task_id; refusing reconciliation",
            task_id,
            reservation_id,
        )
        return False

    if res_entry.get("attempt") is not None:
        if attempt is None or attempt != res_entry.get("attempt"):
            logger.warning(
                "Attempt %s does not match reservation %s (%s); refusing reconciliation",
                attempt,
                reservation_id,
                res_entry.get("attempt"),
            )
            return False

    if res_entry.get("lease_token") is not None:
        if lease_token is None or str(lease_token) != str(res_entry.get("lease_token")):
            logger.warning(
                "Lease token %s does not match reservation %s (%s); refusing reconciliation",
                lease_token,
                reservation_id,
                res_entry.get("lease_token"),
            )
            return False

    reserved_cost_in_entry = int(res_entry.get("cost_minor", 0))
    if cost_minor != reserved_cost_in_entry:
        logger.warning(
            "Cost minor %s does not match reservation %s reserved cost %s; refusing reconciliation",
            cost_minor,
            reservation_id,
            reserved_cost_in_entry,
        )
        return False

    current_status = res_entry.get("status")
    # Idempotency check: if already settled or recovered, do not adjust counters again
    if current_status != BudgetReservationStatus.ACTIVE:
        logger.info(
            "Reservation %s already in state %s; skipping duplicate reconciliation idempotently",
            reservation_id,
            current_status,
        )
        return True

    # 4. Update reservation and counters
    res_entry["reconciled_at"] = now.isoformat()

    reserved_calls = int(budget.get("reserved_tool_calls", 0))
    reserved_cost = int(budget.get("reserved_cost_minor", 0))
    budget["reserved_tool_calls"] = max(0, reserved_calls - 1)
    budget["reserved_cost_minor"] = max(0, reserved_cost - reserved_cost_in_entry)

    if success:
        res_entry["status"] = BudgetReservationStatus.RECONCILED
        res_entry["settled_as"] = "SUCCESS"
        spent_calls = int(budget.get("spent_tool_calls", 0))
        spent_cost = int(budget.get("spent_cost_minor", 0))
        budget["spent_tool_calls"] = spent_calls + 1
        budget["spent_cost_minor"] = spent_cost + reserved_cost_in_entry

        if "remaining_tool_calls" in budget:
            budget["remaining_tool_calls"] = max(0, int(budget["remaining_tool_calls"]) - 1)
        if "remaining_cost_minor" in budget:
            budget["remaining_cost_minor"] = max(
                0, int(budget["remaining_cost_minor"]) - reserved_cost_in_entry
            )
    else:
        # Not successful: distinguish known not dispatched from timeout/cancellation/uncertain
        if not dispatched or outcome == "NOT_DISPATCHED":
            # Known not dispatched: safely refund reservation without consuming remaining budget
            res_entry["status"] = BudgetReservationStatus.RECONCILED
            res_entry["settled_as"] = "NOT_DISPATCHED"
            # spent and uncertain are NOT incremented, remaining is NOT decremented
        else:
            # Call was dispatched or outcome is uncertain (timeout, cancellation, crash, error)
            # Preserve uncertain spend! Do NOT refund!
            res_entry["status"] = BudgetReservationStatus.RECONCILED
            res_entry["settled_as"] = outcome or "UNCERTAIN"
            budget["uncertain_tool_calls"] = int(budget.get("uncertain_tool_calls", 0)) + 1
            budget["uncertain_cost_minor"] = (
                int(budget.get("uncertain_cost_minor", 0)) + reserved_cost_in_entry
            )
            budget["spent_tool_calls"] = int(budget.get("spent_tool_calls", 0)) + 1
            budget["spent_cost_minor"] = (
                int(budget.get("spent_cost_minor", 0)) + reserved_cost_in_entry
            )
            if "remaining_tool_calls" in budget:
                budget["remaining_tool_calls"] = max(0, int(budget["remaining_tool_calls"]) - 1)
            if "remaining_cost_minor" in budget:
                budget["remaining_cost_minor"] = max(
                    0, int(budget["remaining_cost_minor"]) - reserved_cost_in_entry
                )

    reservations[reservation_id] = res_entry
    budget["reservations"] = reservations
    execution.budget = budget
    flag_modified(execution, "budget")
    await session.commit()
    return True


async def recover_abandoned_budget_reservations(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    attempt: int | None = None,
    recovery_reason: str = "task_recovery",
) -> list[dict[str, Any]]:
    """Recovers abandoned ACTIVE reservations for an execution with abandonment verification under locks.

    Enforces parent-first lock ordering (executions -> tasks).
    Re-evaluates current time after lock wait.
    A manual sweep verifies that a task is actually abandoned before recovering its active reservation.
    Live tasks with active leases are NOT recovered.
    Preserves uncertain provider spend: does NOT treat abandoned calls as free!
    Idempotent: skips already recovered reservations.
    Returns list of recovered reservation entries.
    """
    # 1. Lock Parent Execution FIRST
    exec_stmt = select(ExecutionModel).where(ExecutionModel.id == execution_id).with_for_update()
    exec_res = await session.execute(exec_stmt)
    execution = exec_res.scalar_one_or_none()
    if not execution or not execution.budget:
        return []

    now = datetime.now(UTC)
    budget = ensure_execution_budget_structure(execution.budget)
    reservations = dict(budget.get("reservations") or {})
    recovered: list[dict[str, Any]] = []

    parent_terminal = (
        execution.status in ("CANCELLED", "FAILED", "SUCCEEDED", "COMPLETED")
        or execution.cancelled_at is not None
        or (execution.deadline_at is not None and execution.deadline_at <= now)
    )

    for res_id, res in list(reservations.items()):
        if res.get("status") != BudgetReservationStatus.ACTIVE:
            continue

        if task_id is not None and res.get("task_id") != str(task_id):
            continue

        if attempt is not None and res.get("attempt") is not None and res.get("attempt") > attempt:
            continue

        # Verify abandonment under lock
        if parent_terminal:
            is_abandoned = True
        else:
            res_task_id_str = res.get("task_id")
            if not res_task_id_str:
                # No task associated and parent is live -> not abandoned
                continue

            res_task_uuid = uuid.UUID(res_task_id_str)
            # Lock TaskModel SECOND (parent-first order)
            task_stmt = select(TaskModel).where(TaskModel.id == res_task_uuid).with_for_update()
            task_res = await session.execute(task_stmt)
            task = task_res.scalar_one_or_none()

            task_now = datetime.now(UTC)

            if not task:
                is_abandoned = True
            elif task.status in ("CANCELLED", "FAILED", "SUCCEEDED", "COMPLETED"):
                is_abandoned = True
            elif task.status == "PENDING":
                is_abandoned = True
            elif task.status in ("CLAIMED", "RUNNING"):
                # Active status: check lease and attempt
                if task.lease_expiry is None or task.lease_expiry <= task_now:
                    is_abandoned = True
                elif res.get("lease_token") and str(task.lease_token) != res.get("lease_token"):
                    is_abandoned = True
                elif res.get("attempt") is not None and task.attempt > res.get("attempt"):
                    is_abandoned = True
                else:
                    # TASK IS LIVE AND ACTIVE!
                    # Do not recover active reservation for live worker
                    is_abandoned = False
            else:
                is_abandoned = True

        if not is_abandoned:
            logger.info("Reservation %s is held by active live task; skipping recovery", res_id)
            continue

        # Verified abandoned: recover and preserve uncertain spend
        cost_minor = int(res.get("cost_minor", 0))
        res["status"] = BudgetReservationStatus.ABANDONED_UNCERTAIN
        res["settled_as"] = "UNCERTAIN"
        res["recovered_at"] = now.isoformat()
        res["recovery_reason"] = recovery_reason
        reservations[res_id] = res
        recovered.append(res)

        budget["reserved_tool_calls"] = max(0, int(budget.get("reserved_tool_calls", 0)) - 1)
        budget["reserved_cost_minor"] = max(
            0, int(budget.get("reserved_cost_minor", 0)) - cost_minor
        )

        budget["uncertain_tool_calls"] = int(budget.get("uncertain_tool_calls", 0)) + 1
        budget["uncertain_cost_minor"] = int(budget.get("uncertain_cost_minor", 0)) + cost_minor
        budget["spent_tool_calls"] = int(budget.get("spent_tool_calls", 0)) + 1
        budget["spent_cost_minor"] = int(budget.get("spent_cost_minor", 0)) + cost_minor
        budget["remaining_tool_calls"] = max(0, int(budget.get("remaining_tool_calls", 0)) - 1)
        budget["remaining_cost_minor"] = max(
            0, int(budget.get("remaining_cost_minor", 0)) - cost_minor
        )

    # Also recover abandoned route_fallback reservations under the same parent-first lock
    rf_recovered = await _recover_route_fallback_in_locked_execution(
        session=session,
        execution=execution,
        budget=budget,
        parent_terminal=parent_terminal,
        task_id=task_id,
        attempt=attempt,
        recovery_reason=recovery_reason,
        now=now,
    )
    recovered.extend(rf_recovered)

    if recovered:
        budget["reservations"] = reservations
        execution.budget = budget
        flag_modified(execution, "budget")
        await session.commit()

    return recovered


def _recover_route_fallback_reservations_dict(
    *,
    budget: dict[str, Any],
    eligible_fn: Any,
    recovery_reason: str,
    now_iso: str,
) -> list[dict[str, Any]]:
    """Shared helper transitioning abandoned route_fallback reservations to ABANDONED_UNCERTAIN."""
    raw_rf = budget.get("route_fallback")
    if not isinstance(raw_rf, dict):
        return []
    rf = dict(raw_rf)
    rf_reservations = dict(rf.get("reservations") or {})
    recovered: list[dict[str, Any]] = []
    for res_id, res in list(rf_reservations.items()):
        if not isinstance(res, dict) or res.get("status") != BudgetReservationStatus.ACTIVE:
            continue
        if not eligible_fn(res):
            continue
        upd = dict(res)
        upd["status"] = BudgetReservationStatus.ABANDONED_UNCERTAIN
        upd["settled_as"] = "UNCERTAIN"
        upd["recovered_at"] = now_iso
        upd["recovery_reason"] = recovery_reason
        rf_reservations[res_id] = upd
        recovered.append(upd)
        rf["reserved_attempts"] = max(0, int(rf.get("reserved_attempts", 0)) - 1)
        rf["uncertain_attempts"] = int(rf.get("uncertain_attempts", 0)) + 1
        rf["spent_attempts"] = int(rf.get("spent_attempts", 0)) + 1
        rf["remaining_attempts"] = max(0, int(rf.get("remaining_attempts", 0)) - 1)
    if recovered:
        rf["reservations"] = rf_reservations
        budget["route_fallback"] = rf
    return recovered


async def _recover_route_fallback_in_locked_execution(
    *,
    session: AsyncSession,
    execution: ExecutionModel,
    budget: dict[str, Any],
    parent_terminal: bool,
    task_id: uuid.UUID | None,
    attempt: int | None,
    recovery_reason: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """Verifies task abandonment under lock before recovering route_fallback reservations."""
    raw_rf = budget.get("route_fallback")
    if not isinstance(raw_rf, dict):
        return []
    rf_reservations = raw_rf.get("reservations") or {}
    if not rf_reservations:
        return []

    abandoned_ids: set[str] = set()
    for res_id, res in rf_reservations.items():
        if not isinstance(res, dict) or res.get("status") != BudgetReservationStatus.ACTIVE:
            continue
        if task_id is not None and res.get("task_id") != str(task_id):
            continue
        res_attempt = res.get("attempt")
        if attempt is not None and isinstance(res_attempt, int) and res_attempt > attempt:
            continue
        if parent_terminal:
            abandoned_ids.add(res_id)
            continue
        res_task_id_str = res.get("task_id")
        if not res_task_id_str:
            continue
        res_task_uuid = uuid.UUID(res_task_id_str)
        task_stmt = select(TaskModel).where(TaskModel.id == res_task_uuid).with_for_update()
        task = (await session.execute(task_stmt)).scalar_one_or_none()
        task_now = datetime.now(UTC)
        if not task or task.status in ("CANCELLED", "FAILED", "SUCCEEDED", "COMPLETED", "PENDING"):
            abandoned_ids.add(res_id)
        elif task.status in ("CLAIMED", "RUNNING") and (
            task.lease_expiry is None
            or task.lease_expiry <= task_now
            or (res.get("lease_token") and str(task.lease_token) != res.get("lease_token"))
            or (isinstance(res_attempt, int) and task.attempt > res_attempt)
        ):
            abandoned_ids.add(res_id)

    if not abandoned_ids:
        return []

    # Re-key filter by presence in abandoned_ids
    rf = dict(raw_rf)
    rf_res_copy = dict(rf_reservations)
    recovered: list[dict[str, Any]] = []
    now_iso = now.isoformat()
    for res_id in abandoned_ids:
        upd = dict(rf_res_copy[res_id])
        upd["status"] = BudgetReservationStatus.ABANDONED_UNCERTAIN
        upd["settled_as"] = "UNCERTAIN"
        upd["recovered_at"] = now_iso
        upd["recovery_reason"] = recovery_reason
        rf_res_copy[res_id] = upd
        recovered.append(upd)
        rf["reserved_attempts"] = max(0, int(rf.get("reserved_attempts", 0)) - 1)
        rf["uncertain_attempts"] = int(rf.get("uncertain_attempts", 0)) + 1
        rf["spent_attempts"] = int(rf.get("spent_attempts", 0)) + 1
        rf["remaining_attempts"] = max(0, int(rf.get("remaining_attempts", 0)) - 1)
    rf["reservations"] = rf_res_copy
    budget["route_fallback"] = rf
    return recovered


def recover_abandoned_budget_reservations_in_session(
    execution: ExecutionModel,
    *,
    task_id: uuid.UUID | None = None,
    attempt: int | None = None,
    recovery_reason: str = "task_recovery",
) -> list[dict[str, Any]]:
    """Recovers abandoned ACTIVE reservations for a locked execution.

    Preserves uncertain provider spend: does NOT treat abandoned calls as free!
    Deducts cost from remaining and records in spent and uncertain counters.
    Idempotent: skips already recovered reservations.
    Returns list of recovered reservation entries.
    """
    if not execution.budget:
        return []

    budget = ensure_execution_budget_structure(execution.budget)
    reservations = dict(budget.get("reservations") or {})
    recovered: list[dict[str, Any]] = []
    now_iso = datetime.now(UTC).isoformat()

    for res_id, res in list(reservations.items()):
        if res.get("status") != BudgetReservationStatus.ACTIVE:
            continue

        # Filter by task_id if provided
        if task_id is not None and res.get("task_id") != str(task_id):
            continue

        # Filter by attempt if provided
        if attempt is not None and res.get("attempt") is not None and res.get("attempt") > attempt:
            continue

        cost_minor = int(res.get("cost_minor", 0))

        # Transition reservation
        res["status"] = BudgetReservationStatus.ABANDONED_UNCERTAIN
        res["settled_as"] = "UNCERTAIN"
        res["recovered_at"] = now_iso
        res["recovery_reason"] = recovery_reason
        reservations[res_id] = res
        recovered.append(res)

        # Release reserved counter
        budget["reserved_tool_calls"] = max(0, int(budget.get("reserved_tool_calls", 0)) - 1)
        budget["reserved_cost_minor"] = max(
            0, int(budget.get("reserved_cost_minor", 0)) - cost_minor
        )

        # Account for uncertain spend (cannot assume interrupted call was free)
        budget["uncertain_tool_calls"] = int(budget.get("uncertain_tool_calls", 0)) + 1
        budget["uncertain_cost_minor"] = int(budget.get("uncertain_cost_minor", 0)) + cost_minor
        budget["spent_tool_calls"] = int(budget.get("spent_tool_calls", 0)) + 1
        budget["spent_cost_minor"] = int(budget.get("spent_cost_minor", 0)) + cost_minor
        budget["remaining_tool_calls"] = max(0, int(budget.get("remaining_tool_calls", 0)) - 1)
        budget["remaining_cost_minor"] = max(
            0, int(budget.get("remaining_cost_minor", 0)) - cost_minor
        )

    rf_recovered = _recover_route_fallback_reservations_dict(
        budget=budget,
        eligible_fn=lambda r: (
            (task_id is None or r.get("task_id") == str(task_id))
            and (attempt is None or r.get("attempt") is None or r.get("attempt") <= attempt)
        ),
        recovery_reason=recovery_reason,
        now_iso=now_iso,
    )
    recovered.extend(rf_recovered)

    if recovered:
        budget["reservations"] = reservations
        execution.budget = budget
        flag_modified(execution, "budget")

    return recovered
