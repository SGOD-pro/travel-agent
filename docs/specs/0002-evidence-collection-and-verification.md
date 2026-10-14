# 0002. Evidence collection with provenance and canonical place and claim verification

**Date**: 2026-10-10
**Status**: Accepted

## Summary

This specification defines the evidence collection, immutable source observation persistence, canonical place resolution, and deterministic claim verification architecture for SWENA workflows. It implements Phase 4 of the development roadmap by coupling live web search and permitted page extraction with verifiable provenance records in PostgreSQL and PostGIS. The design separates external retrieval from deterministic policy evaluation, ensuring that language model interpretations propose facts without directly mutating verification truth. All worker actions execute via the accepted 0001 execution engine and worker registry, maintaining strict transaction boundaries, budget enforcement, and independent AWS Lambda packaging.

## Context

Travel planning, festival road research, and corridor intelligence depend on factual real world assertions such as venue operating hours, road restrictions, entry fees, and geographic coordinates. Gathering this information requires searching the public web through search engines and extracting structured text from permitted provider pages. However, web sources are frequently stale, contradictory, syndicated, or biased.

Without structured provenance, downstream itinerary algorithms cannot trace where a claim originated or distinguish an authoritative police traffic advisory from an outdated blog post. Furthermore, allowing generative language models to evaluate claim truthfulness autonomously creates hallucinations and false verification states. Critical assertions, such as festival street closures or sanctuary opening hours, require verifiable evidence matching specific temporal and geographic scopes.

Additionally, external searches incur financial costs and latency. Unbounded search requests risk exhausting API budgets or timing out serverless execution contexts. Integrating evidence retrieval with the durable execution engine built in spec 0001 ensures that tasks run under explicit attempt fencing, receive bounded tool budgets, and record partial results gracefully if limits are reached.

## Module ownership boundaries

Component ownership is partitioned across modules per ARCHITECTURE.md and DATABASE.md:

1. **Evidence collector module ownership** (`backend/src/functions/evidence_collector/`):
   - Owns external search adapters (SerpAPI client and test only fixture fallback).
   - Owns permitted web page parsing using Scrapling and CSS selectors.
   - Owns SHA256 content hashing, canonical URL normalization, and raw text excerpt extraction.
   - Owns writing immutable `source_observations` records for execution runs.
   - Owns its dedicated AWS Lambda consumer handler triggered by the task dispatch queue.
   - Emits `NEEDS_BROWSER` requests when dynamic rendering or JavaScript execution is required, without invoking browsers directly.
   - Never evaluates final verification states or resolves canonical place identities.
   - Permitted execution support imports from 0001:
     - `functions.orchestrator.execution.fencing`: `WorkerFencingService`, `TaskQueueMessage`, `TaskClaimResult`, execution engine exceptions (`TaskClaimConflictError`, `TaskLeaseActiveError`, `TaskAlreadyTerminalError`, `StaleQueueMessageError`, `DeadlineExceededError`, `ParentCancelledError`, `LeaseFencingError`).
     - `functions.orchestrator.execution.models`: `ExecutionModel`, `TaskModel`, `WorkerResultModel`, `OutboxEventModel`.
     - `functions.orchestrator.execution.job_events`: `record_job_event`.
     - `config.db`: `get_session_factory`.
     - `contracts.evidence`: `EvidenceClass`, `HotelObservation`, `FareObservation`.
     - `contracts.money`: `Currency`, `Money`.
   - Forbidden imports:
     - Orchestrator evidence policy: `functions.orchestrator.evidence.*` (policy engine, claim verification).
     - Orchestrator canonical places: `functions.orchestrator.places.*` (canonical places repository, PostGIS matching).
     - Orchestrator workflows: `functions.orchestrator.graphs.*`, `functions.orchestrator.runner.*`, `functions.orchestrator.checkpointer.*`.
     - Browser automation: `functions.browser.*`.
     - Reverse imports: Orchestrator modules never import `functions.evidence_collector.*`.

2. **Orchestrator evidence and place policy ownership** (`backend/src/functions/orchestrator/evidence/` and `backend/src/functions/orchestrator/places/`):
   - Owns the deterministic Policy Engine that evaluates claim verification states (`VERIFIED`, `PARTIALLY_VERIFIED`, `CONFLICTED`, `UNVERIFIED`, `UNAVAILABLE`).
   - Owns freshness transition rules based on category expiry windows and travel dates.
   - Owns `canonical_places`, `claims`, and `claim_evidence` tables and repositories.
   - Owns PostGIS spatial coordinate matching, GiST indexing, and nearby distance band queries (`ST_DWithin`).
   - Reconciles proposed claims against existing records without overwriting contradictory assertions.

3. **Orchestrator execution engine ownership** (`backend/src/functions/orchestrator/execution/`):
   - Inherited directly from spec 0001: owns task leases, attempt counters, transactional outbox relay, SQS dispatch, and completion reconciliation.
   - Routes `TASK_DISPATCH` messages destined for `worker == "EVIDENCE"` to the evidence collector queue.

4. **API and browser module boundaries**:
   - API (`backend/src/functions/api/`) queries verified claims and places through owner scoped endpoints; it does not collect evidence directly.
   - Browser module (`backend/src/functions/browser/`) owns headless Chromium rendering for Phase 5; the evidence collector never imports browser runtimes.

## Requirements

