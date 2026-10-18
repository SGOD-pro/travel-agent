"""Unit tests for LAYA decision models, canonical replay digest, and repository reuse.

Covers Slice 4 of spec 0004:
1. Canonical replay digest generation and idempotency key formatting (AC-9).
2. LayaDecisionModel schema, phase extraction, and to_envelope conversion (AC-1, AC-8).
3. LayaDecisionRepository result reuse revalidation across all 4 criteria:
   - Phase matching.
   - Context version matching.
   - Policy version matching.
   - Chosen action remaining in active eligible choices.
4. Idempotent persistence and query methods.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from contracts.decision import (
    DecisionAction,
    DecisionEnvelope,
    DecisionPhase,
    DecisionStatus,
    DecisionType,
    LayaDecisionRequest,
)
from functions.orchestrator.decision.models import LayaDecisionModel
from functions.orchestrator.decision.policy import LayaPolicyValidator
from functions.orchestrator.decision.repository import (
    LayaDecisionRepository,
    compute_canonical_replay_digest,
)

# ---------------------------------------------------------------------------
# 1. Canonical Replay Digest & Idempotency Key Tests (AC-9)
# ---------------------------------------------------------------------------


def test_compute_canonical_replay_digest_structure_and_length() -> None:
    """Verifies that digest produces 64 hex characters and formatted idempotency key."""
    exec_id = uuid.uuid4()
    digest, idem_key = compute_canonical_replay_digest(
        execution_id=exec_id,
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=1,
        policy_version="laya-policy-v1",
        question_version="v1.0",
        eligible_actions=[
            DecisionAction.DISPATCH_EVIDENCE_SEARCH,
            DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
        ],
        sanitized_context={"destination": "Kolkata", "category": "lodging"},
    )

    assert len(digest) == 64
    assert idem_key.startswith(f"{exec_id}:laya_decision:SEARCH_COMPLETION:")
    assert idem_key.endswith(digest[:16])


def test_compute_canonical_replay_digest_dict_key_ordering_invariance() -> None:
    """Verifies that dict key insertion order does not change the canonical digest."""
    exec_id = uuid.uuid4()
    ctx_1 = {"category": "lodging", "destination": "Goa", "count": 3}
    ctx_2 = {"count": 3, "destination": "Goa", "category": "lodging"}

    digest_1, key_1 = compute_canonical_replay_digest(
        execution_id=exec_id,
        phase=DecisionPhase.BROWSER_ELIGIBILITY,
        context_version=2,
        policy_version="laya-policy-v1",
        question_version="v1.0",
        eligible_actions=[DecisionAction.DISPATCH_BROWSER_ESCALATION],
        sanitized_context=ctx_1,
    )

    digest_2, key_2 = compute_canonical_replay_digest(
        execution_id=exec_id,
        phase=DecisionPhase.BROWSER_ELIGIBILITY,
        context_version=2,
        policy_version="laya-policy-v1",
        question_version="v1.0",
        eligible_actions=[DecisionAction.DISPATCH_BROWSER_ESCALATION],
        sanitized_context=ctx_2,
    )

    assert digest_1 == digest_2
    assert key_1 == key_2


def test_compute_canonical_replay_digest_action_ordering_invariance() -> None:
    """Verifies that eligible action order is canonicalized so choice order does not affect digest."""
    exec_id = uuid.uuid4()
    actions_a = [
        DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
        DecisionAction.DISPATCH_EVIDENCE_SEARCH,
        DecisionAction.DISPATCH_BROWSER_ESCALATION,
    ]
    actions_b = [
        DecisionAction.DISPATCH_BROWSER_ESCALATION,
        DecisionAction.DISPATCH_EVIDENCE_SEARCH,
        DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
    ]

    digest_a, key_a = compute_canonical_replay_digest(
        execution_id=exec_id,
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=1,
        policy_version="laya-policy-v1",
        question_version="v1.0",
        eligible_actions=actions_a,
        sanitized_context={"city": "Bengaluru"},
    )

    digest_b, key_b = compute_canonical_replay_digest(
        execution_id=exec_id,
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=1,
        policy_version="laya-policy-v1",
        question_version="v1.0",
        eligible_actions=actions_b,
        sanitized_context={"city": "Bengaluru"},
    )

    assert digest_a == digest_b
    assert key_a == key_b


def test_compute_canonical_replay_digest_string_context_handling() -> None:
    """Verifies string sanitized context is hashed reliably."""
    exec_id = uuid.uuid4()
    digest, key = compute_canonical_replay_digest(
        execution_id=exec_id,
        phase=DecisionPhase.BROWSER_ELIGIBILITY,
        context_version=1,
        policy_version="laya-policy-v1",
        question_version="v1.0",
        eligible_actions=[DecisionAction.DISPATCH_BROWSER_ESCALATION],
        sanitized_context="Plain sanitized text summary for lodging",
    )
    assert len(digest) == 64
    assert key.startswith(f"{exec_id}:laya_decision:BROWSER_ELIGIBILITY:")


def test_compute_canonical_replay_digest_sensitivity_to_inputs() -> None:
    """Verifies that changes in execution_id, phase, context_version, policy_version, or actions produce distinct digests."""
    base_exec = uuid.uuid4()
    base_actions = [DecisionAction.DISPATCH_EVIDENCE_SEARCH]
    base_ctx = {"destination": "Jaipur"}

    digest_base, _ = compute_canonical_replay_digest(
        execution_id=base_exec,
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=1,
        policy_version="laya-policy-v1",
        question_version="v1.0",
        eligible_actions=base_actions,
        sanitized_context=base_ctx,
    )

    # Different execution id
    digest_diff_exec, _ = compute_canonical_replay_digest(
        execution_id=uuid.uuid4(),
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=1,
        policy_version="laya-policy-v1",
        question_version="v1.0",
        eligible_actions=base_actions,
        sanitized_context=base_ctx,
    )
    assert digest_diff_exec != digest_base

    # Different phase
    digest_diff_phase, _ = compute_canonical_replay_digest(
        execution_id=base_exec,
        phase=DecisionPhase.BROWSER_ELIGIBILITY,
        context_version=1,
        policy_version="laya-policy-v1",
        question_version="v1.0",
        eligible_actions=base_actions,
        sanitized_context=base_ctx,
    )
    assert digest_diff_phase != digest_base

    # Different context version
    digest_diff_ver, _ = compute_canonical_replay_digest(
        execution_id=base_exec,
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=2,
        policy_version="laya-policy-v1",
        question_version="v1.0",
        eligible_actions=base_actions,
        sanitized_context=base_ctx,
    )
    assert digest_diff_ver != digest_base

    # Different policy version
    digest_diff_policy, _ = compute_canonical_replay_digest(
        execution_id=base_exec,
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=1,
        policy_version="laya-policy-v2",
        question_version="v1.0",
        eligible_actions=base_actions,
        sanitized_context=base_ctx,
    )
    assert digest_diff_policy != digest_base

    # Different actions
    digest_diff_actions, _ = compute_canonical_replay_digest(
        execution_id=base_exec,
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=1,
        policy_version="laya-policy-v1",
        question_version="v1.0",
        eligible_actions=[
            DecisionAction.DISPATCH_EVIDENCE_SEARCH,
            DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
        ],
        sanitized_context=base_ctx,
    )
    assert digest_diff_actions != digest_base


# ---------------------------------------------------------------------------
# 2. LayaDecisionModel Unit Tests (AC-1, AC-8)
# ---------------------------------------------------------------------------


def test_laya_decision_model_to_envelope_conversion() -> None:
    """Verifies complete database model to domain DecisionEnvelope mapping."""
    dec_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    now = datetime.now(UTC)

    model = LayaDecisionModel(
        id=uuid.uuid4(),
        decision_id=dec_id,
        execution_id=exec_id,
        trip_id=trip_id,
        context_version=1,
        idempotency_key=f"{exec_id}:laya_decision:SEARCH_COMPLETION:abcdef0123456789",
        decision_type="EVIDENCE_ROUTING",
        allowed_choices=["DISPATCH_BROWSER_ESCALATION", "PROCEED_DETERMINISTIC_EVALUATION"],
        chosen_action="DISPATCH_BROWSER_ESCALATION",
        confidence=Decimal("0.850"),
        answer_confidence=Decimal("0.820"),
        model_version="typed-decisions",
        policy_version="laya-policy-v1",
        status="VALIDATED",
        rationale="Policy validator accepted choice with answer_confidence 0.820",
        latency_ms=185,
        request_digest="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        created_at=now,
    )

    envelope = model.to_envelope()

    assert envelope.decision_id == dec_id
    assert envelope.decision_type == DecisionType.EVIDENCE_ROUTING
    assert envelope.phase == DecisionPhase.SEARCH_COMPLETION
    assert envelope.execution_id == exec_id
    assert envelope.trip_id == trip_id
    assert envelope.allowed_choice == DecisionAction.DISPATCH_BROWSER_ESCALATION
    assert envelope.status == DecisionStatus.VALIDATED
    assert envelope.confidence == 0.850
    assert envelope.answer_confidence == 0.820
    assert envelope.model_version == "typed-decisions"
    assert envelope.policy_version == "laya-policy-v1"
    assert envelope.rationale == "Policy validator accepted choice with answer_confidence 0.820"
    assert envelope.latency_ms == 185
    assert envelope.request_digest == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    assert envelope.created_at == now.isoformat()


def test_laya_decision_model_extract_phase_logic() -> None:
    """Verifies phase extraction from idempotency keys and fallback behaviour."""
    exec_id = uuid.uuid4()

    # Valid phases
    for phase in [
        DecisionPhase.SEARCH_COMPLETION,
        DecisionPhase.BROWSER_ELIGIBILITY,
        DecisionPhase.EVALUATION,
    ]:
        model = LayaDecisionModel(
            decision_id=uuid.uuid4(),
            execution_id=exec_id,
            idempotency_key=f"{exec_id}:laya_decision:{phase.value}:1234567812345678",
            decision_type="EVIDENCE_ROUTING",
            allowed_choices=[],
            chosen_action="DISPATCH_EVIDENCE_SEARCH",
            policy_version="laya-policy-v1",
            status="VALIDATED",
            latency_ms=10,
            request_digest="abc",
        )
        assert model.extract_phase() == phase

    # Malformed key falls back cleanly to BROWSER_ELIGIBILITY
    malformed = LayaDecisionModel(
        decision_id=uuid.uuid4(),
        execution_id=exec_id,
        idempotency_key="malformed_key",
        decision_type="EVIDENCE_ROUTING",
        allowed_choices=[],
        chosen_action="DISPATCH_EVIDENCE_SEARCH",
        policy_version="laya-policy-v1",
        status="VALIDATED",
        latency_ms=10,
        request_digest="abc",
    )
    assert malformed.extract_phase() == DecisionPhase.BROWSER_ELIGIBILITY


def test_laya_decision_model_none_confidences() -> None:
    """Verifies that None confidences convert cleanly without error."""
    exec_id = uuid.uuid4()
    model = LayaDecisionModel(
        decision_id=uuid.uuid4(),
        execution_id=exec_id,
        idempotency_key=f"{exec_id}:laya_decision:SEARCH_COMPLETION:1234567812345678",
        decision_type="EVIDENCE_ROUTING",
        allowed_choices=[],
        chosen_action="DISPATCH_EVIDENCE_SEARCH",
        confidence=None,
        answer_confidence=None,
        policy_version="laya-policy-v1",
        status="FALLBACK_ERROR",
        latency_ms=0,
        request_digest="abc",
    )
    envelope = model.to_envelope()
    assert envelope.confidence is None
    assert envelope.answer_confidence is None


# ---------------------------------------------------------------------------
# 3. LayaDecisionRepository Result Reuse Revalidation Tests (AC-9)
# ---------------------------------------------------------------------------


def _build_test_model(
    *,
    execution_id: uuid.UUID,
    phase: DecisionPhase = DecisionPhase.SEARCH_COMPLETION,
    context_version: int = 1,
    policy_version: str = LayaPolicyValidator.POLICY_VERSION,
    chosen_action: DecisionAction = DecisionAction.DISPATCH_BROWSER_ESCALATION,
    idempotency_key: str | None = None,
) -> LayaDecisionModel:
    dec_id = uuid.uuid4()
    if idempotency_key is None:
        idempotency_key = f"{execution_id}:laya_decision:{phase.value}:fedcba9876543210"

    return LayaDecisionModel(
        id=uuid.uuid4(),
        decision_id=dec_id,
        execution_id=execution_id,
        trip_id=None,
        context_version=context_version,
        idempotency_key=idempotency_key,
        decision_type="EVIDENCE_ROUTING",
        allowed_choices=[chosen_action.value],
        chosen_action=chosen_action.value,
        confidence=Decimal("0.850"),
        answer_confidence=Decimal("0.850"),
        model_version="typed-decisions",
        policy_version=policy_version,
        status="VALIDATED",
        rationale="Valid test rationale",
        latency_ms=100,
        request_digest="digest_test_123",
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_repository_find_reusable_decision_successful_reuse() -> None:
    """Verifies that an existing decision meeting all 4 criteria is reused cleanly."""
    exec_id = uuid.uuid4()
    active_phase = DecisionPhase.SEARCH_COMPLETION
    idem_key = f"{exec_id}:laya_decision:SEARCH_COMPLETION:abcdef0123456789"

    model = _build_test_model(
        execution_id=exec_id,
        phase=active_phase,
        context_version=1,
        policy_version=LayaPolicyValidator.POLICY_VERSION,
        chosen_action=DecisionAction.DISPATCH_BROWSER_ESCALATION,
        idempotency_key=idem_key,
    )

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = model
    session.execute.return_value = mock_result

    repo = LayaDecisionRepository(session=session)

    req = LayaDecisionRequest(
        decision_id=uuid.uuid4(),
        execution_id=exec_id,
        trip_id=None,
        context_version=1,
        idempotency_key=idem_key,
        policy_derived_eligible_choices=[
            DecisionAction.DISPATCH_BROWSER_ESCALATION,
            DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
        ],
        budget_remaining=3,
        context={"destination": "Kolkata"},
    )

    reused = await repo.find_reusable_decision(request=req, active_phase=active_phase)

    assert reused is not None
    assert reused.decision_id == model.decision_id
    assert reused.allowed_choice == DecisionAction.DISPATCH_BROWSER_ESCALATION
    assert reused.phase == DecisionPhase.SEARCH_COMPLETION


@pytest.mark.asyncio
async def test_repository_find_reusable_decision_missing_record() -> None:
    """Verifies that if no decision exists for the idempotency key, None is returned."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    repo = LayaDecisionRepository(session=session)

    exec_id = uuid.uuid4()
    req = LayaDecisionRequest(
        decision_id=uuid.uuid4(),
        execution_id=exec_id,
        trip_id=None,
        context_version=1,
        idempotency_key=f"{exec_id}:laya_decision:SEARCH_COMPLETION:notfound",
        policy_derived_eligible_choices=[DecisionAction.DISPATCH_EVIDENCE_SEARCH],
        budget_remaining=3,
        context={},
    )

    reused = await repo.find_reusable_decision(request=req, active_phase=DecisionPhase.SEARCH_COMPLETION)
    assert reused is None


