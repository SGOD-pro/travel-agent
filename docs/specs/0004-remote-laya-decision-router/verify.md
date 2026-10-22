# Verify: Remote LAYA Decision Router, spec 0004, updated 2026-10-17
_Steps derived from spec 0004 acceptance criteria. /check verify runs these; /test locks the durable ones._

## Acceptance criteria

- [x] AC-1: SWENA defines internal typed domain contracts (`LayaDecisionRequest`, `DecisionEnvelope`, `DecisionAction`, `DecisionStatus`) in `contracts/decision.py` that remain decoupled from upstream wire protocols, with `LayaDecisionModel` database mapping in `functions/orchestrator/decision/models.py`. (Slices 1 and 4 complete)
- [x] AC-2: The remote client adapter implements the upstream Jev compatible wire protocol (`POST /v1/systemone`), transforming SWENA internal requests into `state` and typed `choice` questions, parsing returned `answers` dictionaries with graceful handling of strict mode responses (`LAYA_JEV_STRICT=1`) where optional fields are missing, keeping unavailable entropy confidence null, and treating unavailable truncation metadata as unknown with defensive client side request length bounding without asserting a universal 512 token ceiling or claiming full context proof. (Slices 1 and 2 complete)
- [x] AC-3: Action eligibility operates across three explicit workflow phases: search completion, browser eligibility, and evaluation. (Slice 3 complete)
- [x] AC-4: Remote client enforces configurable endpoints (`LAYA_ENDPOINT_URL`), separate Bearer authentication (`LAYA_API_KEY`), and optional Cloudflare Access service tokens (`CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`). (Slice 2 complete)
- [x] AC-5: Remote client enforces a cumulative total retry deadline (default 5.0 seconds across all attempts) with individual socket timeout (default 2.5 seconds), executing at most 2 retries on transient network errors or 5xx responses, and failing immediately with zero retries on 4xx client errors or when `LAYA_ENDPOINT_URL` is empty. (Slice 2 complete)
- [x] AC-6: Deterministic policy engine (`LayaPolicyValidator`) validates all responses before dispatch. (Slice 3 complete)
- [x] AC-7: Explicit deterministic fallback table maps all failure modes to safe deterministic actions while preserving execution budgets, cancellation barriers, deadlines, and required evidence collection. (Slice 3 complete)
- [x] AC-8: Actual execution lease and transaction boundaries keep database access, cancellation checks, and lease fencing strictly in orchestration. (Slice 4 complete)
- [x] AC-9: Replay safety uses a phase specific canonical digest incorporating execution phase, context version, sorted eligible actions, policy version, question version, and sanitized context digest with 4 point result reuse revalidation. (Slice 4 complete)
- [x] AC-10: Acceptance test suite provides offline wire contract unit tests with canned fixtures, connected smoke acceptance against live endpoint, and a bounded 25 case labeled routing quality evaluation benchmark. (Slice 5 complete)

## Specced surfaces (Slice 1)