**User stories**:
- As a travel planning workflow, I want to execute targeted web searches and permitted page scrapes so that candidate facts about venues, timings, and routes are gathered with attributable evidence.
- As a system verifier, I want every extracted claim linked to an immutable source observation with URL, retrieval timestamp, and content hash so that citations are verifiable and syndicated copies are identified.
- As an itinerary planner, I want contradictory assertions preserved side by side rather than silently overwritten so that conflicting travel guidance is surfaced honestly to travelers.
- As an operations engineer, I want external search tasks bounded by call limits and minor unit cost caps so that runaway workflows cannot exceed external provider budgets.

**Acceptance criteria**:
- **AC-1**: Executing search queries via the SerpAPI adapter returns normalized organic search and place results, logging provider response latency and minor unit cost while respecting timeout limits.
- **AC-2**: Extracting permitted web pages via the Scrapling adapter parses structured text for hotel, fare, destination, and festival observations using CSS selectors, rejecting unparseable prices rather than coercing them to zero.
- **AC-3**: Every collected source page or search result persists an immutable `source_observations` row with SHA256 content hash, canonical URL, retrieval timestamp, publisher identifier, extraction method, and excerpt text.
- **AC-4**: A syndicate or copied article sharing an identical content hash or text fingerprint with an existing observation is linked but flagged as non independent corroboration.
- **AC-5**: Canonical places are stored in `canonical_places` with reviewed names, locality, PostGIS `geometry(Point, 4326)`, access points, geocoder provenance, and provider identifier mappings, indexed via GiST for spatial lookup.
- **AC-6**: Spatial queries for nearby places support configurable annular bands (such as 2000 to 3000 meters and 5000 to 7000 meters) using `ST_DWithin` on geography coordinates.
- **AC-7**: Candidate place matching resolves incoming place observations against existing `canonical_places` records using spatial proximity within 100 meters and name similarity. Ambiguous places (same name, different localities) are flagged for clarification without destructive entity merging.
- **AC-8**: Extracted claims persist in the `claims` table linked to observations via `claim_evidence`, recording subject, category, typed value, validity interval, geographic scope, and policy version.
- **AC-9**: The deterministic Policy Engine evaluates claim verification status (`VERIFIED`, `PARTIALLY_VERIFIED`, `CONFLICTED`, `UNVERIFIED`) and availability status separately; generative model output alone cannot transition a claim to `VERIFIED`.
- **AC-10**: Critical road closures and festival restrictions require an authoritative advisory from government or police domains with matching geographic and temporal scope to reach `VERIFIED` status; secondary blog mentions remain `UNVERIFIED`.
- **AC-11**: Contradictory claims for the same subject and category (such as conflicting opening hours or divergent entry fees) are preserved as separate records with status `CONFLICTED` and cross referenced in their contradictions list.
- **AC-12**: Claim freshness is evaluated against category policies (`FRESH`, `STALE`, or `UNKNOWN`); claims past their category validity window transition to `STALE` without altering historical verification records.
- **AC-13**: When a permitted web page requires JavaScript or dynamic interaction, the collector records a placeholder observation, flags `browser_requests`, and returns without attempting headless browser execution.
- **AC-14**: Tasks dispatched with budget limits enforce durable budget reservations across concurrent tasks. They stop dispatching external calls immediately when limits are reached or execution deadlines approach, returning partial observations and acknowledging cancellation safely (deferred to Slice 3 alongside live provider adapters).
- **AC-15**: The evidence collector handles SQS `TASK_DISPATCH` payloads in its own Lambda handler reliably. It executes tasks under strict transaction boundaries using 0001 stale worker fencing. Observation writes, worker results, and completion outbox events commit in one atomic transaction. Observation uniqueness uses `ON CONFLICT (execution_id, canonical_url, content_hash)` DO NOTHING for immutable snapshot replay without mutating existing evidence while allowing different URLs with identical `content_hash` to exist as separate attributable observations.
- **AC-16**: Local tests run entirely offline using explicit test-only canned fixture adapters without requiring network access or a live `SERPAPI_API_KEY`.
- **AC-17**: The evidence collector function packages independently into a ZIP archive under 50 MB compressed and 250 MB uncompressed via a dedicated SAM `template.yaml`.
- **AC-18**: SSRF network security acceptance checks confirm that requests to local, link-local, and private IP ranges fail safely before and after HTTP redirects.

## Options considered

### Option 1: Direct in line extraction without provenance persistence

Execute HTTP requests and Scrapling directly inside planning graph nodes, passing extracted dictionary data directly in graph memory without database records.

**Pros**:
- Avoids database schema migrations for observations and evidence junction tables.
- Minimizes round trip database latency during graph execution.

**Cons**:
- Complete loss of factual provenance: citations cannot be verified after execution completes.
- Dual syndication cannot be detected, allowing copied blog content to masquerade as independent verification.
- Replaying or resuming workflows forces duplicate paid API calls to SerpAPI.
- Violates core architectural requirement SH-04 for source provenance and auditability.

### Option 2: Monolithic worker combining collection, LLM interpretation, and verification

Package SerpAPI search, HTML extraction, Bedrock model invocation, and claim verification into a single large worker Lambda function.

**Pros**:
- Fewer total task dispatches across SQS.
- Encapsulates the entire research lifecycle inside one function folder.

**Cons**:
- Blurs module boundaries between untrusted external web retrieval and authoritative claim verification.
- Bundles heavy model invocation dependencies into the collector Lambda package, risking package size limits.
- Holds open Lambda execution time while waiting sequentially for external search, scraping, and LLM completions.
- Violates architectural decision AD-03 that deterministic policy owns verification state transitions.

### Option 3: Decoupled collector worker with immutable provenance, standalone verification helpers, and PostGIS canonical places (Recommended)