@pytest.mark.asyncio
async def test_repository_find_reusable_decision_phase_mismatch_bypasses() -> None:
    """Revalidation Check 1: Phase mismatch must bypass result reuse and trigger fresh inference."""
    exec_id = uuid.uuid4()
    # Stored record is from BROWSER_ELIGIBILITY phase
    model = _build_test_model(
        execution_id=exec_id,
        phase=DecisionPhase.BROWSER_ELIGIBILITY,
        context_version=1,
        policy_version=LayaPolicyValidator.POLICY_VERSION,
        chosen_action=DecisionAction.DISPATCH_BROWSER_ESCALATION,
    )

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = model
    session.execute.return_value = mock_result

    repo = LayaDecisionRepository(session=session)

    req = LayaDecisionRequest(
        decision_id=uuid.uuid4(),
        execution_id=exec_id,
        trip_id=None,
        context_version=1,
        idempotency_key=model.idempotency_key,
        policy_derived_eligible_choices=[DecisionAction.DISPATCH_BROWSER_ESCALATION],
        budget_remaining=3,
        context={},
    )

    # Calling with active_phase=SEARCH_COMPLETION
    reused = await repo.find_reusable_decision(request=req, active_phase=DecisionPhase.SEARCH_COMPLETION)
    assert reused is None


