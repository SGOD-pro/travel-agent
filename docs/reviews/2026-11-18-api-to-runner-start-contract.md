# Review, main (uncommitted), 2026-11-18

**Reviewed by**: claude-opus-4-6-thinking (author on Gemini)
**Scope**: 9 files, uncommitted
**Verdict**: Approve with nits

## Summary

This change wires the authenticated `POST /api/v1/trips/{trip_id}/plans` acceptance endpoint to the execution engine so that the outbox carries a durable start payload with the server resolved brief snapshot, budget, and identity rather than the HTTP response stub. It also hardens the consumer wire format parser for dual format envelopes (`event_id`/`id`, `payload`/`payload_ref`) and adds identity validation fencing inside `GraphRunnerService.handle_execution_start`. The overall quality is high: the security and correctness invariants (owner isolation, budget sovereignty, brief snapshot immutability, atomic rollback, duplicate delivery safety) are well engineered and thoroughly tested. The findings below are all minor or cosmetic.

## Minor

### 🟡 `payload_state` spread can let start payload keys overwrite canonical state fields, `runner.py:326`
**Problem**: `initial_state` is built by spreading `payload_state` first, then overwriting `status`, `current_step`, `accepted_result_ids`, `task_outputs`, `execution_id`, and `owner_id`. However the spread happens before these overrides, meaning any key in the payload that collides with a field NOT in the override list (for example `errors`, `pending_tasks`, `current_step` is overridden but `evidence_routing_action` or `static_search_completed` are not) would silently inject into graph initial state. Today `start_payload` is constructed by trusted server code in `trips.py:327` and contains only `trip_id`, `owner_id`, `version`, `brief`, `workflow`, and `context_version`. The risk is low because the only source of truth for `start_payload` is server side. But if a future change adds a field or if a different workflow populates payload differently, the merge order can silently inject unexpected state.
**Why it matters**: Defensive state construction avoids surprising graph behavior if the payload schema evolves.
**Suggested fix**: Explicitly pick only the expected keys from `payload_state` (`trip_id`, `owner_id`, `version`, `brief`, `workflow`, `budget`) instead of a blanket spread, or place the spread after all canonical defaults so the canonical values always win.

### 🟡 Identity checks in `runner.py:203` raise unguarded `PermissionError` and `ValueError`, `runner.py:208,218`
**Problem**: The new owner_id and trip_id mismatch checks raise raw `PermissionError` and `ValueError` respectively. These exceptions are not subclasses of `GraphRunnerError` or `ExecutionEngineError`, so they will not be caught by any error handling expecting the execution engine exception hierarchy. In the consumer, `process_record` does not catch them either, so they bubble up to `_process_one` and cause a `batchItemFailures` entry, which is correct for SQS retry semantics. However the raw `PermissionError` and `ValueError` types make it ambiguous whether these are expected fencing rejections or unexpected bugs when reading logs.
**Why it matters**: Log triage becomes harder when expected security rejections log the same way as unexpected bugs. Using the established exception hierarchy would let callers distinguish fencing rejections from unexpected errors.
**Suggested fix**: Consider raising `ExecutionNotFoundError` or a new `ExecutionFencingError(GraphRunnerError)` for these identity mismatches so they integrate with the existing error taxonomy and can be caught or logged at the right level if needed.

### 🟡 `execution_budget` passed to `accept_job` skips validation against arbitrary client input, `trips.py:325`
**Problem**: `execution_budget` is computed from `ensure_execution_budget_structure(collector_budget)` where `collector_budget` comes from `QueryPlanningService.calculate_collector_budget(planned_queries)` and `planned_queries` comes from `QueryPlanningService.plan_destination_queries(validated_brief)`. The brief is loaded from the persisted `TripVersionModel`, not from client input. The client `planning_options` dict from `StartPlanRequest` is included in the idempotency `payload_dict` for hash computation but never merged into `start_payload` or `execution_budget`. This is correct and prevents the client from influencing spending or identity. However, the `planning_options` dict is included in the idempotency hash via `request.model_dump(mode="json")` at line 339, meaning two requests with different `planning_options` but the same `Idempotency-Key` would produce a 409 conflict (correct). But there is no documented contract about what `planning_options` is for or whether it has any effect on the system. If it is entirely ignored, consider documenting or logging that explicitly to avoid confusion.
**Why it matters**: A client sending `planning_options` with budget overrides might believe they took effect. An explicit log message or response field indicating "planning_options are not applied" would prevent false assumptions.
**Suggested fix**: Add a note in the `StartPlanRequest` docstring or in the endpoint's response clarifying that `planning_options` is reserved for future use and currently has no effect on budget or execution behavior.

