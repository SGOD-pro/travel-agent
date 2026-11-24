# Review, main, 2026-11-25

**Reviewed by**: review subagent (claude) (author on prior session)
**Scope**: 9 files, uncommitted
**Verdict**: Approve with nits

## Summary
This slice implements durable three attempt budget reservations for Valhalla `/route` fallback, aligns cluster return leg geometry with solver hub pricing (`[Abbey Falls -> Madikeri Hub -> Bengaluru]`), and updates Ooty routing diagnostics and replay classification. Following the prior review, the Blocker in `service.py` null matrix recovery, the Major in `run_hitl_pipeline.py` replay classification, and six Minor findings in `service.py`, `budget.py`, and `graph.py` were resolved with targeted regression tests. All 24 unit and integration tests and `ruff check` pass, and all 5 files under `src/` pass `mypy`, leaving only a `mypy` narrowing issue in `scripts/run_hitl_pipeline.py` and minor items.

## Minor
### 🟡 `mypy` union errors in `classify_pipeline_providers`, `backend/scripts/run_hitl_pipeline.py:61`
**Problem**: `classify_pipeline_providers` calls `diag.get("llm")`, `diag.get("weather")`, and `diag.get("search")` twice inside ternary `isinstance(..., dict)` expressions (lines 61, 70, 84). Under `strict = true`, `mypy` does not narrow repeated `.get(...)` calls and fails with 4 `[union-attr]` errors at lines 62, 73, 77, and 85.
**Why it matters**: Running `uv run mypy` against `scripts/run_hitl_pipeline.py` exits with code 1 even though the 5 files under `src/` pass cleanly.
**Suggested fix**: Bind each `diag.get(...)` result to a local variable before the `isinstance(..., dict)` check so `mypy` narrows the dictionary type.

### 🟡 Direct calls without `task_id` skip lease checks, `backend/src/functions/orchestrator/execution/budget.py:112`
**Problem**: `reconcile_route_fallback_attempt` now validates `task_id`, `attempt`, and `lease_token` bindings against the stored reservation, while `reserve_route_fallback_attempt` still permits `task_id=None` and `lease_token=None` when called directly outside `execute_task`.
**Why it matters**: Callers that omit `task_id` bypass lease fencing at reservation time, though `execute_task` always supplies `task_id` and `lease_token` in worker execution.
**Suggested fix**: Require `task_id` and `lease_token` on production reservation calls or enforce a separate explicit test flag when running without a task row.

### 🟡 Punctuation hyphens in comments and docstrings, `backend/src/functions/route_optimizer/service.py:260`
**Problem**: Several comments and docstrings in `budget.py`, `service.py`, `test_valhalla_bounded_fallback_regressions.py`, and `test_valhalla_fallback_durable_cap.py` still contain hyphenated compound words (such as `execution-wide`, `parent-first`, `In-memory`, `three-attempt`, `short-trip`, `mid-task`, and `mid-flight`).
**Why it matters**: Conflicts with the repository prose and comment convention against punctuation dashes or hyphens.
**Suggested fix**: Replace remaining hyphenated compound words in comments and docstrings with unhyphenated words.

## Nits
- ⚪ `backend/src/functions/orchestrator/execution/budget.py:767`, `_recover_route_fallback_in_locked_execution` duplicates the reservation transition and counter update loop already factored into `_recover_route_fallback_reservations_dict` at line 679.
- ⚪ `backend/src/functions/route_optimizer/valhalla.py:359`, `resp.json()` runs before `on_after_attempt`, so a malformed 200 JSON body skips immediate reconciliation (the reservation remains `ACTIVE` until recovery sweep, preserving the cap).
- ⚪ `backend/src/functions/route_optimizer/service.py:685`, `safe_reason` hardcodes `1500 km` and `150 km` thresholds for any error code `154` response rather than reporting the message from `exc.response_body` when available.

## Strengths
- Null matrix cell recovery in `service.py:1070` now propagates `LeaseFencingError`, `ExecutionDeadlineExceededError`, `ParentCancelledError`, `ParentTerminalError`, `ExecutionNotFoundError`, and `ValhallaRoutingError` (`429` or `154`) instead of masking them as `INFEASIBLE`, backed by both unit and integration assertions.
- Durable reservation commits before outbound `/route` calls in `ValhallaClient._send_request` accurately count retries, timeouts, and hydration probes while leaving normal short trip routing uncharged and preserving uncertain attempts across crashes and redelivery.
- Cluster return leg stitching in `_hydrate_leg_geometries_and_revalidate_schedule` prehydrates `[Hub, Local, Hub]` in one `/route` call, reuses cached segments, applies `exclude_polygons`, and revalidates closures and return deadlines.
- `classify_pipeline_providers` in `scripts/run_hitl_pipeline.py` inspects runtime diagnostics before falling back to settings and requires all providers to be nonlive for `"fixture"` classification, with unit coverage across live, fixture, and mixed combinations.

## Test coverage
All 16 unit tests in `test_valhalla_bounded_fallback.py` and `test_valhalla_bounded_fallback_regressions.py` (including 4 new regression tests covering null matrix error propagation, `TRAIN` first mode selection plus `hard_constraints`, advisory `"154"` false positive avoidance plus partial fixture publication, and runtime provider classification), all 4 PostgreSQL integration tests in `test_valhalla_fallback_durable_cap.py` (with `ROUTING_UNAVAILABLE` asserted on cap exhaustion), `ruff check`, and `mypy` on the 5 `src/` files passed during this review; `mypy` on `scripts/run_hitl_pipeline.py` failed with 4 `[union-attr]` errors.
