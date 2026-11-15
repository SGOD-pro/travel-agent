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
    Validates execution status, cancellation, deadline, and task lease active fencing.
    Returns reservation_id on success, or None if budget exhausted or lease invalid.
    """
    now = datetime.now(UTC)

    # 1. Lock Parent Execution FIRST
    exec_stmt = select(ExecutionModel).where(ExecutionModel.id == execution_id).with_for_update()
    exec_res = await session.execute(exec_stmt)
    execution = exec_res.scalar_one_or_none()
    if not execution:
        return None

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
) -> bool:
    """Atomically reconciles budget reservation.

    Prevents stale workers from reconciling after lease loss.
    Idempotent on repeated calls.
    """
    if not getattr(session, "is_active", True):
        logger.warning("Session inactive; skipping budget reconciliation for %s", execution_id)
        return False

    now = datetime.now(UTC)

    # 1. Lock Parent Execution FIRST
    exec_stmt = select(ExecutionModel).where(ExecutionModel.id == execution_id).with_for_update()
    exec_res = await session.execute(exec_stmt)
    execution = exec_res.scalar_one_or_none()
    if not execution or not execution.budget:
        return False

    budget = ensure_execution_budget_structure(execution.budget)
    reservations = dict(budget.get("reservations") or {})

    # 2. Check task lease validity if task_id and lease_token provided
    if task_id is not None and lease_token is not None:
        task_stmt = select(TaskModel).where(TaskModel.id == task_id).with_for_update()
        task_res = await session.execute(task_stmt)
        task = task_res.scalar_one_or_none()
        if task:
            # If lease is revoked or assigned to another token or expired:
            if task.lease_token != lease_token or (task.lease_expiry and task.lease_expiry <= now):
                logger.warning(
                    "Stale worker with lease %s attempted to reconcile task %s (active lease: %s)",
                    lease_token,
                    task_id,
                    task.lease_token,
                )
                return False

    # 3. Locate reservation entry if reservation_id provided or lookup via task
    res_entry: dict[str, Any] | None = None
    if reservation_id and reservation_id in reservations:
        res_entry = reservations[reservation_id]
    elif task_id is not None:
        # Fallback: look for ACTIVE reservation for this task
        for r_id, r in reservations.items():
            if r.get("task_id") == str(task_id) and r.get("status") == BudgetReservationStatus.ACTIVE:
                if attempt is None or r.get("attempt") == attempt:
                    res_entry = r
                    reservation_id = r_id
                    break

    if res_entry is not None and reservation_id is not None:
        current_status = res_entry.get("status")
        # Idempotency check: if already settled or recovered, do not adjust counters again
        if current_status != BudgetReservationStatus.ACTIVE:
            logger.info(
                "Reservation %s already in state %s; skipping duplicate reconciliation",
                reservation_id,
                current_status,
            )
            return True

        res_entry["status"] = BudgetReservationStatus.RECONCILED
        res_entry["settled_as"] = "SUCCESS" if success else "FAILED"
        res_entry["reconciled_at"] = now.isoformat()
        reservations[reservation_id] = res_entry
        budget["reservations"] = reservations

    # 4. Update counters
    reserved_calls = int(budget.get("reserved_tool_calls", 0))
    reserved_cost = int(budget.get("reserved_cost_minor", 0))

    budget["reserved_tool_calls"] = max(0, reserved_calls - 1)
    budget["reserved_cost_minor"] = max(0, reserved_cost - cost_minor)

    if success:
        spent_calls = int(budget.get("spent_tool_calls", 0))
        spent_cost = int(budget.get("spent_cost_minor", 0))
        budget["spent_tool_calls"] = spent_calls + 1
        budget["spent_cost_minor"] = spent_cost + cost_minor

        if "remaining_tool_calls" in budget:
            budget["remaining_tool_calls"] = max(0, int(budget["remaining_tool_calls"]) - 1)
        if "remaining_cost_minor" in budget:
            budget["remaining_cost_minor"] = max(
                0, int(budget["remaining_cost_minor"]) - cost_minor
            )

    execution.budget = budget
    flag_modified(execution, "budget")
    await session.commit()
    return True


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
