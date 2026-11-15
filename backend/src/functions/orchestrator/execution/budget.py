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

    if recovered:
        budget["reservations"] = reservations
        execution.budget = budget
        flag_modified(execution, "budget")
        await session.commit()

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

    if recovered:
        budget["reservations"] = reservations
        execution.budget = budget
        flag_modified(execution, "budget")

    return recovered