@pytest.mark.asyncio
async def test_repository_find_reusable_decision_context_version_mismatch_bypasses() -> None:
    """Revalidation Check 2: Context version mismatch must bypass result reuse."""
    exec_id = uuid.uuid4()
    # Stored record is version 1
    model = _build_test_model(
        execution_id=exec_id,
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=1,
        policy_version=LayaPolicyValidator.POLICY_VERSION,
        chosen_action=DecisionAction.DISPATCH_EVIDENCE_SEARCH,
    )

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = model
    session.execute.return_value = mock_result

    repo = LayaDecisionRepository(session=session)

    # Active request has advanced to context_version 2
    req = LayaDecisionRequest(
        decision_id=uuid.uuid4(),
        execution_id=exec_id,
        trip_id=None,
        context_version=2,
        idempotency_key=model.idempotency_key,
        policy_derived_eligible_choices=[DecisionAction.DISPATCH_EVIDENCE_SEARCH],
        budget_remaining=3,
        context={},
    )

    reused = await repo.find_reusable_decision(request=req, active_phase=DecisionPhase.SEARCH_COMPLETION)
    assert reused is None


@pytest.mark.asyncio
async def test_repository_find_reusable_decision_policy_version_mismatch_bypasses() -> None:
    """Revalidation Check 3: Policy version mismatch must bypass result reuse."""
    exec_id = uuid.uuid4()
    # Stored record was created under legacy policy version
    model = _build_test_model(
        execution_id=exec_id,
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=1,
        policy_version="laya-policy-v0-legacy",
        chosen_action=DecisionAction.DISPATCH_EVIDENCE_SEARCH,
    )

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = model
    session.execute.return_value = mock_result

    repo = LayaDecisionRepository(session=session)

    req = LayaDecisionRequest(
        decision_id=uuid.uuid4(),
        execution_id=exec_id,
        trip_id=None,
        context_version=1,
        idempotency_key=model.idempotency_key,
        policy_derived_eligible_choices=[DecisionAction.DISPATCH_EVIDENCE_SEARCH],
        budget_remaining=3,
        context={},
    )

    reused = await repo.find_reusable_decision(request=req, active_phase=DecisionPhase.SEARCH_COMPLETION)
    assert reused is None


