# 0001. Orchestrator execution engine, transactional outbox, and reconciliation

**Date**: 2026-10-07
**Status**: Accepted

## Summary

This specification establishes the durable execution engine, transactional outbox, and checkpoint reconciliation layer for SWENA asynchronous workflows. It resolves the exact transaction boundaries, fenced mutation predicates, queue message claiming semantics, scheduled relay and recovery loops, and replay safe LangGraph checkpoint reconciliation. All persistence adheres strictly to the canonical tables in TECHNICAL_SPEC.md, DATABASE.md, and API.md, using PostgreSQL as the single source of truth.

## Context

The SWENA travel intelligence platform executes long running, multi step workflows across corridor discovery, route optimization, and itinerary synthesis. Client requests submit briefs and receive accepted job identifiers that map directly to underlying execution records. These background tasks take seconds to minutes and must operate reliably without lost tasks, ghost records, or orphaned executions.

Without explicit transaction boundaries, conflict safe idempotency, and fenced mutation predicates, concurrent workers can claim identical tasks, creating race conditions and redundant external API spend. Network disconnections or worker container terminations leave jobs stuck in running states indefinitely. Furthermore, writing task state to the database while attempting immediate external queue publication creates dual write inconsistencies whenever network interruptions occur.

This specification closes all transaction boundaries, defines the exact SQL mutation predicates with optimistic fencing, details queue message handling, bounds relay and recovery scheduling using supported AWS EventBridge schedules, and establishes replay safe reconciliation between PostgreSQL tables and LangGraph thread checkpoints.

## Module ownership boundaries

Persistence ownership is strictly partitioned between modules per DATABASE.md:

1. **API module ownership** (`backend/src/functions/api/`):
   - Owns HTTP request validation, authentication token verification, and client response generation.
   - Owns the `api_idempotency` table and its repository (`backend/src/functions/api/idempotency.py`).
   - Owns creating the initial execution record and the initial execution start event during client request ingress.
   - Never accesses internal task leasing, worker results, or LangGraph checkpoint tables.

2. **Orchestrator module ownership** (`backend/src/functions/orchestrator/execution/`):
   - Owns `executions`, `tasks`, `worker_results`, `outbox_events`, `completion_receipts`, and `job_events`.
   - Owns the task dispatching, worker leasing, fenced completion, outbox relay, lease recovery, and LangGraph checkpoint reconciliation.
   - Zero imports from API repository code or API service implementations.

## Requirements

**User stories**:
- As an API client, I want to submit travel planning requests with an idempotency key and receive a durable execution identifier so that concurrent or retried network requests never trigger duplicate paid executions.
- As a traveler or frontend application, I want to cancel a running trip execution so that background workers cease further task dispatch and the execution reaches an immutable terminal state.
- As a backend system operator, I want worker crashes, lease expirations, and unapplied checkpoints to recover automatically through scheduled recovery loops without manual database intervention.

**Acceptance criteria**:
- **AC-1**: Submitting a creation request with an existing idempotency key and identical input hash returns the original response without creating a second execution record.
- **AC-2**: Submitting a creation request with an existing idempotency key but a conflicting input hash returns an HTTP 409 conflict error.
- **AC-3**: Two concurrent requests arriving at the identical millisecond with identical idempotency keys resolve deterministically: exactly one commits the insert while the concurrent request waits and returns the committed response without a unique constraint error.
- **AC-4**: All execution queries, status checks, cancellations, and task operations require explicit owner verification, returning HTTP 404 or denying access if the record belongs to another user.
- **AC-5**: Enqueuing tasks writes task records and corresponding `outbox_events` rows within the same database transaction, ensuring zero orphaned tasks or unrecorded dispatches.
- **AC-6**: A worker claims a pending task by acquiring an exclusive lease with a fresh `lease_token` UUID, setting `lease_expiry`, and advancing the `attempt` counter strictly at claim time.
- **AC-7**: When multiple workers attempt to claim the same pending task concurrently, exactly one worker acquires the lease while other workers skip the row.
- **AC-8**: Completing a task requires matching the active `lease_token`, status `RUNNING`, and valid unexpired lease; an attempt by a stale worker with an expired lease token is rejected with zero accepted state change.
- **AC-9**: Requesting execution cancellation transitions the execution status to `CANCELLED` and sets `cancelled_at`; late worker results arriving after cancellation are discarded by the completion reconciler.
- **AC-10**: If a worker crashes while in state `CLAIMED` or `RUNNING`, or if a task lease expires before recovery runs, the scheduled recovery process resets the task to `PENDING`, increments no attempt counter, and emits a fresh retry dispatch outbox event with exponential backoff if the attempt counter is below maximum attempts.
- **AC-11**: When a task reaches its maximum attempt limit without success, the system transitions its status to `FAILED`, emits a task failure outbox event, and halts further automatic reclaims.
- **AC-12**: The outbox relay acquires durable relay leases on undelivered events using bounded batch queries with skip locked semantics, marks `delivered_at` upon successful transport dispatch, and limits execution duration to fit bounded Lambda runtimes.
- **AC-13**: The completion reconciler records incoming completion events into `completion_receipts` using conflict safe insert statements, discarding duplicate deliveries with zero repeated graph state transitions.
- **AC-14**: A crash occurring either before or after a LangGraph checkpoint commit recovers cleanly on replay, updating checkpoint state and marking `applied_at` without duplicate side effects or corruption, verified by accepted result checkpoint markers.
- **AC-15**: LangGraph checkpoints are never committed autonomously outside the orchestrator's explicit transactions; they buffer in-memory during execution and flush atomically in Phase 3.
- **AC-16**: Executions stranded in `RUNNING` state due to a worker crash are identified by the scheduled recovery loop, which clears the expired lease and emits an `EXECUTION_RESUME` outbox event to recover the graph.