Build an independent evidence collector worker Lambda that executes bounded SerpAPI search and Scrapling extraction, computes SHA256 hashes, and writes immutable `source_observations` records. Provide standalone deterministic verification and place resolution helpers in `orchestrator/evidence/` and `orchestrator/places/` that evaluate claims against strict policies, preserve conflicts, and query PostGIS coordinates.

**Pros**:
- Maintains strict provenance: raw observations are immutable and verifiable.
- Enforces deterministic policy gates: generative models propose facts, but policy code transitions verification status.
- Preserves contradictory evidence side by side without destructive data loss.
- Allows the evidence collector worker to package into a lean, fast Lambda ZIP without LLM or heavy geospatial libraries.
- Supports complete offline testing with deterministic fixture adapters.

**Cons**:
- Requires four new database tables and PostGIS extension support.
- Requires orchestrating collection and verification across separate task or graph steps.

## Decision

**Chosen option**: Option 3: Decoupled collector worker with immutable provenance, standalone verification helpers, and PostGIS canonical places.

The evidence collector executes external I/O tasks within strict spend and call limits, storing immutable source observations. Deterministic verification helpers in the orchestrator evaluate claims, maintain freshness lifecycles, and link assertions to PostGIS canonical places.

**Implementation skills**: None used.

## Rationale

This decision directly satisfies the system requirements in REQUIREMENTS.md and TECHNICAL_SPEC.md while protecting the architectural invariants established in spec 0001:

1. **Separation of untrusted retrieval from factual authority**: The web is untrusted. Gathering text must not grant that text immediate authority. By persisting observations first and running verification policies second, the platform ensures that fake news, syndicated promotional blogs, or conflicting forum posts cannot compromise downstream routing or budgeting algorithms.
2. **Preservation of contradictory truth**: In travel, facts conflict constantly. One website may list a museum as open on Mondays while the official portal notes a seasonal renovation closure. Option 3 records both observations and flags the claim as `CONFLICTED`, giving the trip planner and the user the honest ability to clarify or plan precautions.
3. **Budget and operational resilience**: Serverless workers must be lean and bounded. Separating collection into a dedicated Lambda with strict timeout and tool call ceilings prevents external provider latency spikes from blocking the primary workflow.

## Feature design

### Data model sketch

Four new tables support evidence collection, place identity, and verified claims:

```text
source_observations
  id: UUID (PK)
  execution_id: UUID (FK executions.id, CASCADE)
  source_url: Text
  canonical_url: Text (normalized URL)
  publisher: String(128)
  content_hash: String(64) (SHA256 hex digest)
  retrieved_at: DateTime(UTC)
  published_at: DateTime(UTC) (nullable)
  artifact_ref: JSONB (nullable, bucket/key/version/size)
  excerpt_text: Text
  excerpt_locator: String(256) (CSS selector or JSON path)
  extraction_method: String(32) (SERPAPI_SEARCH, SCRAPLING_HTTP, MANUAL_FIXTURE)
  created_at: DateTime(UTC)
  Unique Constraint: (execution_id, canonical_url, content_hash)
  Indexes: (execution_id), (content_hash), (canonical_url)
  Snapshot Invariants: Records are write once immutable snapshots. Replays reuse the same snapshot via ON CONFLICT (execution_id, canonical_url, content_hash) DO NOTHING with zero DO UPDATE mutations. Changed content produces a new snapshot under that URL with a new content_hash. Different URLs with identical content maintain distinct canonical_url attribution.

canonical_places
  id: UUID (PK)
  names: JSONB (array of string names and aliases)
  primary_name: String(256)
  locality: String(128)
  point: Geometry(Point, 4326) (PostGIS point)
  access_points: JSONB (array of secondary coordinates, parking, gates)
  provider_ids: JSONB (key value map, e.g. {"google_place_id": "ChIJ...", "osm_id": "123"})
  geocoder_provenance: String(64) (e.g. google_places, osm, mapbox)
  identity_claim_refs: JSONB (array of UUID claim references)
  access_status: String(32) (PUBLIC, RESTRICTED, UNKNOWN)
  is_candidate: Boolean (True if pending verification, False if verified)
  created_at: DateTime(UTC)
  updated_at: DateTime(UTC)
  Indexes: GiST(point), (locality), (primary_name), Unique on specific provider IDs (e.g. osm_id) for provider identity uniqueness

claims
  id: UUID (PK)
  execution_id: UUID (FK executions.id, CASCADE, nullable)
  subject_id: String(256) (target entity or place identifier)
  place_id: UUID (FK canonical_places.id, SET NULL, nullable)
  category: String(64) (HOURS, FEES, RESTRICTION, CUISINE, ATTRACTION, WEATHER)
  typed_value: JSONB (structured claim data, e.g. {"open": "09:00", "close": "18:00"})
  verification: String(32) (VERIFIED, PARTIALLY_VERIFIED, CONFLICTED, UNVERIFIED)
  availability: String(32) (AVAILABLE, UNAVAILABLE, UNKNOWN)
  freshness: String(32) (FRESH, STALE, UNKNOWN)
  valid_from: DateTime(UTC) (nullable)
  valid_until: DateTime(UTC) (nullable)
  geographic_scope: JSONB (locality, bounding box, or polygon identifier)
  policy_version: String(32)
  confidence: Float (nullable, range 0.0 to 1.0)
  contradictions: JSONB (array of conflicting claim UUIDs and reasons)
  decision_history: JSONB (array of state transitions and reasons)
  created_at: DateTime(UTC)
  updated_at: DateTime(UTC)
  Indexes: (subject_id), (category), (place_id), (verification), (freshness)

claim_evidence
  claim_id: UUID (FK claims.id, CASCADE)
  observation_id: UUID (FK source_observations.id, CASCADE)
  support_type: String(32) (SUPPORTS, REFUTES, PARTIAL)
  locator: String(256) (excerpt locator or snippet pointer)
  created_at: DateTime(UTC)
  Primary Key: (claim_id, observation_id)
  Indexes: (observation_id)
```