@pytest.mark.asyncio
async def test_repository_find_reusable_decision_chosen_action_no_longer_eligible_bypasses() -> None:
    """Revalidation Check 4: If previously chosen action is now exhausted/ineligible, bypass reuse."""
    exec_id = uuid.uuid4()
    # Stored record chose browser escalation
    model = _build_test_model(
        execution_id=exec_id,
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=1,
        policy_version=LayaPolicyValidator.POLICY_VERSION,
        chosen_action=DecisionAction.DISPATCH_BROWSER_ESCALATION,
    )

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = model
    session.execute.return_value = mock_result

    repo = LayaDecisionRepository(session=session)

    # Active request now has exhausted browser budget; only deterministic evaluation is eligible
    req = LayaDecisionRequest(
        decision_id=uuid.uuid4(),
        execution_id=exec_id,
        trip_id=None,
        context_version=1,
        idempotency_key=model.idempotency_key,
        policy_derived_eligible_choices=[DecisionAction.PROCEED_DETERMINISTIC_EVALUATION],
        budget_remaining=0,
        context={},
    )

    reused = await repo.find_reusable_decision(request=req, active_phase=DecisionPhase.SEARCH_COMPLETION)
    assert reused is None


@pytest.mark.asyncio
async def test_repository_find_reusable_decision_invalid_action_value_bypasses() -> None:
    """Edge Case: If stored action string is not a valid DecisionAction enum, bypass cleanly."""
    exec_id = uuid.uuid4()
    model = _build_test_model(
        execution_id=exec_id,
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=1,
        policy_version=LayaPolicyValidator.POLICY_VERSION,
    )
    model.chosen_action = "OBSOLETE_INVALID_ACTION"

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = model
    session.execute.return_value = mock_result

    repo = LayaDecisionRepository(session=session)

    req = LayaDecisionRequest(
        decision_id=uuid.uuid4(),
        execution_id=exec_id,
        trip_id=None,
        context_version=1,
        idempotency_key=model.idempotency_key,
        policy_derived_eligible_choices=[DecisionAction.DISPATCH_EVIDENCE_SEARCH],
        budget_remaining=3,
        context={},
    )

    reused = await repo.find_reusable_decision(request=req, active_phase=DecisionPhase.SEARCH_COMPLETION)
    assert reused is None