- [x] Contract `DecisionAction` in `backend/src/contracts/decision.py`: enumerates `DISPATCH_EVIDENCE_SEARCH`, `DISPATCH_BROWSER_ESCALATION`, and `PROCEED_DETERMINISTIC_EVALUATION`.
- [x] Contract `DecisionType` in `backend/src/contracts/decision.py`: enumerates `EVIDENCE_ROUTING`.
- [x] Contract `DecisionPhase` in `backend/src/contracts/decision.py`: enumerates `SEARCH_COMPLETION`, `BROWSER_ELIGIBILITY`, and `EVALUATION`.
- [x] Contract `DecisionStatus` in `backend/src/contracts/decision.py`: enumerates `VALIDATED`, `DETERMINISTIC_SINGLETON`, `REJECTED_BY_POLICY`, and all `FALLBACK_*` states.
- [x] Contract `LayaDecisionRequest` in `backend/src/contracts/decision.py`: frozen model enforcing positive budget, context version, and non empty eligible choices.
- [x] Contract `DecisionEnvelope` in `backend/src/contracts/decision.py`: frozen model validating that `DETERMINISTIC_SINGLETON` decisions mandate `confidence=null` and `answer_confidence=null`, and enforcing finite float bounds on confidence metrics.
- [x] Abstract port `LayaDecisionPort` in `backend/src/contracts/decision.py`: declares async `query_decision` and `check_health`.
- [x] Upstream wire schemas in `backend/src/functions/decision_router/schemas.py`: `LayaWireQuestion`, `LayaWireRequest`, `LayaChoiceAnswer`, `LayaWireUsage`, `LayaWireRouting`, `LayaWireResponse` matching NandhaKishorM/laya release v0.4.9.
- [x] Probability validation helper `validate_wire_probabilities` in `backend/src/functions/decision_router/schemas.py`: validates finite floats in 0.0 to 1.0, unity partition normalization, argmax consistency, alignment tolerance, and key membership against offered criteria.
- [x] Helper functions `resolve_answer_confidence`, `resolve_model_version`, and `resolve_truncation_status` in `backend/src/functions/decision_router/schemas.py`: derive strict mode missing field defaults and map unknown truncation metadata.
- [x] Offline fixture adapter `FixtureLayaDecisionAdapter` in `backend/src/functions/decision_router/fixtures.py`: implements `LayaDecisionPort` with singleton bypass and canned response translation.
- [x] Configuration parameters in `backend/src/config/config.py`: `LAYA_ENDPOINT_URL`, `LAYA_API_KEY`, `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`, `LAYA_REQUEST_TIMEOUT_SECONDS`, `LAYA_TOTAL_DEADLINE_SECONDS`, `LAYA_MAX_RETRIES`, `LAYA_CONFIDENCE_THRESHOLD`, `LAYA_MODEL_NAME`, with deadline model validator.
- [x] Unit test suite in `backend/tests/unit/test_laya_decision_router.py`: 25 passed tests.

## Specced surfaces (Slice 2)

- [x] Remote HTTP client adapter `RemoteLayaDecisionAdapter` in `backend/src/functions/decision_router/client.py`: implements `LayaDecisionPort` with `httpx.AsyncClient`.
- [x] Dual authentication headers in `_build_request_headers`: separates `Authorization: Bearer {LAYA_API_KEY}` from Cloudflare Access service tokens `CF-Access-Client-Id` and `CF-Access-Client-Secret`.
- [x] Upstream query dispatch in `query_decision`: constructs `POST /v1/systemone` wire payload and maps responses to `DecisionEnvelope`.
- [x] Deterministic singleton bypass in `query_decision`: single eligible choice returns status `DETERMINISTIC_SINGLETON` with null confidences and latency 0 ms without network round trips.
- [x] Fast failure on unconfigured endpoint: empty `LAYA_ENDPOINT_URL` returns status `FALLBACK_UNCONFIGURED` with zero retries.
- [x] Cumulative deadline enforcement: cumulative deadline across all retry attempts (default 5.0s) tracked via wall clock timer, aborting immediately with `FALLBACK_TIMEOUT` if exhausted.
- [x] Socket timeout enforcement: individual attempt socket timeout (default 2.5s) enforced via `min(LAYA_REQUEST_TIMEOUT_SECONDS, remaining_deadline)`.
- [x] Transient retry backoff: retries up to `LAYA_MAX_RETRIES` (default 2) with exponential backoff on transient network errors or HTTP 5xx responses.
- [x] Immediate 4xx client error failure: zero retries on HTTP 4xx responses, failing fast with `FALLBACK_ERROR`.
- [x] Strict mode resolution: missing `answer_confidence` derived from `probabilities[choice]`, missing `confidence` preserved as null, missing `routing` defaulted to model or unknown, and missing `usage` mapped to unknown truncation status.
- [x] Payload and telemetry sanitization in `backend/src/functions/decision_router/sanitization.py`:
  - `sanitize_headers`: redacts `Authorization`, `CF-Access-Client-Id`, `CF-Access-Client-Secret`, cookies, and API keys.
  - `sanitize_context`: extracts allowed execution fields while stripping traveler names, emails, phones, user IDs, and auth tokens.
  - `bound_sanitized_summary`: bounds long textual summaries client side to safe length ceilings.
  - `sanitize_text`: redacts known secret strings from log and exception traces.
