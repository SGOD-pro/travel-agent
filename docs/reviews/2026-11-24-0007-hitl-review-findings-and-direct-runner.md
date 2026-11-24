# Review, main, 2026-11-24

**Reviewed by**: Staff Software Engineer (author on claude-3-5-sonnet)
**Scope**: 13 files, uncommitted
**Verdict**: Blocked

## Summary
The change extracts a shared `TripPlanningService` to allow direct execution of the human in the loop planning pipeline without ASGI or synthetic HTTP tokens, introduces safe loopback database connection guards, and expands concurrency and lifecycle testing in `test_hitl_pipeline.py`. While the service extraction and lock ordering improvements are well conceived, dropping `planning_options` during the `start_planning` refactor silently breaks idempotency conflict detection, causing an immediate failure in `test_api_trip_plans_endpoint_idempotency_integration`. Furthermore, `question.status = "SUPERSEDED"` is rolled back on stale trip version errors, and `test_hitl_pipeline.py` introduces 19 strict typing errors under mypy.

## Blockers
### 🔴 Idempotency conflict bypass when differing planning options are submitted, `backend/src/functions/api/routes/trips.py:307` and `backend/src/functions/api/trip_planning_service.py:137`
**Problem**: When refactoring `start_trip_plan` from `trips.py` into `TripPlanningService.start_planning`, `request.planning_options` was dropped. The service hardcodes `payload_dict = {"trip_id": str(trip_id), "workflow": workflow}` without taking or hashing `planning_options`.
**Why it matters**: This breaks the idempotency contract required by spec 0007 and causes an immediate test regression: `tests/integration/test_job_acceptance_idempotency.py::test_api_trip_plans_endpoint_idempotency_integration` fails with `AssertionError: assert 202 == 409`. When clients send duplicate requests with the same idempotency key but conflicting planning options, the service returns `202 Accepted` with a cached job instead of rejecting with `409 Conflict`.
**Suggested fix**: Update `TripPlanningService.start_planning` to accept `planning_options: dict[str, Any] | None = None` (or generic `payload: dict[str, Any] | None = None`), include `planning_options` in `payload_dict`, and pass `request.planning_options` from `start_trip_plan` in `trips.py`.

## Major
### 🟠 Ephemeral question status mutation rolled back on stale trip version error, `backend/src/functions/orchestrator/hitl/service.py:267`
**Problem**: In `TravelerAnswerService.submit_answer`, when a stale trip version is detected, `question.status` is set to `QuestionStatus.SUPERSEDED.value` immediately before raising `StaleTripVersionError`.
**Why it matters**: Raising `StaleTripVersionError` aborts the transaction in route handlers and test callers, rolling back the database session. The mutation to `SUPERSEDED` is discarded and never committed, leaving the question in status `PENDING` (as verified by `test_hitl_pipeline.py:1145`). If `SUPERSEDED` status must be durable, it must be committed separately; otherwise the mutation is misleading dead code.
**Suggested fix**: If the question status should become `SUPERSEDED`, perform and commit the update in an independent transaction before raising, or remove the assignment and document that stale version rejections leave the pending question unmutated.

### 🟠 Strict typing violations in integration test suite, `backend/tests/integration/test_hitl_pipeline.py:101`
**Problem**: Running `mypy` against `test_hitl_pipeline.py` fails with 19 errors under strict mode (`strict = true` in `pyproject.toml`). Test coroutines and inner helpers (`_submit`, `_holder`, `_submitter`) lack type annotations.
**Why it matters**: Pre merge CI gates configured with strict mypy checks will reject the branch. The rest of the project adheres to strict typing across tests and production code.
**Suggested fix**: Annotate all test functions with `-> None` and provide explicit parameter and return types for inner coroutines.

## Minor
### 🟡 Reimplemented database scheme normalization in runner script, `backend/scripts/run_hitl_pipeline.py:96`
**Problem**: `normalize_asyncpg_url` is reimplemented in `run_hitl_pipeline.py` instead of reusing connection utilities from `acceptance_database.py`.
**Why it matters**: Duplicating URL parsing logic across scripts and test fixtures risks behavioral divergence when database options change.
**Suggested fix**: Move `normalize_asyncpg_url` into `acceptance_database.py` and import it.

### 🟡 Deferred local imports inside endpoint request handlers, `backend/src/functions/api/routes/jobs.py:304` and `backend/src/functions/api/routes/trips.py:303`
**Problem**: `from functions.api.trip_planning_service import TripPlanningService` is imported inside handler functions rather than at module level.
**Why it matters**: There are no circular dependencies between `trip_planning_service.py` and the route modules. Local imports inside request handlers add unnecessary overhead and violate codebase style conventions.
**Suggested fix**: Move the imports to the top of `jobs.py` and `trips.py`.

## Nits
- ⚪ `backend/scripts/hitl_pipeline_pause_direct_demo.json:25`, file is missing a trailing newline.
- ⚪ `backend/tests/integration/test_hitl_pipeline.py:803`, subprocess invocation uses relative `cwd="."` rather than anchoring to the backend directory via `Path(__file__)`.

## Strengths
- Clean extraction of `TripPlanningService` enabling direct Python pipeline invocation without synthetic JWTs or HTTP loopback overhead.
- Excellent concurrency safety in `TravelerAnswerService` with canonical row lock ordering (`TravelerQuestionModel` then `ExecutionModel` then `TripModel`) and post lock deadline rechecks.
- True subprocess isolation and synchronized overlapping concurrency tests in `test_hitl_pipeline.py`.

## Test coverage
- `tests/integration/test_hitl_pipeline.py` provides thorough coverage of human in the loop question presentation, answer validation, version bumping, and resume lifecycle across 12 assertions.
- Existing regression test `tests/integration/test_job_acceptance_idempotency.py::test_api_trip_plans_endpoint_idempotency_integration` failed due to the dropped `planning_options` payload in `TripPlanningService.start_planning`.
- Strict typing coverage in `test_hitl_pipeline.py` requires annotation fixes to pass `mypy --strict`.