# ---------------------------------------------------------------------------
# 4. LayaDecisionRepository Persistence & Query Tests (AC-8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_persist_decision_and_lookup() -> None:
    """Verifies that persist_decision executes pg_insert with on_conflict_do_nothing and loads row."""
    exec_id = uuid.uuid4()
    dec_id = uuid.uuid4()
    now_iso = datetime.now(UTC).isoformat()

    envelope = DecisionEnvelope(
        decision_id=dec_id,
        decision_type=DecisionType.EVIDENCE_ROUTING,
        phase=DecisionPhase.SEARCH_COMPLETION,
        execution_id=exec_id,
        trip_id=None,
        idempotency_key=f"{exec_id}:laya_decision:SEARCH_COMPLETION:1234567812345678",
        allowed_choice=DecisionAction.DISPATCH_BROWSER_ESCALATION,
        status=DecisionStatus.VALIDATED,
        confidence=0.880,
        answer_confidence=0.850,
        model_version="typed-decisions",
        policy_version="laya-policy-v1",
        rationale="Accepted by policy",
        latency_ms=120,
        request_digest="digest_abc_123",
        created_at=now_iso,
    )

    persisted_model = _build_test_model(
        execution_id=exec_id,
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=1,
        chosen_action=DecisionAction.DISPATCH_BROWSER_ESCALATION,
        idempotency_key=envelope.idempotency_key,
    )

    session = AsyncMock()
    mock_lookup_result = MagicMock()
    mock_lookup_result.scalar_one.return_value = persisted_model
    session.execute.return_value = mock_lookup_result

    repo = LayaDecisionRepository(session=session)

    res = await repo.persist_decision(
        envelope=envelope,
        context_version=1,
        allowed_choices=[
            DecisionAction.DISPATCH_BROWSER_ESCALATION,
            DecisionAction.PROCEED_DETERMINISTIC_EVALUATION,
        ],
    )

    assert session.execute.call_count == 2  # 1 insert + 1 lookup
    session.flush.assert_called_once()
    assert res == persisted_model