## Options considered

### Option 1: Direct queue dispatch without transactional outbox

API and orchestrator workers publish directly to Amazon SQS without an intermediate database outbox table.

**Pros**:
- Lower dispatch latency since events bypass a secondary database write.
- Simpler architecture with fewer moving pieces.

**Cons**:
- Creates the dual write vulnerability: if the database transaction commits but the network call to SQS fails, the task is permanently lost.
- Complicates local integration testing by requiring cloud emulators for basic persistence verification.

### Option 2: Redis distributed locks for worker leasing

Use Redis Redlock or key expiration to manage worker leases and job execution states.

**Pros**:
- High throughput key expiration and in memory locking primitives.
- Fast lease renewals.

**Cons**:
- Violates the core architectural invariant that PostgreSQL is the single source of truth for execution state.
- Redis failover or cluster partition can release locks prematurely, leading to split brain execution.
- Execution state and lock state become disconnected across separate datastores.

### Option 3: Canonical PostgreSQL transactional outbox, fenced leases, and completion receipts (Recommended)

Implement execution tracking, worker task leasing, outbox events, and completion receipts directly on PostgreSQL using the tables specified in DATABASE.md. An outbox relay moves events to transport, while a scheduled recovery loop cleans expired leases and reconciles unapplied checkpoints.

**Pros**:
- Eliminates dual write errors by committing task dispatch and outbox records in a single database transaction.
- Prevents zombie worker overwrites using fenced lease tokens and monotonic attempt counters.
- Provides identical execution behavior across local Docker environments and AWS serverless deployments.
- Reconciles separate LangGraph checkpoint commits idempotently via completion receipts.

**Cons**:
- Polling workers and outbox relays require careful connection pooling and index optimization.
- Completed outbox events and receipt logs require scheduled retention cleanup.

## Decision

**Chosen option**: Option 3: Canonical PostgreSQL transactional outbox, fenced leases, and completion receipts.

We implement the execution engine in `backend/src/functions/orchestrator/execution/` adhering strictly to the `executions`, `tasks`, `worker_results`, `outbox_events`, `completion_receipts`, and `api_idempotency` tables from DATABASE.md.

## Rationale

PostgreSQL provides ACID guarantees that allow SWENA to couple business aggregate mutations with asynchronous task events in a single commit. This eliminates ghost tasks and state drift.

Fencing worker results with a unique `lease_token` and attempt counter ensures safety against slow workers and network partitions. If a worker pauses during a garbage collection cycle or network hang and resumes after its lease has expired, its write attempt fails safely because the token in the database has already rotated.

Receipt deduplication via `completion_receipts` handles the reality of at least once messaging queues without requiring expensive distributed consensus. LangGraph checkpoints are reconciled against accepted results, ensuring that worker results resume the state graph cleanly even if the application container crashes between database commit and graph invocation.

## Feature design

**Data model sketch**:

Mapped directly to DATABASE.md without competing tables:

```text
Table: api_idempotency
- owner_id: UUID (Required)
- operation: VARCHAR(64) (Required)
- key: VARCHAR(128) (Required)
- request_hash: VARCHAR(64) (Required, SHA256 of canonical request payload)
- response_json: JSONB (Required, stored response body)
- status_code: INTEGER (Required, HTTP response status)
- expires_at: TIMESTAMPTZ (Required, expiration timestamp)
Unique Constraint: (owner_id, operation, key)
Index: (expires_at)

Table: executions
- id: UUID (Primary Key)
- owner_id: UUID (Required, FK users.id)
- trip_id: UUID (Nullable, FK trips.id)
- workflow: VARCHAR(32) (ROUTE, MEDIA, TRAVEL)
- status: VARCHAR(32) (CREATED, RUNNING, WAITING_TASKS, WAITING_USER, SUCCEEDED, PARTIAL, FAILED, CANCELLED)
- context_version: INTEGER (Default 1, immutable plan version)
- checkpoint_id: VARCHAR(128) (Nullable, LangGraph thread checkpoint pointer)
- deadline_at: TIMESTAMPTZ (Required, absolute execution timeout)
- budget: JSONB (Nonnegative tool and cost limits)
- cancelled_at: TIMESTAMPTZ (Nullable)
- lease_token: UUID (Nullable, active orchestrator node lock)
- lease_expiry: TIMESTAMPTZ (Nullable)
- created_at: TIMESTAMPTZ (Default now)
- updated_at: TIMESTAMPTZ (Default now)
Indexes: (owner_id, created_at), partial index on (status, deadline_at) where status in ('CREATED', 'RUNNING', 'WAITING_TASKS')

Table: tasks
- id: UUID (Primary Key)
- execution_id: UUID (Required, FK executions.id)
- parent_task_id: UUID (Nullable, FK tasks.id)
- worker: VARCHAR(32) (EVIDENCE, BROWSER, DECISION, MEDIA_EXTRACT, MEDIA_ANALYZE, OPTIMIZE)
- action: VARCHAR(32) (e.g. collect, render, classify, solve)
- payload_ref: JSONB (Inline typed parameters or ArtifactRef pointer)
- input_hash: VARCHAR(64) (SHA256 of payload for deterministic tracking)
- idempotency_key: VARCHAR(128) (Required)
- attempt: INTEGER (Default 0, monotonic counter incremented strictly on claim)
- max_attempts: INTEGER (Default 3)
- context_version: INTEGER (Default 1)
- status: VARCHAR(32) (PENDING, CLAIMED, RUNNING, COMPLETED, FAILED, CANCELLED)
- lease_token: UUID (Nullable, current worker lease identifier)
- lease_expiry: TIMESTAMPTZ (Nullable)
- deadline_at: TIMESTAMPTZ (Required)
- result_id: UUID (Nullable, FK worker_results.id, set only upon accepted completion)
- created_at: TIMESTAMPTZ (Default now)
- updated_at: TIMESTAMPTZ (Default now)
Unique Constraint: (execution_id, idempotency_key)
Indexes: (execution_id, status), partial index on (status, lease_expiry) where status in ('PENDING', 'CLAIMED', 'RUNNING')

Table: worker_results
- id: UUID (Primary Key)
- task_id: UUID (Required, FK tasks.id)
- execution_id: UUID (Required, FK executions.id)
- attempt: INTEGER (Required)
- context_version: INTEGER (Required)
- status: VARCHAR(32) (SUCCESS, PARTIAL, NEEDS_BROWSER, WAITING_EXTERNAL, FAILED)
- output_ref: JSONB (Inline output payload or ArtifactRef)
- warnings: JSONB (Array of safe warning strings)
- error: JSONB (Nullable, error code, safe message, retry class)
- usage: JSONB (Provider calls, tokens, execution duration milliseconds)
- started_at: TIMESTAMPTZ (Required)
- finished_at: TIMESTAMPTZ (Required)
Unique Constraint: (task_id, attempt)

Table: outbox_events
- id: UUID (Primary Key)
- execution_id: UUID (Required, FK executions.id)
- task_id: UUID (Nullable, FK tasks.id)
- kind: VARCHAR(64) (e.g. TASK_DISPATCH, TASK_COMPLETED, TASK_FAILED, EXECUTION_CANCELLED)
- payload_ref: JSONB (Envelope for transport dispatch)
- due_at: TIMESTAMPTZ (Default now, backoff timestamp for retries)
- attempts: INTEGER (Default 0)
- delivered_at: TIMESTAMPTZ (Nullable, set when published to queue)
- lease_token: UUID (Nullable, active relay worker lease)
- lease_expiry: TIMESTAMPTZ (Nullable, relay lease expiration)
- created_at: TIMESTAMPTZ (Default now)
Index: partial on (due_at) where delivered_at is null

Table: completion_receipts
- event_id: UUID (Primary Key, unique message identifier from completion outbox)
- task_id: UUID (Required, FK tasks.id)
- execution_id: UUID (Required, FK executions.id)
- attempt: INTEGER (Required)
- result_id: UUID (Required, FK worker_results.id)
- received_at: TIMESTAMPTZ (Default now)
- applied_at: TIMESTAMPTZ (Nullable, set when LangGraph checkpoint accepts result)
Index: (task_id, result_id)

Table: job_events
- id: UUID (Primary Key)
- execution_id: UUID (Required, FK executions.id)
- sequence: INTEGER (Required, monotonic per execution sequence)
- event_type: VARCHAR(64) (Required)
- safe_payload: JSONB (Required, scrubbed for client polling)
- timestamp: TIMESTAMPTZ (Default now)
Unique Constraint: (execution_id, sequence)
Index: (execution_id, sequence)
```

**State transitions**:

Execution lifecycle:
- `CREATED`: Record initialized, start outbox event created.
- `RUNNING`: Orchestrator active, evaluating nodes.
- `WAITING_TASKS`: Tasks dispatched to workers, orchestrator paused.
- `WAITING_USER`: Ambiguous place or missing constraint requiring human response.
- `SUCCEEDED`: Terminal success, complete result persisted.
- `PARTIAL`: Terminal partial outcome, budget exhausted or unresolvable gaps.
- `FAILED`: Terminal failure, unrecoverable error.
- `CANCELLED`: Terminal cancellation, client requested abort.

Task lifecycle:
- `PENDING`: Written by orchestrator, waiting for worker claim.
- `CLAIMED`: Worker lease acquired with `lease_token`.
- `RUNNING`: Worker actively executing bounded tool calls.
- `COMPLETED`: Worker completed, valid `result_id` linked, accepted by fence.
- `FAILED`: Max attempts exhausted or unrecoverable worker failure.
- `CANCELLED`: Parent execution cancelled before completion.

## Exact transaction boundaries

The execution layer partitions database operations into nine distinct, isolated transactions. No transaction is held open while awaiting network calls or long running computations:

1. **Transaction 1: Job acceptance and idempotency reservation (API Layer)**
   - Owned by API module (`backend/src/functions/api/`).
   - Executes conflict safe insert on `api_idempotency`:
     ```sql
     INSERT INTO api_idempotency (
         owner_id, operation, key, request_hash, response_json, status_code, expires_at
     ) VALUES (
         :owner_id, :operation, :key, :request_hash, :response_json, :status_code, :expires_at
     )
     ON CONFLICT (owner_id, operation, key) DO NOTHING
     RETURNING owner_id;
     ```
   - If row is returned: this is a new request. Inserts `executions` row with status `CREATED` and inserts initial `outbox_events` record (`EXECUTION_START`). Commits transaction. Client receives HTTP 202 with `execution_id`.
   - If zero rows returned: concurrent or repeat request. Executes conflict query:
     ```sql
     SELECT request_hash, response_json, status_code
     FROM api_idempotency
     WHERE owner_id = :owner_id AND operation = :operation AND key = :key;
     ```
   - If `request_hash` matches: commits and returns cached `response_json` with stored `status_code`.
   - If `request_hash` differs: rolls back and raises HTTP 409 conflict error.

2. **Transaction 2: Task dispatch by orchestrator**
   - Owned by Orchestrator module (`backend/src/functions/orchestrator/`).
   - Serializes parent execution:
     ```sql
     SELECT id, status, context_version
     FROM executions
     WHERE id = :execution_id
     FOR UPDATE;
     ```
   - Flushes the buffered LangGraph checkpointer state to PostgreSQL, committing the checkpoint exactly once atomically with the orchestrator tasks.
   - Inserts `tasks` rows with status `PENDING`, attempt counter initialized to 0, and null lease token.
   - Inserts matching `outbox_events` rows with kind `TASK_DISPATCH`.
   - Updates `executions SET status = 'WAITING_TASKS', updated_at = now() WHERE id = :execution_id`.
   - Commits atomically. Zero task can exist without its corresponding outbox dispatch entry, and no checkpoint exists without its tasks.

3. **Transaction 3: Durable outbox relay batch claim and dispatch**
   - Outbox relay worker acquires durable leases on due events using skip locked semantics:
     ```sql
     UPDATE outbox_events
     SET lease_token = :relay_lease_token,
         lease_expiry = now() + (:relay_lease_seconds * interval '1 second')
     WHERE id IN (
         SELECT id
         FROM outbox_events
         WHERE delivered_at IS NULL
           AND due_at <= now()
           AND (lease_expiry IS NULL OR lease_expiry <= now())
         ORDER BY due_at ASC
         LIMIT :batch_size
         FOR UPDATE SKIP LOCKED
     )
     RETURNING id, execution_id, task_id, kind, payload_ref;
     ```
   - Commits lease acquisition.
   - Relay worker publishes messages to Amazon SQS or local queue adapter outside database transaction.
   - For each successfully dispatched event, executes delivery confirmation:
     ```sql
     UPDATE outbox_events
     SET delivered_at = now(),
         attempts = attempts + 1,
         lease_token = NULL,
         lease_expiry = NULL
     WHERE id = :id AND lease_token = :relay_lease_token;
     ```
   - If queue transport fails for an event, applies exponential backoff:
     ```sql
     UPDATE outbox_events
     SET due_at = now() + (interval '2 seconds' * power(2, attempts)),
         attempts = attempts + 1,
         lease_token = NULL,
         lease_expiry = NULL
     WHERE id = :id AND lease_token = :relay_lease_token;
     ```
   - Commits delivery confirmations.

4. **Transaction 4: Queue message receipt and atomic task claiming**
   - Worker receives queue message containing `task_id` and `execution_id`.
   - Executes fenced atomic claim query, incrementing the attempt counter strictly on claim:
     ```sql
     UPDATE tasks
     SET status = 'CLAIMED',
         lease_token = :new_lease_token,
         lease_expiry = now() + (:lease_seconds * interval '1 second'),
         attempt = attempt + 1,
         updated_at = now()
     WHERE id = :task_id
       AND (
         (status = 'PENDING' AND (lease_expiry IS NULL OR lease_expiry <= now()))
         OR (status IN ('CLAIMED', 'RUNNING') AND lease_expiry <= now())
       )
     RETURNING id, attempt, lease_token;
     ```
   - If row returned: commits transaction. Worker now holds exclusive lease and runs tool invocations.
   - If zero rows returned: checks task status. If `COMPLETED`, `FAILED`, or `CANCELLED`, acknowledges and drops duplicate queue message. If active under another unexpired lease, leaves message or delays visibility.

5. **Transaction 5: Worker result commit and completion outbox**
   - Worker finishes tool execution.
   - Inserts `worker_results` record.
   - Executes fenced completion predicate on `tasks`:
     ```sql
     UPDATE tasks
     SET status = 'COMPLETED',
         result_id = :result_id,
         lease_token = NULL,
         lease_expiry = NULL,
         updated_at = now()
     WHERE id = :task_id
       AND lease_token = :lease_token
       AND status IN ('CLAIMED', 'RUNNING')
       AND lease_expiry > now()
     RETURNING id;
     ```
   - Inserts `outbox_events` record with kind `TASK_COMPLETED`.
   - If update returns zero rows: lease expired or task was cancelled; transaction rolls back, result is rejected, error is logged.
   - If update succeeds: commits transaction. Worker acknowledges queue message.

6. **Transaction 6: Conflict safe completion receipt insertion**
   - Reconciler receives `TASK_COMPLETED` outbox event.
   - Inserts receipt using conflict safe syntax:
     ```sql
     INSERT INTO completion_receipts (
         event_id, task_id, execution_id, attempt, result_id, received_at
     ) VALUES (
         :event_id, :task_id, :execution_id, :attempt, :result_id, now()
     )
     ON CONFLICT (event_id) DO NOTHING
     RETURNING event_id;
     ```
   - If zero rows returned: duplicate completion event. Commits transaction and exits immediately with zero secondary transitions.
   - If row returned: commits receipt.