### 🟡 Consumer defaults `workflow` to `"TRAVEL"` from payload dict when both body and payload are missing it, `consumer.py:49`
**Problem**: Line 49 chains `body.get("workflow") or payload_dict.get("workflow") or "TRAVEL"`. If neither the top level body nor the nested payload contains `workflow`, the default is `"TRAVEL"`. This is a reasonable fallback for the current system, but it means a malformed message missing `workflow` will silently succeed with `"TRAVEL"` semantics rather than failing loudly. This is consistent with the pre change behavior (which also defaulted to `"TRAVEL"`), so no regression is introduced.
**Why it matters**: If a second workflow type is added later, a missing `workflow` field silently routing to `"TRAVEL"` could mask integration bugs.
**Suggested fix**: Consider logging a warning when `workflow` is not found in either location and the fallback is used, so production debugging is easier.

## Nits

- ⚪ `idempotency.py:136`: `dict(start_payload)` creates a shallow copy. The brief snapshot inside is a nested dict. If anything downstream mutated the outbox payload dict, the mutation would propagate back to `start_payload`. Since the outbox payload is persisted as JSONB (deep copied on insert), this is safe in practice, but a note or `copy.deepcopy` would make intent clearer.
- ⚪ `consumer.py:47`: `payload_dict = raw_payload if isinstance(raw_payload, dict) else {}` silently swallows non dict payloads (e.g., a string `payload_ref`). The old code would have used the string `payload_ref` directly. If a legacy message had a string URI as `payload_ref`, this change would silently drop it. If legacy `payload_ref` values were always dicts, this is fine.
- ⚪ `runner.py:188-189`: The type annotations `execution_owner_id: uuid.UUID` and `execution_trip_id: uuid.UUID | None` are forward declarations used to carry values across the `async with` boundary. This is a correct pattern but slightly unusual. A comment explaining why they are pre declared (to outlive the session scope) would help future readers.
- ⚪ `test_api_to_runner_start_contract.py:316`: The bare `except Exception as e:` on the `TripBrief.model_validate` call in `trips.py:316` is broader than necessary. `ValidationError` from Pydantic would be more precise, though the HTTP 400 outcome is the same.
- ⚪ `verify.md`: The new verification entries include hardcoded `postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/acceptance_db` connection strings rather than referencing `$ACCEPTANCE_DATABASE_URL` or `$TEST_DATABASE_URL` consistently. The existing entries in the same file use `$ACCEPTANCE_DATABASE_URL`. Aligning would avoid confusion.

## Strengths

- Excellent brief snapshot immutability design: the accepted v1 brief is persisted in the outbox at acceptance time, so any subsequent brief mutation (v1 to v2) is invisible to the execution pipeline. The dedicated test `test_brief_update_after_acceptance_preserves_accepted_version_snapshot` rigorously proves this invariant end to end through relay and consumer.
- Budget sovereignty is correctly enforced: `QueryPlanningService.calculate_collector_budget` and `ensure_execution_budget_structure` compute the budget from the server resolved brief, completely ignoring client supplied `planning_options`. The test explicitly sends spoofed budget overrides and verifies they have no effect.
- The new `test_api_to_runner_start_contract.py` is an unusually thorough integration suite: it exercises the complete acceptance to runner pipeline through real HTTP, real JWT auth, real outbox relay serialization, and real consumer parsing without manual payload hydration, which is exactly the right level of integration fidelity for a contract this critical.
- The consumer wire format parsing gracefully handles both canonical (`event_id`, `payload`) and legacy (`id`, `payload_ref`) envelope shapes, ensuring backward compatibility during relay format evolution.

## Test coverage

All new production logic (start payload construction, budget initialization, identity validation, wire format dual parsing) is covered by the new `test_api_to_runner_start_contract.py` integration suite. The three adjusted test files (`test_job_acceptance_idempotency.py`, `test_job_surfaces.py`, `test_graph_runner_consumer.py`) correctly add `TripVersionModel` fixtures and update mock targets to match the production import path change. Coverage is thorough across the happy path, duplicate delivery, cross owner rejection, identity mismatch fencing, brief version immutability, atomic rollback, missing brief, and unsupported workflow scenarios.