@pytest.mark.asyncio
async def test_repository_get_by_decision_id() -> None:
    """Verifies retrieving decision by decision_id."""
    dec_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    model = _build_test_model(execution_id=exec_id)

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = model
    session.execute.return_value = mock_result

    repo = LayaDecisionRepository(session=session)
    res = await repo.get_by_decision_id(decision_id=dec_id)

    assert res == model
    session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_repository_get_decisions_for_execution() -> None:
    """Verifies retrieving chronological decisions for an execution."""
    exec_id = uuid.uuid4()
    model_1 = _build_test_model(execution_id=exec_id)
    model_2 = _build_test_model(execution_id=exec_id)

    session = AsyncMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [model_1, model_2]
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    session.execute.return_value = mock_result

    repo = LayaDecisionRepository(session=session)
    res = await repo.get_decisions_for_execution(execution_id=exec_id)

    assert len(res) == 2
    assert res[0] == model_1
    assert res[1] == model_2


def test_laya_decision_model_phase_property() -> None:
    """Verifies that model.phase property accesses the extracted DecisionPhase."""
    exec_id = uuid.uuid4()
    model = LayaDecisionModel(
        decision_id=uuid.uuid4(),
        execution_id=exec_id,
        idempotency_key=f"{exec_id}:laya_decision:SEARCH_COMPLETION:fedcba0987654321",
        decision_type="EVIDENCE_ROUTING",
        allowed_choices=[],
        chosen_action="DISPATCH_EVIDENCE_SEARCH",
        policy_version="laya-policy-v1",
        status="VALIDATED",
        latency_ms=15,
        request_digest="abc",
    )
    assert model.phase == DecisionPhase.SEARCH_COMPLETION