7. **Transaction 7: Serialized execution lock and replay safe graph resume**
   - Reconciler locks parent execution to serialize graph steps:
     ```sql
     SELECT id, status, context_version, checkpoint_id
     FROM executions
     WHERE id = :execution_id
     FOR UPDATE;
     ```
   - If `executions.status` is `CANCELLED`:
     ```sql
     UPDATE completion_receipts
     SET applied_at = now()
     WHERE event_id = :event_id;
     ```
     Commits transaction and discards result with zero graph resume.
   - If execution is active: retrieves LangGraph checkpoint state.
   - Checks if `result_id` is present in `state.values.get("accepted_result_ids", [])`.
   - If absent: calls `graph.aupdate_state` appending `result_id` to `accepted_result_ids` and setting `task_outputs[task_id] = output`.
   - Marks receipt applied:
     ```sql
     UPDATE completion_receipts
     SET applied_at = now()
     WHERE event_id = :event_id
       AND applied_at IS NULL;
     ```
   - Checks if all dispatched tasks for the current execution step are completed; if true, sets `executions.status = 'RUNNING'`.
   - Flushes the buffered LangGraph checkpointer state to PostgreSQL within the transaction.
   - Commits transaction.

8. **Transaction 8: Fenced execution cancellation**
   - Client calls `POST /api/v1/jobs/{job_id}/cancel`.
   - Executes fenced cancellation query:
     ```sql
     UPDATE executions
     SET status = 'CANCELLED',
         cancelled_at = now(),
         updated_at = now()
     WHERE id = :execution_id
       AND owner_id = :owner_id
       AND status NOT IN ('SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED')
     RETURNING id;
     ```
   - Inserts `job_events` record recording cancellation.
   - Inserts `outbox_events` record with kind `EXECUTION_CANCELLED`.
   - Commits transaction. Client receives cancellation confirmation.

9. **Transaction 9: Scheduled recovery, lease expiration, and retry dispatch**
   - Recovery loop identifies expired task leases.
   - Reclaims expired tasks with remaining attempts:
     ```sql
     UPDATE tasks
     SET status = 'PENDING',
         lease_token = NULL,
         lease_expiry = NULL,
         updated_at = now()
     WHERE status IN ('CLAIMED', 'RUNNING')
       AND lease_expiry <= now()
       AND attempt < max_attempts
     RETURNING id, execution_id, worker, action, payload_ref, attempt, context_version;
     ```
   - For each reclaimed task, inserts a fresh retry dispatch outbox event with exponential backoff:
     ```sql
     INSERT INTO outbox_events (
         id, execution_id, task_id, kind, payload_ref, due_at
     ) VALUES (
         gen_random_uuid(), :execution_id, :task_id, 'TASK_DISPATCH', :payload_ref, now() + (:backoff_seconds * interval '1 second')
     );
     ```
   - Marks exhausted tasks failed:
     ```sql
     UPDATE tasks
     SET status = 'FAILED',
         lease_token = NULL,
         lease_expiry = NULL,
         updated_at = now()
     WHERE status IN ('CLAIMED', 'RUNNING')
       AND lease_expiry <= now()
       AND attempt >= max_attempts
     RETURNING id, execution_id;
     ```
   - For each failed task, inserts `TASK_FAILED` outbox event.
   - Reclaims expired executions stuck in `RUNNING`:
     ```sql
     UPDATE executions
     SET lease_token = NULL,
         lease_expiry = NULL,
         updated_at = now()
     WHERE status = 'RUNNING'
       AND lease_expiry <= now()
     RETURNING id;
     ```
   - For each recovered execution, inserts an `EXECUTION_RESUME` outbox event to wake it up.
   - Commits recovery updates.

## Queue message claiming protocol

Queue transport provides at least once delivery. Receipt of a queue message does not confer task ownership. Workers follow this strict protocol:

1. **Queue message payload**:
   ```json
   {
     "schema_version": "1.0",
     "task_id": "22222222-2222-4222-8222-222222222222",
     "execution_id": "11111111-1111-4111-8111-111111111111"
   }
   ```
2. **Claim verification**:
   The worker initiates Transaction 4. It queries the database using `FOR UPDATE`:
   - If task status is `COMPLETED`, `FAILED`, or `CANCELLED`: message is a duplicate or redundant replay. Worker acknowledges and removes queue message immediately.
   - If task status is `RUNNING` or `CLAIMED` and `lease_expiry > now()`: another worker is actively executing this task under an unexpired lease. Worker leaves message or extends visibility timeout, deferring duplicate delivery.
   - If task status is `PENDING`, or (`status` IN ('CLAIMED', 'RUNNING') AND `lease_expiry <= now()` AND `attempt < max_attempts`): worker executes fenced claim update, acquiring exclusive ownership with a fresh `lease_token` and incrementing the attempt counter.
3. **Tool execution**:
   Only after Transaction 4 commits does the worker invoke external tools. During execution, long running tools periodically extend their lease via the renewal predicate:
   ```sql
   UPDATE tasks
   SET lease_expiry = now() + (:extension_seconds * interval '1 second'),
       updated_at = now()
   WHERE id = :task_id
     AND lease_token = :lease_token
     AND status IN ('CLAIMED', 'RUNNING')
     AND lease_expiry > now()
   RETURNING id;
   ```