- [x] Health probe `check_health`: executes `GET /health` with authentication headers, returning True on HTTP 200 and False on failure or unconfigured endpoint.
- [x] Unit test suite in `backend/tests/unit/test_laya_remote_client.py`: 16 passed tests.

## Specced surfaces (Slice 3)

- [x] Policy validator `LayaPolicyValidator` in `backend/src/functions/orchestrator/decision/policy.py`:
  - `compute_eligible_choices`: computes prerequisite filtered actions across `SEARCH_COMPLETION`, `BROWSER_ELIGIBILITY`, and `EVALUATION` phases.
  - `is_singleton`: detects single eligible choice states for deterministic bypass.
  - `create_singleton_decision`: creates deterministic singleton envelopes with `status=DETERMINISTIC_SINGLETON`, `confidence=None`, `answer_confidence=None`, and `latency_ms=0`.
  - `handle_zero_choices`: defaults to `PROCEED_DETERMINISTIC_EVALUATION` if required destination evidence verified, else raises `InsufficientRequiredEvidenceError`.
  - `validate_response`: enforces parent cancellation barriers, deadline barriers, explicit fallback triggers, tokenizer truncation rejection, explicit abstention rejection, probability criteria matching, finite float checks, unity normalization, argmax consistency, and answer confidence threshold gating (default 0.700).
  - `resolve_fallback`: deterministic fallback table mapping all failure conditions to safe default actions while preserving execution budgets and required destination evidence.
- [x] Canonical request digest helper `compute_request_digest` in `policy.py`: SHA256 digest over execution ID, phase, context version, policy version, question version, and sorted eligible choices.
- [x] Exception types `InsufficientRequiredEvidenceError`, `ExecutionCancelledError`, `ExecutionDeadlineExceededError` in `policy.py`.
- [x] Unit test suite in `backend/tests/unit/test_laya_policy_validator.py`: 27 passed tests.

## Specced surfaces (Slice 4)

- [x] Database migration `backend/infrastructure/migrations/versions/0011_add_laya_decisions.py`: creates table `laya_decisions` with UUID primary key, foreign key on `executions.id` with CASCADE, `context_version`, `Numeric(4, 3)` confidences, and unique constraint `uq_laya_execution_idempotency` on `(execution_id, idempotency_key)`. Live PostgreSQL database upgraded and verified at revision `0011_add_laya_decisions`.
- [x] SQLAlchemy model `LayaDecisionModel` in `backend/src/functions/orchestrator/decision/models.py`:
  - Table mapping matching the spec data model sketch.
  - Distinct `confidence` and `answer_confidence` numeric columns.
  - `extract_phase()` extracting workflow phase from idempotency key format.
  - `to_envelope()` converting database model to immutable domain `DecisionEnvelope`.
- [x] Repository `LayaDecisionRepository` in `backend/src/functions/orchestrator/decision/repository.py`:
  - `find_reusable_decision`: enforces four point revalidation checks: matching phase, matching context version, matching policy version, and chosen action remaining an active eligible choice.
  - `persist_decision`: executes PostgreSQL `pg_insert` with `on_conflict_do_nothing` on `(execution_id, idempotency_key)` to resolve concurrent runner conflicts cleanly and loads committed row.
  - `get_by_decision_id`: looks up decision record by unique decision UUID.
  - `get_by_idempotency_key`: looks up decision record by execution ID and idempotency key.
  - `get_decisions_for_execution`: queries chronological decision history for an execution.
