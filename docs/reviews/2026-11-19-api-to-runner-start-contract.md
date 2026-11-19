# Review, main (uncommitted), 2026-11-19

**Reviewed by**: claude-opus-4-6-thinking (author on Gemini)
**Scope**: 9 files, uncommitted
**Verdict**: Approve with nits

## Summary

This change implements and tests the authenticated API to runner start contract: job acceptance with owner scoped idempotency, serialized outbox relay through the production consumer into GraphRunnerService, identity mismatch fencing, deepcopy isolation of outbox payloads, and comprehensive integration tests covering duplicate delivery, cross owner rejection, brief version immutability, and atomic rollback. The code quality is high, the transaction boundaries are precise, and the test coverage is thorough across all stated acceptance criteria. Two minor issues and three nits are noted below.

## Minor

### 🟡 Payload `trip_id` passthrough when execution has no trip, `runner.py:340`

**Problem**: `initial_state` spreads `**payload_state` first, then overwrites canonical fields. The `trip_id` is set conditionally only when `execution_trip_id is not None` (line 352). If a future caller creates an execution without a `trip_id` but the payload contains one, the payload's `trip_id` would leak into `initial_state` without validation, because the identity mismatch check at line 227 also requires `execution.trip_id is not None` to fire.

**Why it matters**: Today `start_trip_plan` always provides `trip_id` to `accept_job`, so `execution.trip_id` is always set and this path is unreachable. However, the runner is a general purpose service, and a future caller omitting `trip_id` from the execution record would allow an attacker's payload to inject an arbitrary `trip_id` into the graph state without raising `ExecutionTripMismatchError`.

**Suggested fix**: After the conditional block at line 352, add `elif "trip_id" in initial_state: del initial_state["trip_id"]` to strip any payload supplied `trip_id` when the execution record has no trip. Alternatively, include `"trip_id": None` in the canonical fields dict literal before the conditional override.

### 🟡 `verify.md` uses `$ACCEPTANCE_DATABASE_URL` inconsistently with surrounding commands, `docs/specs/0007-trip-planner-e2e/verify.md:44`

**Problem**: Line 44 uses `TEST_DATABASE_URL=$ACCEPTANCE_DATABASE_URL` while line 45 uses `DATABASE_URL=$ACCEPTANCE_DATABASE_URL DB_SSL=false`. Both appear to reference the same acceptance database. Some commands earlier in the file also use `TEST_DATABASE_URL=$ACCEPTANCE_DATABASE_URL`. The inconsistency between `TEST_DATABASE_URL` and `DATABASE_URL` variables is documented but could confuse contributors.

**Why it matters**: A contributor running verify commands may use the wrong environment variable and hit the wrong database, or silently run against a different instance. The `DB_SSL=false` on line 45 but not line 44 adds to the ambiguity about whether SSL is required for the acceptance database.

**Suggested fix**: Standardize on a single environment variable pattern across all verify commands, and document once whether SSL override is needed. If the fixtures under `test_job_acceptance_idempotency.py` create their own engine from `settings.DATABASE_URL` (they do), document that those tests read `DATABASE_URL` directly while others read `TEST_DATABASE_URL` via conftest.

## Nits

- ⚪ `consumer.py:31`, f string in `logger.error` call uses eager interpolation rather than lazy `%s` formatting. Lines 37 and 97 have the same pattern. Surrounding code (lines 49, 155) correctly uses `%s` style. Consistency would be nice.
- ⚪ `runner.py:200-203`, the comment explaining forward declarations ("Forward declarations to carry authoritative execution identity out of the Phase 1 database transaction without holding the lock during LangGraph execution") is helpful. Consider adding type annotations to the declarations themselves for readability: `execution_owner_id: uuid.UUID | None = None` would satisfy the type checker without relying on the reader to verify that all code paths assign before use.
- ⚪ `test_api_to_runner_start_contract.py:68-73`, `_StubLambdaContext` duplicates `MockContext` from `test_graph_runner_consumer.py:39-44`. A shared fixture or conftest helper would eliminate the duplication.

## Strengths

- The `initial_state` construction at runner.py:340 correctly spreads `**payload_state` first and then overwrites with canonical fields, preventing payload state pollution of execution identity fields (`status`, `execution_id`, `owner_id`). This is a clean, minimal fix for a real security concern.
- The error taxonomy at runner.py:52 is well designed: `ExecutionIdentityMismatchError(GraphRunnerError)` with `ExecutionOwnerMismatchError(PermissionError)` and `ExecutionTripMismatchError(ValueError)` gives callers specific catch targets while preserving isinstance compatibility with both `GraphRunnerError` and the standard Python error types.
- `copy.deepcopy(start_payload)` at idempotency.py:138 prevents shared mutable state between the outbox event payload and the caller's dict, closing a subtle aliasing bug.
- The integration test `test_authenticated_api_to_runner_start_contract_end_to_end` is excellent: it exercises the full chain from authenticated HTTP POST through real outbox relay serialization, production consumer dispatch (`runner=None`), GraphRunnerService execution, to checkpoint and task verification, all without manual payload hydration or mocked graph nodes.
- The test for spoofed `planning_options` (lines 158 through 167 of the contract test) explicitly injects `budget`, `owner_id`, `brief`, and `version` overrides in the client request and then verifies they had no effect on the persisted execution budget, proving the owning policy boundary is real.

## Test coverage

Test coverage is strong. The 5 new integration tests in `test_api_to_runner_start_contract.py` cover the core contract end to end (happy path with spoofed options), duplicate acceptance and delivery idempotency with mid flight budget spend, cross owner and identity mismatch rejection, brief version immutability after acceptance, and atomic rollback with missing brief/unsupported workflow. The existing 4 tests in `test_graph_runner_consumer.py` cover consumer batch handling, malformed records, timeout, and duplicate delivery. The 9 tests in `test_job_acceptance_idempotency.py` and 4 in `test_job_surfaces.py` cover the remaining acceptance and status surface. All changed production code is exercised.
