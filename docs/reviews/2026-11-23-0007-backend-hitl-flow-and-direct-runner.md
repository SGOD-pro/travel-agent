# Review, uncommitted, 2026-11-23

**Reviewed by**: Claude 3.5 Sonnet (author on Claude 3.5 Sonnet)
**Scope**: 19 files, uncommitted
**Verdict**: Approve with nits

## Summary
This change implements the human in the loop planning flow and direct runner for spec 0007. It introduces draft brief ingestion, question persistence, clean WAITING_USER execution pausing, lease release, and atomic resume staging through TravelerAnswerService. The implementation exhibits rigorous transactional boundaries, reliable optimistic and row locking concurrency control, and thorough integration test coverage across live PostgreSQL. The few identified items are minor resilience improvements and code deduplication.

## Minor
### 🟡 Idempotent timeline event recording on question pause, `backend/src/functions/orchestrator/execution/runner.py:888`
**Problem**: In GraphRunnerService._execute_graph, q_row insertion is guarded by checking if existing_q is None, but record_job_event for QUESTION_ASKED is called outside this conditional block.
**Why it matters**: If graph execution re-evaluates or replays while in WAITING_USER, duplicate QUESTION_ASKED timeline events are recorded in job_events.
**Suggested fix**: Indent the record_job_event call inside the existing_q is None block so that timeline events are recorded exactly once per question.

### 🟡 Bypass of await_user_input_node on answer resumption, `backend/src/functions/orchestrator/graphs/trip_planner/graph.py:243`
**Problem**: TravelerAnswerService.submit_answer calls graph.aupdate_state with as_node=TripPlanningNode.PARSE_BRIEF and status="RUNNING". Because parse_brief_routing_edge checks if state status is WAITING_USER, setting status to RUNNING causes LangGraph to select DISCOVER_CANDIDATES as the next node. Consequently, await_user_input_node is bypassed.
**Why it matters**: While the resumed execution proceeds cleanly, await_user_input_node and its state transition to current_step="user_input_applied" remain unexecuted dead logic.
**Suggested fix**: Either route directly to DISCOVER_CANDIDATES intentionally and remove the redundant await_user_input_node, or invoke aupdate_state targeting the current graph node position so that await_user_input_node executes its planned transition.

### 🟡 Missing question execution validation against route job parameter, `backend/src/functions/api/routes/jobs.py:305`
**Problem**: The POST /api/v1/jobs/{job_id}/responses endpoint authenticates that current_user owns job_id and TravelerAnswerService verifies that caller_id owns question_id. However, neither layer verifies that question.execution_id matches the job_id supplied in the URL path.
**Why it matters**: A caller possessing multiple jobs could post a response to job A with a question ID belonging to job B. The service would successfully update job B while the request was sent to job A's URL.
**Suggested fix**: Validate in submit_answer or the API route that question.execution_id == job_id, returning HTTP 404 or HTTP 400 if they do not match.

### 🟡 Blocking terminal input in asynchronous runner, `backend/scripts/run_hitl_pipeline.py:475`
**Problem**: The interactive prompt in run_pipeline uses the standard synchronous input() function inside an async coroutine. Additionally, it does not check whether sys.stdin is an interactive terminal.
**Why it matters**: Calling input() blocks the main thread OS file descriptor, freezing the entire asyncio event loop and preventing LocalOutboxRelay from polling. Running without --non-interactive in headless or non-interactive environments raises unhandled EOFError.
**Suggested fix**: Wrap interactive terminal reading in await asyncio.to_thread(input, ...) and check sys.stdin.isatty(), falling back to default arguments when running in non-interactive streams.

### 🟡 Early connection factory instantiation before database refusal check, `backend/scripts/run_hitl_pipeline.py:58`
**Problem**: The module calls db_module.get_session_factory() at top level import time on line 58. The safety function enforce_safe_database is only called inside run_pipeline on line 314.
**Why it matters**: If an unapproved database connection string is configured in the environment, connection pool initialization occurs before the guard executes.
**Suggested fix**: Invoke enforce_safe_database at top level before initializing engine and session factory instances.

## Nits
- ⚪ `backend/src/contracts/question.py:53`, JobResponseSubmissionRequest in routes/jobs.py duplicates QuestionAnswerRequest from contracts/question.py and can be imported directly.
- ⚪ `backend/infrastructure/migrations/versions/0016_add_traveler_questions.py:52`, validation_rules column declares Python default=dict without server_default=sa.text("'{}'::jsonb"), which could fail raw SQL inserts.
- ⚪ `backend/scripts/run_hitl_pipeline.py:326`, LocalOutboxRelay is instantiated and started with a temporary user ID before inspecting resume_job_id, requiring runtime mutation of relay._target_owner_id.

## Strengths
- Clean separation between loose initial input via TripBriefDraft and strict invariants in TripBrief, ensuring downstream planning logic never handles incomplete contracts.
- Robust concurrency control in TravelerAnswerService with explicit row locking, ownership verification, stale context checks, and conflict detection.
- Transactional coupling of LangGraph state checkpoints, outbox events, and question status updates, guaranteeing zero orphaned resumes or split state upon crashes.

## Test coverage
The integration test suite in backend/tests/integration/test_hitl_pipeline.py covers all 10 essential assertions against real PostgreSQL PostGIS instances, including incomplete brief pausing, valid resumption, invalid date validation, idempotent duplicate replay, concurrent conflicting answer rejection, tenant ownership mismatch, stale context version rejection, cancellation barrier, process restart resumption, and complete brief bypass. Unit test coverage across unaffected areas remains preserved.