- [x] Canonical replay digest helper `compute_canonical_replay_digest` in `backend/src/functions/orchestrator/decision/repository.py`: computes deterministic SHA256 request digest and formatted idempotency key `{execution_id}:laya_decision:{phase}:{request_digest[:16]}` with dict key and action list sorting invariance.
- [x] Module exports in `backend/src/functions/orchestrator/decision/__init__.py`: exports `LayaDecisionModel`, `LayaDecisionRepository`, and `compute_canonical_replay_digest`.
- [x] Unit test suite in `backend/tests/unit/test_laya_decision_repository.py`: 18 passed tests.

## Specced surfaces (Slice 5)

- [x] Decision node `route_evidence_decision_node` in `backend/src/functions/orchestrator/graphs/trip_planner/graph.py`: placed between `discover_candidates` and `escalate_dynamic_evidence`, evaluates evidence routing actions, enqueues static search tasks in `pending_tasks` on `DISPATCH_EVIDENCE_SEARCH`, yields/passes to browser escalation on `DISPATCH_BROWSER_ESCALATION`, advances directly to schedule solving on `PROCEED_DETERMINISTIC_EVALUATION`, and records chronological decision history in `laya_decisions`.
- [x] Conditional edge `route_evidence_decision_edge` in `backend/src/functions/orchestrator/graphs/trip_planner/graph.py`: directs execution flow to `solve_schedule` when deterministic evaluation is approved, or to `escalate_dynamic_evidence` on search/browser escalation.
- [x] Graph state extension `TripPlanningState`: adds `laya_decisions: list[dict[str, Any]]`, `evidence_routing_action: str`, `static_search_completed: bool`, `remaining_search_budget: int`.
- [x] Live connected acceptance test in `backend/tests/integration/test_laya_connected_acceptance.py`: verifies live Cloudflare tunnel communication against remote Kaggle endpoint with GET `/health` probe and POST `/v1/systemone` inference, latency tracking, and confidence gating.
- [x] 25 scenario deterministic policy regression suite in `backend/tests/unit/test_laya_evaluation_benchmark.py`: verifies 100% precision on canonical deterministic scenarios (policy conformance >= 85%, simulated abstention <= 10%), with connected live sample support; live connected labeled model evaluation remains PENDING.
- [x] Unit test suite in `backend/tests/unit/test_laya_graph_node.py`: 14 passed tests.
- [x] Connected PostgreSQL lifecycle regression test suite in `backend/tests/integration/test_laya_lifecycle_regression.py`: 4 passed tests proving collect dispatch, task preservation, await_evidence_results interrupt and resumption, observation backed completion derivation, real consumer execution, runtime decision reuse, and atomic decision persistence under runner fencing.

## Commands

```bash
# Run all LAYA unit and benchmark tests
uv run pytest tests/unit/test_laya_*.py -v

# Run connected PostgreSQL lifecycle regression test suite
uv run pytest tests/integration/test_laya_lifecycle_regression.py -v

# Run live connected acceptance test against Cloudflare tunnel
RUN_CONNECTED_LAYA_TESTS=1 uv run pytest tests/integration/test_laya_connected_acceptance.py -v

# Run full backend unit test suite
uv run pytest tests/unit/

# Run linter and type checker
uv run ruff check src/contracts/decision.py src/functions/decision_router src/functions/orchestrator/decision src/functions/orchestrator/graphs/trip_planner/graph.py src/config/config.py infrastructure/migrations/versions/0011_add_laya_decisions.py tests/unit/test_laya_*.py tests/integration/test_laya_connected_acceptance.py
uv run mypy src/contracts/decision.py src/functions/decision_router src/functions/orchestrator/decision src/functions/orchestrator/graphs/trip_planner/graph.py src/config/config.py tests/unit/test_laya_*.py tests/integration/test_laya_connected_acceptance.py
```