### State transitions

Observation and Claim flow:
```text
[Source Observation] (via collector) -> [Typed Claim Proposal] (via orchestrator LLM/Decision task) -> [Deterministic Verification] (via Policy Engine)
```

Claim verification state machine:
```text
[Typed Claim Proposal]
       |
       v
  UNVERIFIED <-----------------------------------------------+
       |                                                     |
       +---> (Authoritative match, valid time/scope) ----> VERIFIED
       |                                                     |
       +---> (Partial support or non authoritative source) -> PARTIALLY_VERIFIED
       |                                                     |
       +---> (Mutually exclusive assertions found) --------> CONFLICTED
```

Exact rules:
- **Support**: A claim transitions to `VERIFIED` only if fully supported by an authoritative source.
- **Authority**: Defined by an explicit domain whitelist (e.g., `*.gov.in` for public transit/closures).
- **Conflict**: Conflicting claims are preserved as `CONFLICTED` rather than silently overwritten.
- **Freshness**: Measured purely by publication date or ephemeral rules.
- **Availability**: Evaluates separately from verification (e.g., `UNAVAILABLE` when a verified source confirms permanent closure).

Claim freshness state machine:
```text
  [Created Claim]
       |
       +---> (Within category validity window) -------------> FRESH
       |                                                      |
       +---> (Validity window elapsed) ---------------------> STALE
       |                                                      |
       +---> (No publication timestamps) -------------------> UNKNOWN
```

Category freshness validity defaults:
- WEATHER: 6 hours (or provider valid until forecast timestamp).
- RESTRICTION (road closures, traffic advisories): 24 hours during travel window; requires explicit travel date match.
- HOURS and FEES: 90 days.
- ATTRACTION identity and locality: 365 days.

Freshness timestamp evaluation priority:
Freshness strictly relies on `published_at` or explicit validity windows. Retrieval time is never treated as publication time. If publication date is missing, freshness is `UNKNOWN`, avoiding false confidence in stale content retrieved recently.

### Task payload and worker interface

The evidence collector executes as an independent AWS Lambda worker, receiving tasks from SQS, reusing 0001 execution support, and committing results through strict lease fencing.

#### 1. Actual TASK_DISPATCH message and database payload mapping

The orchestrator transactional outbox relay dispatches tasks using the canonical `OutboxMessage` serialization from spec 0001.

Actual SQS message body received by the worker consumer:
```json
{
  "event_id": "33333333-3333-4333-8333-333333333333",
  "execution_id": "11111111-1111-4111-8111-111111111111",
  "task_id": "22222222-2222-4222-8222-222222222222",
  "kind": "TASK_DISPATCH",
  "payload": {
    "schema_version": "1.0",
    "execution_id": "11111111-1111-4111-8111-111111111111",
    "task_id": "22222222-2222-4222-8222-222222222222",
    "worker": "EVIDENCE",
    "action": "collect",
    "attempt": 0,
    "context_version": 1,
    "payload": {
      "queries": ["pujor Kolkata traffic advisory 2026", "Dakshineswar temple opening timings"],
      "claim_scope": {
        "locality": "Kolkata",
        "travel_date": "2026-10-20"
      },
      "allowed_sources": ["official", "attributable_local", "permitted_aggregators"],
      "freshness_need": "travel_window",
      "budget": {
        "remaining_tool_calls": 5,
        "remaining_cost_minor": 500,
        "currency": "INR",
        "remaining_iterations": 2
      }
    }
  },
  "attempts": 0,
  "created_at": "2026-10-10T12:00:00Z"
}
```

Task payload mapping and database loading steps:
1. **Message parsing and field extraction**:
   - The Lambda consumer parses the JSON record body and validates `kind == "TASK_DISPATCH"`.
   - **Outbox relay delivery attempts**: Extracted from top level `body["attempts"]`. This represents outbox relay transport retries from `outbox_events.attempts` and is strictly excluded from worker task attempt fencing.
   - **Task execution attempt**: Extracted from `body["payload"]["attempt"]`. This represents the monotonic task execution attempt counter from `tasks.attempt` at dispatch time, used for task claim fencing.
   - **Workflow context version**: Extracted from `body["payload"]["context_version"]`. This represents the workflow checkpoint context version from `executions.context_version` and `tasks.context_version`.
   - **Task and execution IDs**: Extracted from `body["task_id"]` (or `body["payload"]["task_id"]`) and `body["execution_id"]` (or `body["payload"]["execution_id"]`).
   - **Worker role and action**: Extracted from `body["payload"]["worker"]` and `body["payload"]["action"]`.