4. **Result completion**:
   Worker executes Transaction 5. If fenced update succeeds, worker acknowledges queue message. If update returns zero rows (fenced out), worker discards local output and logs failure.

## Supported AWS scheduling

Both relay and recovery processes operate on bounded, scheduled cadences using supported AWS EventBridge Scheduler configurations:

**Supported AWS EventBridge configurations**:
- **Outbox relay schedule**: AWS EventBridge Scheduler schedule calling the relay Lambda handler on a recurring schedule with rate expression `rate(1 minute)` and payload `{"action": "relay", "batch_size": 50}`.
- **Execution recovery schedule**: AWS EventBridge Scheduler schedule calling the recovery Lambda handler on a recurring schedule with rate expression `rate(1 minute)` and payload `{"action": "recovery"}`.
- **Local development equivalence**: In local Docker development, an asynchronous background task runner in Python executes the same relay and recovery functions using `asyncio.sleep(15)` intervals without requiring AWS services.

**Outbox relay execution loop**:
- Duration cap: loops for a maximum of `OUTBOX_MAX_RUNTIME_SECONDS` (default 25 seconds), then cleanly exits to avoid Lambda timeouts.
- Concurrency: multiple relay workers can run in parallel. Each worker uses `SELECT FOR UPDATE SKIP LOCKED` on `outbox_events`, preventing lock contention.
- Exponential backoff: if queue transport fails, event `due_at` is set to `now() + (interval '2 seconds' * power(2, attempts))` and `attempts` counter increments.

**Scheduled recovery execution loop**:
- Scan 1: Expired task leases (`status IN ('CLAIMED', 'RUNNING') AND lease_expiry <= now()`). Reclaims eligible tasks to `PENDING` with fresh `TASK_DISPATCH` events or marks exhausted tasks `FAILED`.
- Scan 2: Stalled completion receipts (`received_at < now() - interval '60 seconds' AND applied_at IS NULL`). Triggers checkpoint reconciliation.
- Scan 3: Expired idempotency records (`expires_at < now()`). Deletes stale cache entries.

## Replay safe checkpoint reconciliation

LangGraph checkpoints and custom application tables live in separate persistence contexts. The reconciliation protocol guarantees replay safety across all crash boundaries using accepted result markers:

1. **Receipt insertion**:
   The reconciler receives a `TASK_COMPLETED` outbox event and inserts a row into `completion_receipts` using `ON CONFLICT (event_id) DO NOTHING`.
2. **Cancellation barrier**:
   Before updating checkpoint state, the reconciler checks `executions.status`. If the execution was cancelled, the reconciler sets `applied_at = now()` and skips checkpoint updates, guaranteeing that cancelled executions are never resumed.
3. **Accepted result checkpoint markers**:
   The LangGraph checkpoint thread state maintains a dedicated list of accepted result markers:
   ```python
   class ExecutionState(TypedDict):
       accepted_result_ids: list[str]
       task_outputs: dict[str, Any]
   ```
   Before updating state, the reconciler loads the current thread state:
   ```python
   current_state = await graph.aget_state(
       config={"configurable": {"thread_id": str(execution_id)}}
   )
   accepted_markers = current_state.values.get("accepted_result_ids", [])
   ```
   If `str(result_id)` is already present in `accepted_markers`, the checkpoint has already incorporated this result. The reconciler skips graph update and advances directly to marking `applied_at`.
4. **Idempotent graph update**:
   If `str(result_id)` is absent, the reconciler invokes LangGraph:
   ```python
   await graph.aupdate_state(
       config={"configurable": {"thread_id": str(execution_id)}},
       values={
           "accepted_result_ids": accepted_markers + [str(result_id)],
           "task_outputs": {str(task_id): result_output},
       },
       as_node="worker_node",
   )
   ```
   *Crucially, this `aupdate_state` call uses a transaction-bound checkpointer. The checkpointer buffers the write in memory. The write is flushed to the database atomically in Phase 3 (Transaction 7), preventing state forks if a runner is concurrently active in Phase 2.*
5. **Receipt confirmation**:
   After graph invocation succeeds, the reconciler marks `completion_receipts.applied_at = now()`.

**Crash analysis and recovery**:
- **Crash before Phase 3 flush**: `worker_results` and `completion_receipts` exist, but `applied_at` is NULL. No checkpoint is committed. Scheduled recovery or queue redelivery detects unapplied receipt, re-invokes `aupdate_state` with identical output, and marks `applied_at = now()`.
- **Crash after Phase 3 flush**: LangGraph checkpoint contains the update and accepted result marker, and `completion_receipts.applied_at` is marked. If a queue redelivery occurs, the conflict-safe `INSERT` ignores it.

## Value sourcing

| Action | Value produced / displayed | Source |
|---|---|---|
| Create execution | execution_id and created_at | System generated UUID and current timestamp |
| Create execution | context_version | Starts at 1; incremented upon user answer or plan revision |
| Check idempotency | cached response | response_json stored in api_idempotency table |
| Dispatch tasks | task_id and input_hash | Generated UUID and SHA256 digest of task payload |
| Claim task | lease_token | Fresh random UUID generated on each claim transaction |
| Claim task | lease_expiry | Current database timestamp plus configured lease duration seconds |
| Complete task | result_id | Fresh UUID generated and inserted into worker_results |
| Outbox relay | delivered_at | Current timestamp recorded after transport acknowledge |
| Completion receipt | event_id and received_at | Carried from completion outbox event payload |
| Reconcile checkpoint | checkpoint_id | Thread checkpoint pointer returned by LangGraph adapter |