@pytest.mark.asyncio
async def test_repository_persist_decision_truncates_long_rationale() -> None:
    """Verifies that persist_decision defensively bounds rationale to 1000 characters."""
    exec_id = uuid.uuid4()
    dec_id = uuid.uuid4()
    long_rationale = "x" * 1500

    envelope = DecisionEnvelope.model_construct(
        decision_id=dec_id,
        decision_type=DecisionType.EVIDENCE_ROUTING,
        phase=DecisionPhase.SEARCH_COMPLETION,
        execution_id=exec_id,
        trip_id=None,
        idempotency_key=f"{exec_id}:laya_decision:SEARCH_COMPLETION:1234567812345678",
        allowed_choice=DecisionAction.DISPATCH_BROWSER_ESCALATION,
        status=DecisionStatus.VALIDATED,
        confidence=0.880,
        answer_confidence=0.850,
        model_version="typed-decisions",
        policy_version="laya-policy-v1",
        rationale=long_rationale,
        latency_ms=120,
        request_digest="digest_abc_123",
        created_at=datetime.now(UTC).isoformat(),
    )

    persisted_model = _build_test_model(
        execution_id=exec_id,
        phase=DecisionPhase.SEARCH_COMPLETION,
        context_version=1,
        chosen_action=DecisionAction.DISPATCH_BROWSER_ESCALATION,
        idempotency_key=envelope.idempotency_key,
    )

    session = AsyncMock()
    mock_lookup_result = MagicMock()
    mock_lookup_result.scalar_one.return_value = persisted_model
    session.execute.return_value = mock_lookup_result

    repo = LayaDecisionRepository(session=session)
    await repo.persist_decision(envelope=envelope, context_version=1)

    insert_stmt = session.execute.call_args_list[0][0][0]
    # Verify the parameters passed to insert statement have rationale sliced to 1000 chars
    compiled_params = insert_stmt.compile().params
    assert len(compiled_params["rationale"]) == 1000
    assert compiled_params["rationale"] == "x" * 1000