2. **Fenced claim execution**: The consumer constructs `TaskQueueMessage(task_id=task_id, execution_id=execution_id, attempt=body["payload"]["attempt"])` and invokes `WorkerFencingService.claim_task(message=..., worker="EVIDENCE", action="collect", expected_context_version=body["payload"]["context_version"], lease_seconds=300)`.
3. **Transaction 4 locking**: The fencing service locks the parent row in `executions` with `FOR UPDATE` to verify the execution is not cancelled and not terminal. It then locks the task row in `tasks` with `FOR UPDATE` to verify worker role, action, context version, attempt limits, and active lease expiry. Stale queue messages where `message_attempt < task.attempt` are safely rejected.
4. **Monotonic claim transition**: On a valid claim, `tasks.attempt` increments atomically (`attempt = attempt + 1`), a unique `lease_token` (UUID) is generated, `lease_expiry` is set to `now + 300s`, and `status` transitions to `CLAIMED`.
5. **Database payload source of truth**: The fencing service returns a `TaskClaimResult` containing `task_id`, `execution_id`, `worker`, `action`, `attempt`, `context_version`, `lease_token`, `lease_expiry`, and `payload_ref`. The worker loads task parameters directly from `claim_result.payload_ref`, mapping it to `EvidenceCollectPayload`. The database row remains the single authoritative source of truth for task input parameters, preventing truncation or tampering from message transport.

Canonical database task payload (`tasks.payload_ref` mapped to `EvidenceCollectPayload`):
```json
{
  "queries": ["pujor Kolkata traffic advisory 2026", "Dakshineswar temple opening timings"],
  "claim_scope": {
    "locality": "Kolkata",
    "travel_date": "2026-10-20"
  },
  "allowed_sources": ["official", "attributable_local", "permitted_aggregators"],
  "freshness_need": "travel_window",
  "budget": {
    "remaining_tool_calls": 5,
    "remaining_cost_minor": 500,
    "currency": "INR",
    "remaining_iterations": 2
  }
}
```

Schema valid worker result output (`worker_results.output_ref` mapped to `EvidenceCollectResult`):
```json
{
  "observations": [
    {
      "observation_id": "550e8400-e29b-41d4-a716-446655440000",
      "canonical_url": "https://example.com/fixture/advisory2026",
      "publisher": "example.com",
      "content_hash": "fixture_hash_a1b2c3d4e56789abcdef0123456789abcdef0123456789abcdef01234567",
      "retrieved_at": "2026-10-10T12:00:00Z",
      "excerpt_text": "Central Avenue closed for heavy vehicles from 16:00 to 04:00.",
      "extraction_method": "SCRAPLING_HTTP"
    }
  ],
  "unavailable_sources": [],
  "browser_requests": [],
  "usage": {
    "tool_calls_made": 2,
    "cost_minor": 150,
    "currency": "INR"
  },
  "budget_exhausted": false
}
```

#### 2. Collector queue routing and permitted execution support imports

Queue routing architecture:
- **Dedicated worker queue**: The evidence collector Lambda listens to `evidence-collector-task-dispatch-${Environment}` with a dead letter redrive policy targeting `evidence-collector-task-dlq-${Environment}` after 5 receive attempts.
- **Outbox relay routing**: The 0001 outbox relay publishes `TASK_DISPATCH` events destined for `worker == "EVIDENCE"` to the evidence collector queue URL configured in `EVIDENCE_COLLECTOR_QUEUE_URL`.
- **Partial batch failure handling**: The Lambda handler processes SQS batches concurrently using `asyncio.gather` and reports individual failures via `batchItemFailures: [{"itemIdentifier": message_id}]`, matching the accepted 0001 consumer pattern.

Permitted execution support imports from 0001:
- `functions.orchestrator.execution.fencing`: `WorkerFencingService`, `TaskQueueMessage`, `TaskClaimResult`, and fencing exception classes (`TaskClaimConflictError`, `TaskLeaseActiveError`, `TaskAlreadyTerminalError`, `StaleQueueMessageError`, `DeadlineExceededError`, `ParentCancelledError`, `LeaseFencingError`).
- `functions.orchestrator.execution.models`: `ExecutionModel`, `TaskModel`, `WorkerResultModel`, `OutboxEventModel`.
- `functions.orchestrator.execution.job_events`: `record_job_event`.
- `config.db`: `get_session_factory`.
- `contracts.evidence`: `EvidenceClass`, `HotelObservation`, `FareObservation`.
- `contracts.money`: `Currency`, `Money`.

Strictly forbidden imports:
- The evidence collector never imports orchestrator evidence verification or policy modules (`functions.orchestrator.evidence.*`).
- The evidence collector never imports canonical place resolution or spatial repositories (`functions.orchestrator.places.*`).
- The evidence collector never imports orchestrator graph runners, checkpointers, or workflow state machines (`functions.orchestrator.graphs.*`, `functions.orchestrator.runner.*`, `functions.orchestrator.checkpointer.*`).
- The evidence collector never imports browser automation or Playwright runtimes (`functions.browser.*`).
- Orchestrator modules never import evidence collector business code (`functions.evidence_collector.*`).

Packaging and staged file layout:
- The evidence collector packages through its own SAM `template.yaml` located at `backend/src/functions/evidence_collector/template.yaml`.
- During packaging, only the collector code (`functions/evidence_collector/`), database configuration (`config/`), shared contracts (`contracts/`), and execution support files (`functions/orchestrator/execution/{fencing.py, models.py, job_events.py}`) are staged.
- Heavy dependencies including LangGraph, GeoPandas, Shapely, and Playwright are excluded from the collector `requirements.txt`. This keeps the deployment artifact well below 50 MB compressed and 250 MB uncompressed.

#### 3. Retrieval uniqueness, replay behavior, and fenced commit boundaries

Retrieval identity versus content duplication:
- A URL represents retrieval identity (where and when data was gathered). A content hash represents payload identity (what text was returned).
- Two different URLs often host identical text, such as syndicated articles, copied festival schedules, or duplicate fare tables. Both URLs must be retained to maintain factual provenance and detect syndication.
- Uniqueness constraint on `source_observations`:
  `UNIQUE (execution_id, canonical_url, content_hash)`