## Key invariants

- PostgreSQL is the single source of truth for all execution, task, and outbox states.
- Every API operation and read query must enforce owner matching (`owner_id = authenticated_user_id`). A foreign key alone does not prove authorization.
- An outbox event must be written in the exact same database transaction as the state change that produced it.
- Task attempt counters increment strictly at claim time in Transaction 4, never at dispatch or recovery time.
- A worker result is accepted if and only if the `lease_token` matches the active task record, status is `RUNNING`, and lease has not expired.
- Stale workers whose leases expired can never overwrite newer results or advance task state.
- Once an execution enters a terminal state (`SUCCEEDED`, `PARTIAL`, `FAILED`, `CANCELLED`), its status is immutable. Late worker results arriving for cancelled executions are safely discarded.
- Transport queues deliver at least once; all consumers must deduplicate using logical event and task identifiers.

## Security model

- Owner identity is derived strictly from verified cryptographic tokens via the SWYRA Auth JWKS endpoint. Client supplied `owner_id` fields are rejected.
- All database queries enforce owner predicates. Accessing another user's execution returns an HTTP 404 to avoid information disclosure.
- Payloads and error messages must never record unmasked secrets, raw authorization headers, or sensitive credentials.

## Guarantees and limitations

- **Delivery guarantees**: Queue transport is at least once. Outbox relay can publish duplicate messages if a network timeout occurs while marking `delivered_at`.
- **Deduplication guarantee**: The completion reconciler provides at most once processing per attempt by enforcing unique constraints on `completion_receipts.event_id`. Replays result in a clean skip with zero repeated side effects.
- **Safety guarantee**: Zombie workers are neutralized by optimistic fencing (`lease_token` match). A worker that suffers a process pause cannot overwrite a task reclaimed by another worker.
- **Limitations**: Clock drift across database and worker containers must stay within acceptable tolerances (sub second). Lease durations should exceed expected network round trip plus execution time (default 60 seconds). Network partition between database and external queue delays event dispatch until the relay re-polls.

## Configuration required

- `DATABASE_URL`: SQLAlchemy asyncpg connection string.
- `ENVIRONMENT`: Runtime environment mode (`local`, `staging`, `production`).
- `DEBUG`: Boolean flag controlling query echo.
- `ORCHESTRATOR_LEASE_SECONDS`: Default duration for task execution leases (default 60).
- `OUTBOX_BATCH_SIZE`: Maximum events processed per outbox relay invocation (default 50).
- `OUTBOX_MAX_RUNTIME_SECONDS`: Maximum seconds an outbox relay loop runs before cleanly exiting (default 25).

## Critical test scenarios and PostgreSQL integration tests

Every race and crash boundary is verified using real PostgreSQL asyncpg integration tests:

1. **Owner scoped idempotency hit**: Submitting two identical creation requests with the same key and payload returns the identical execution identifier and payload without a second insert, verifies **AC-1**, **AC-4**.
2. **Owner scoped idempotency conflict**: Submitting a request with an existing key but a different request payload returns an HTTP 409 conflict, verifies **AC-2**.
3. **Concurrent idempotency race**: Two identical requests arrive at the exact same millisecond with identical idempotency key before either has committed; verify exactly one creates the record and the second waits or handles the constraint cleanly to return the identical response without race failure, verifies **AC-3**.
4. **Cross owner access rejection**: User B attempting to read, cancel, or complete an execution belonging to User A receives an HTTP 404, verifies **AC-4**.
5. **Atomic dispatch and outbox write**: Dispatched tasks and corresponding `outbox_events` commit atomically; injecting a transaction abort leaves zero orphaned tasks or events, verifies **AC-5**.
6. **Concurrent worker claim race**: Ten concurrent workers query for pending tasks simultaneously using `SELECT FOR UPDATE SKIP LOCKED`; each task is claimed by exactly one worker with a unique `lease_token`, verifies **AC-6**, **AC-7**.
7. **Attempt counter increment on claim only**: Verify task attempt counter remains 0 on dispatch and increments strictly to 1 upon worker claim in Transaction 4, verifies **AC-6**.
8. **Fenced lease completion**: Worker 1 claims a task. Worker 1 completes the task before lease expiry; completion succeeds and links `tasks.result_id`, verifies **AC-6**, **AC-8**.
9. **Zombie worker rejection**: Worker 1 claims a task. The lease expires and recovery resets the task. Worker 2 claims the task with a new lease token. Worker 1 wakes up and attempts to complete; Worker 1 completion is rejected due to mismatched lease token, verifies **AC-8**.
10. **Lease expiry before recovery**: A task's lease expires while recovery has not yet run. Worker 2 queries for claimable tasks and acquires the task directly via the fenced claim predicate; Worker 1 late completion is rejected, verifies **AC-6**, **AC-8**, **AC-10**.
11. **CLAIMED task worker crash**: Worker acquires task lease and terminates immediately before calling any tool or writing any result; verify recovery detects expired lease and resets task to `PENDING` with retry dispatch event and unchanged attempt counter, verifies **AC-10**.
12. **Running cancellation race**: An execution has active running tasks. Client requests cancellation; execution transitions to `CANCELLED`. In flight workers return results; completion reconciler detects cancelled state, discards results, and prevents graph resurrection, verifies **AC-9**.
13. **Crash recovery and retry dispatch event**: A worker crashes without completing. Scheduled recovery identifies expired lease, emits fresh `TASK_DISPATCH` outbox event with backoff timestamp, and preserves retry capability, verifies **AC-10**.
14. **Max retries terminal failure**: A task crashes repeatedly until reaching maximum attempts; recovery transitions task status to `FAILED`, emits `TASK_FAILED` outbox event, and stops re-queueing, verifies **AC-11**.
15. **Durable outbox relay lease and bounded dispatch**: Relay acquires durable lease on undelivered events, publishes to transport, marks `delivered_at`, and exits cleanly when batch size or time limit is reached, verifies **AC-12**.
16. **Outbox duplicate deduplication**: A completion outbox event is delivered twice to the reconciler; the second event triggers conflict safe insert on `completion_receipts.event_id` and exits cleanly without duplicating graph updates, verifies **AC-13**.
17. **Crash before checkpoint commit**: `worker_results` and `completion_receipts` exist, but LangGraph checkpoint was not updated; verify reconciler replays the state update cleanly, verifies **AC-14**.
18. **Crash after checkpoint commit**: LangGraph checkpoint contains the update and accepted result marker, but `completion_receipts.applied_at` was not marked; verify reconciler detects marker, bypasses duplicate node execution, and successfully marks `applied_at`, verifies **AC-14**.