- Preserving different URLs: Two rows with distinct `canonical_url` values and identical `content_hash` values insert successfully without conflict, ensuring separate attribution.
- Preserving immutable snapshots: Every observation record is write once and immutable. No `DO UPDATE` of evidence is ever performed.
- Changed content snapshots: If the content at a URL changes on a subsequent retrieval, the new `content_hash` forms a distinct row under that `canonical_url`, preserving both the historical snapshot and the new observation.
- Syndication index: A non unique B tree index on `content_hash` (`ix_source_observations_content_hash`) allows the downstream Policy Engine to identify identical content across different publishers and flag non independent corroboration.

Replay behavior and snapshot reuse:
- If a task attempt is retried or redispatched following a network timeout, observation inserts use `ON CONFLICT (execution_id, canonical_url, content_hash) DO NOTHING`. This makes retries idempotent and reuses the existing logical retrieval snapshot without modifying existing records.
- If an unexpired observation for `(canonical_url, content_hash)` already exists for this `execution_id` from an earlier task within its category freshness window, the collector reuses the persisted snapshot instead of making duplicate external HTTP calls.

Fenced atomic commit boundaries and observation rollback:
- Network operations (search queries and HTTP page scrapes) execute outside database transactions. No database connection or row lock is held during external network calls.
- Once retrieval completes, the worker opens one single database session (`async with session_factory() as session:`) to persist results:
  1. The worker stages all collected `source_observations` inserts within the session transaction.
  2. The worker calls `fencing_service.complete_task(session=session, ...)`.
  3. Inside `complete_task`, the parent `executions` row and child `tasks` row are locked `FOR UPDATE` to verify the execution is uncancelled, unexpired, and that `lease_token` matches an active lease.
  4. If fencing succeeds, `complete_task` inserts `worker_results`, updates `tasks` to `COMPLETED`, inserts the `TASK_COMPLETED` outbox event, records the job event, and issues `await self._session.commit()` as its final step. This commits both the staged `source_observations` and the completion records atomically.
  5. If `complete_task` rejects fencing (due to an expired lease, stolen lease token, execution cancellation, or passed deadline), it raises an exception (`LeaseFencingError`, `DeadlineExceededError`, `ParentCancelledError`) before reaching its internal commit.
  6. The raised exception propagates to the worker error handler, which rolls back the session (`await session.rollback()` or session context manager exit on error). Because `complete_task` never committed, all pending `source_observations` records staged in that session roll back completely in PostgreSQL. No orphaned observations or snapshots are ever committed from a stale or expired attempt.

#### 4. Budget accounting, retries, cancellation, and deadlines

Budget accounting in Slice 1 versus deferred metered dispatch:
- Local attempt iteration caps: In Slice 1, the worker evaluates `tasks.payload_ref["budget"]` (`remaining_tool_calls`, `remaining_cost_minor`) as a local iteration cap for the current attempt. In memory counters (`calls_made`, `cost_minor_spent`) bound loop iterations within the Lambda execution to prevent runaway processing. They are local attempt guards and must not be called durable reservations.
- Usage recording: Upon task completion, `worker_results.usage` records actual expenditure (`{"tool_calls_made": N, "cost_minor": M, "currency": "INR"}`). The orchestrator execution engine reconciles this usage against the parent `executions.budget` in Transaction 6.
- Deferred metered dispatch (AC-14): Metered dispatch, rate limiting, and durable pre call budget reservations across concurrent tasks are explicitly deferred to Slice 3. In Slice 3, live SerpAPI calls will atomically reserve budget from `executions.budget` prior to provider dispatches and reconcile unused reservations on failure or retry.

Retries and attempt fencing:
- Transient network failures during collection trigger `WorkerFencingService.fail_task(...)`, inserting a `worker_results` record with status `FAILED` and returning the task to `PENDING` with an exponential backoff delay.
- The `attempt` counter increments strictly upon successful `claim_task`, never during failure recording or recovery polling.
- Reaching `max_attempts` (default 3) marks the task permanently `FAILED` and emits a failure outbox event.

Cancellation handling:
- `claim_task` checks `executions.status == "CANCELLED"` and `executions.cancelled_at`. If cancelled, it raises `ParentCancelledError` and skips execution.
- During multi query loops, before each external call, the worker checks parent execution status. If cancelled, it halts further external calls, marks output as cancelled or partial, and exits.

Deadline enforcement:
- Execution and task deadlines: `claim_task` verifies that both `executions.deadline_at` and `tasks.deadline_at` are in the future.
- Pre call deadline check: Before each external call, the worker verifies `now + provider_timeout < task.deadline_at`. If the deadline is approaching, external calls stop and partial results return.
- Lambda execution guard: The consumer checks `context.get_remaining_time_in_millis() < 3000`. If remaining execution time is under 3 seconds, it halts work and reports a partial batch failure to let SQS redrive safely rather than suffering an unhandled Lambda timeout.

Test fixture isolation:
- Test fixture adapters (`FixtureSearchAdapter`, `FixtureWebExtractionAdapter`) are strictly enabled via explicit configuration flags or test harness injection.
- Test fixtures are strictly test only and never used as production fallbacks. When live API budgets expire or network calls fail in production, the worker returns partial observations or records failures; it never injects synthetic canned test fixtures into live traveler workflows.

### Value sourcing

| Action | Value produced or displayed | Source |
|---|---|---|
| Search query execution | Organic search items and snippets | SerpAPI HTTP response or local fixture JSON |
| Page scrape | Extracted text excerpts and rates | Scrapling CSS selector extraction from HTTP body |
| Observation persistence | `content_hash` | SHA256 hex digest of raw extracted payload |
| Observation persistence | `canonical_url` | Normalized URL stripping tracking queries |
| Observation persistence | `retrieved_at` | Current system UTC instant at retrieval time |
| Place resolution | `point` coordinates | PostGIS `ST_SetSRID(ST_MakePoint(lng, lat), 4326)` from geocoded provider data |
| Nearby place discovery | Annular distance match | `ST_DWithin(point::geography, origin::geography, max_m)` and distance >= min_m |
| Claim evaluation | `verification` status | Deterministic Policy Engine applying domain authority rules |
| Claim evaluation | `freshness` status | Clock comparison against category max age policy |
| Budget enforcement | `budget_exhausted` | Comparison of calls made against `budget.remaining_tool_calls` |

### Key invariants

1. **Indicative extractions never masquerade as live offers**: Rates and fares extracted from aggregators remain labeled `INDICATIVE_SEARCH` per contract invariant.
2. **Strict decimal non negative money**: Unparseable prices or missing fee values are recorded as unknown; they are never coerced to zero.
3. **Immutable source observations**: An observation record is write once; it cannot be modified or updated in place.
4. **Non destructive conflict handling**: When two observations contradict each other, both claims are preserved with status `CONFLICTED`; neither is discarded.
5. **Generative models never grant verification**: Bedrock or Nova model outputs are treated as proposed claims; only the deterministic Policy Engine can mark a claim `VERIFIED`.
6. **Critical closures require authoritative domains**: Festival street closures and safety warnings require authoritative government, municipal, or police sources to reach `VERIFIED`. The authority domain whitelist is configured in `backend/src/functions/orchestrator/evidence/policy.py` containing domains matching `*.gov.in`, `*.nic.in`, `*.police.gov.in`, and verified official event portals.
7. **Spatial and candidate matching integrity**: All coordinates stored in `canonical_places` use WGS 84 (`EPSG:4326`) with longitude between -180 and 180 and latitude between -90 and 90. Candidate place matching combines PostGIS `ST_DWithin` spatial proximity (distance <= 100 meters) with normalized name token similarity (threshold >= 0.8) in Python, without requiring external database text extensions.

### Security model

1. **Owner scoping**: All queries and mutations check the active execution owner; observations and claims associated with private trips never leak to cross owner sessions.
2. **Server side request forgery prevention (SSRF)**: The Scrapling HTTP fetcher validates target URLs against strict protocols (`http`, `https`). It resolves DNS records before fetching, rejecting localhost, link local (169.254.169.254), and private IP ranges (RFC 1918) both initially and across any HTTP redirects. Redirects are capped (e.g., max 3), and a maximum download size limit of 5 MB is enforced for untrusted content.
3. **Credential isolation**: `SERPAPI_API_KEY` is retrieved from environment variables and never logged, returned in API payloads, or stored in database tables.

### Configuration required

- `DATABASE_URL`: PostgreSQL connection string with asyncpg dialect and PostGIS extension.
- `SERPAPI_API_KEY`: API key for SerpAPI search provider (optional in local fixture test mode).
- `SERPAPI_BASE_URL`: Base URL for SerpAPI requests, defaulting to `https://serpapi.com`.
- `HTTP_REQUEST_TIMEOUT_SECONDS`: Request timeout for external fetches, defaulting to 20 seconds.
- `MAX_SOURCE_PAYLOAD_BYTES`: Maximum allowable download size for scraped pages, defaulting to 5242880 (5 MB).

### Critical test scenarios

- Happy path search: Executing a multi query search with the SerpAPI client returns normalized observations, verifies **AC-1**, **AC-3**.
- Happy path page scrape: Extracting hotel and transit listings using Scrapling CSS selectors parses rates safely, verifies **AC-2**, **AC-3**.
- Content deduplication: Collecting duplicate or syndicated articles links existing records and flags non independent corroboration, verifies **AC-4**.
- Spatial point insert and query: Inserting canonical places creates valid PostGIS points and `ST_DWithin` queries filter 2000 to 3000 meter bands correctly, verifies **AC-5**, **AC-6**, **AC-7**.
- Deterministic verification: Authoritative police advisory verifies festival road closure; secondary blog keeps it unverified, verifies **AC-8**, **AC-9**, **AC-10**.
- Conflict preservation: Two divergent opening hours assertions result in two `CONFLICTED` claim records cross referenced in contradictions, verifies **AC-11**.
- Freshness expiration: A claim past its category validity window transitions to `STALE`, verifies **AC-12**.
- Dynamic page escalation: A page requiring JavaScript emits a `browser_requests` token without invoking a browser, verifies **AC-13**.
- Budget exhaustion: A task with a budget of 1 call stops after 1 query and returns partial results, verifies **AC-14** under Slice 3 live provider testing.
- 0001 worker execution: An SQS task dispatch claiming an `EVIDENCE.collect` task runs through `EXECUTOR_REGISTRY`, completes, and commits results to PostgreSQL, verifies **AC-15**.
- Offline test isolation: Complete test suite passes with zero network access using fixture adapters, verifies **AC-16**.

## Build plan and requirement-to-test mapping

The build plan establishes a thin end to end thread across persistence, adapter, policy, and worker dispatch, separating offline fixtures from live-provider smoke tests and SAM builds.