## Build plan

The build plan proceeds in strict foundation slices, testing each boundary with real PostgreSQL transactions before proceeding:

1. Create Alembic migration for canonical tables: `api_idempotency`, `executions`, `tasks`, `worker_results`, `outbox_events`, `completion_receipts`, and `job_events`, satisfies **AC-1**, **AC-2**, **AC-5**.
2. Implement `IdempotencyRepository` in `backend/src/functions/api/idempotency.py` under API module ownership, supporting owner scoped reservation, SHA256 hash checks, and conflict safe inserts, satisfies **AC-1**, **AC-2**, **AC-3**, **AC-4**.
3. Implement `ExecutionRepository` and `TaskRepository` in `backend/src/functions/orchestrator/execution/repository.py` with atomic outbox dispatch and fenced skip locked claim mechanics incrementing attempts strictly on claim, satisfies **AC-4**, **AC-5**, **AC-6**, **AC-7**.
4. Implement fenced completion and cancellation logic in `backend/src/functions/orchestrator/execution/completion.py`, rejecting stale lease tokens and handling cancellation races, satisfies **AC-8**, **AC-9**.
5. Implement scheduled recovery loop in `backend/src/functions/orchestrator/execution/recovery.py` handling task lease expiration, retry dispatch event generation, terminal failure transitions, and execution lease recovery for orphaned `RUNNING` executions, satisfies **AC-10**, **AC-11**, **AC-16**.
6. Implement bounded outbox relay runner in `backend/src/functions/orchestrator/execution/relay.py` with durable relay leases, time capped execution loops, and batching, satisfies **AC-12**.
7. Implement `TransactionBoundCheckpointer` in `backend/src/functions/orchestrator/execution/checkpointer.py` that buffers LangGraph state writes in memory and flushes atomically with SQLAlchemy sessions, satisfies **AC-15**.
8. Implement completion reconciler in `backend/src/functions/orchestrator/execution/reconciliation.py` using conflict safe `completion_receipts` deduplication, accepted result markers, and `TransactionBoundCheckpointer`, satisfies **AC-13**, **AC-14**, **AC-15**.
9. Build comprehensive PostgreSQL integration test suite in `backend/tests/integration/test_orchestrator_execution.py` covering all critical race, crash, and reconciliation scenarios, satisfies **AC-1** to **AC-16**.

## Consequences

**Positive**:
- Direct alignment with TECHNICAL_SPEC.md and DATABASE.md without competing tables or schemas.
- Complete protection against dual write bugs, zombie worker overwrites, and replay inconsistencies.
- Clear separation between at least once queue delivery and at most once deduplicated application state transitions.
- Reliable crash recovery that operates with supported AWS EventBridge scheduling and local Python equivalents.

**Negative / tradeoffs**:
- Requires periodic vacuuming and archiving for high volume outbox and receipt tables.
- Skip locked polling queries require dedicated database connections from worker pools.

**Neutral**:
- Requires running database migration before downstream modules can dispatch background tasks.

## Follow-up

- [ ] Execute Alembic migration to establish the canonical execution tables.
- [ ] Connect outbox relay to Amazon SQS transport adapter in cloud deployment environments.
- [ ] Configure AWS EventBridge Scheduler schedules for outbox relay and recovery.

## References

**Project sources**:
- `docs/TECHNICAL_SPEC.md`
- `docs/reference/DATABASE.md`
- `docs/reference/API.md`
- `docs/DIAGRAMS.md`
- `archive/phases.md`

**Practices & standards**:
- Transactional Outbox Pattern for distributed event consistency.
- Fenced Distributed Locks using monotonic tokens and attempt counters (Martin Kleppmann fencing token pattern).
- At least once messaging with receiver deduplication receipts.
- Skip Locked concurrency patterns for queue tables in PostgreSQL.