```text
1. [x] [Slice 1: Offline collection and persisted observations through durable execution completion]
   - Write Alembic migration for `source_observations` with `UNIQUE (execution_id, canonical_url, content_hash)` constraint and `content_hash` index.
   - Implement explicit test only fixture search and extraction adapters with SHA256 content hashing (strictly avoiding production fallback).
   - Setup dedicated evidence collector SQS Lambda consumer reusing permitted 0001 execution support (`WorkerFencingService`, `models.py`, `job_events.py`, `config/db.py`) without importing orchestrator evidence policy, canonical places, or graph modules.
   - Implement `EvidenceCollectorService` executing the atomic completion boundary committing `source_observations`, `worker_results`, `tasks`, and `outbox_events` in one database transaction under 0001 lease fencing, with clean observation rollback on fencing rejection.
   - Implement attempt limits, cancellation checks, deadline verification, and local invocation iteration bounds.
   - Slice 1 acceptance boundary: Confirms offline collection, persisted source observations, and durable task completion through 0001 fencing; strictly excludes claim verification, place resolution, and metered live dispatch.
   - Satisfies AC-3, AC-15, AC-16 (Tests: test_fixture_isolation, test_retry_persistence, test_fenced_completion_boundary).

2. [x] [Slice 2: Claim and place persistence models]
   - Write Alembic migration for `canonical_places`, `claims`, and `claim_evidence`.
   - Implement SQLAlchemy ORM models and Pydantic schemas.
   - Verify on live PostgreSQL with PostGIS extension.
   - Satisfies AC-5, AC-8 (Test: test_persistence_layer).

3. [x] [Slice 3: Live provider adapters]
   - Implement SerpAPI HTTP client adapter with rate limiting and timeout handling.
   - Implement Scrapling extraction adapter for permitted web sources with safe price parsing.
   - Implement metered dispatch and durable pre call budget reservations across concurrent tasks.
   - Implement browser escalation request emission (NEEDS_BROWSER).
   - Satisfies AC-1, AC-2, AC-4, AC-13, AC-14, AC-18 (Tests: test_serpapi_adapter, test_scrapling_parser, test_worker_budget_exhaustion, test_ssrf_protection).

4. [x] [Slice 4: Canonical place resolution service]
   - Implement `CanonicalPlacesRepository` with PostGIS spatial operations and geocoder provenance.
   - Implement candidate resolution logic with 100 meter proximity and ambiguous place detection.
   - Implement annular discovery band queries (ST_DWithin).
   - Satisfies AC-6, AC-7 (Tests: test_canonical_places_spatial, test_ambiguous_resolution).

5. [x] [Slice 5: Deterministic Policy Engine]
   - Implement `ClaimVerificationPolicy` with domain authority rules.
   - Implement category freshness evaluator strictly relying on publication dates.
   - Implement conflict detection and mutual contradiction cross referencing.
   - Satisfies AC-9, AC-10, AC-11, AC-12 (Tests: test_policy_authority, test_freshness_rules, test_conflict_preservation).

6. [x] [Slice 6: SAM packaging and verification suites]
   - Create `backend/src/functions/evidence_collector/template.yaml` and `requirements.txt`.
   - Validate SAM build and packaging size within Lambda limits.
   - Write unit, integration, and E2E trace tests separating offline fixture tests, live-provider smoke tests, SAM builds, and deferred AWS deployment.
   - Includes a final "connected source-backed evidence scenario."
   - Satisfies AC-17 (Tests: test_sam_package_size, test_e2e_connected_source).
```

## Consequences

**Positive**:
- Full factual auditability: Every claim in the platform links directly to immutable source observations.
- Hallucination prevention: Generative models can no longer assert unverified truths directly into state.
- Resilient to web churn: Conflicting or stale information is represented truthfully rather than causing silent corruption.
- Cost control: External tool call limits and spend caps prevent runaway billing.

**Negative / tradeoffs**:
- Increased database storage: Storing raw text excerpts and observation records increases PostgreSQL table growth over time.
- Spatial dependency: Requires PostGIS extension installed and active on the database engine.
- Latency overhead: Multi step collection, hashing, and verification adds seconds to asynchronous research jobs.

**Neutral**:
- Requires periodic vacuuming and maintenance of spatial GiST indexes as canonical places accumulate.

## Follow-up

- [ ] Configure PostGIS extensions in local development Docker compose and verify Aiven PostgreSQL compatibility.
- [ ] Establish initial domain authority whitelist for Indian municipal and traffic authorities (e.g. Kolkata Traffic Police, Delhi Traffic Police).
- [ ] Review retention and cleanup schedules for historical `source_observations` older than 180 days.

## References

**Project sources**:
- `docs/DEVELOPMENT.md`: Phase 4 evidence collector and canonical place claim verification guidelines.
- `docs/ARCHITECTURE.md`: Component ownership, module boundaries, and architectural decisions AD-03, AD-04, AD-07.
- `docs/TECHNICAL_SPEC.md`: Section 2 domain models, Section 4 database schema, and Section 5 evidence protocols.
- `docs/reference/DATABASE.md`: Application table baseline and PostGIS spatial indexing rules.
- `docs/specs/0001-orchestrator-execution-engine.md`: Accepted execution engine, worker fencing, and `EXECUTOR_REGISTRY` integration.
- `backend/src/contracts/evidence.py`: Domain contracts for `EvidenceClass`, `HotelObservation`, and `FareObservation`.

**Practices & standards**:
- PostGIS spatial indexing with GiST and `ST_DWithin` geography distance queries.
- Immutable append only observation logs for auditability and provenance.
- Defensive SSRF mitigation on web fetchers (RFC 1918 and link local IP blocking).
- Strict decimal arithmetic for financial values to prevent floating point rounding errors.
