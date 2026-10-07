# Database reference — persistence owner

## Storage boundaries
This file owns planned application persistence, relationships, constraints and recovery transactions. [TECHNICAL_SPEC.md](../TECHNICAL_SPEC.md) owns internal object schemas; [API.md](API.md) owns HTTP behavior. These are proposed migrations, not applied tables.

PostgreSQL is durable application storage; PostGIS stores public place coordinates and supports nearby queries. S3 stores raw pages, videos, audio, frames and large result artifacts. LangGraph's selected PostgreSQL adapter owns checkpoint migrations. Cache storage is optional and expendable; losing it must not lose execution state. Do not put model weights or media blobs in database rows.

## Application table baseline

Implement migrations in Phase 1; field definitions below are minimums, not claims of applied SQL. UUID IDs; timestamptz UTC; jsonb only for versioned typed payloads. Foreign keys and unique keys enforce logical identity. Owner filters are mandatory even when UUID is known.

| Table | Key fields | Relations and indexes |
|---|---|---|
| users | id, auth_subject, issuer, created_at | Unique (issuer,auth_subject); no password table assumed |
| trips | id, owner_id, brief, current_plan_version, created_at | FK users; index (owner_id,created_at) |
| executions | id, owner_id, trip_id nullable, workflow/scope, status, context_version, checkpoint_id, deadline_at, budget, cancelled_at, lease_token/expiry | FK owner/trip; index (owner_id,created_at), partial active/deadline index |
| tasks | id, execution_id, parent_task_id, worker/action, payload_ref, input_hash, idempotency_key, attempt, context_version, status, lease_token/expiry, deadline_at, result_id | FK execution/parent; unique (execution_id,idempotency_key); index pending+deadline/execution |
| worker_results | id, task_id, attempt, context_version, status, output_ref, warnings, error, usage, timestamps | FK task; unique (task_id,attempt); accepted pointer set only by fenced attempt |
| outbox_events | id, execution_id, task_id nullable, kind, payload_ref, due_at, attempts, delivered_at | Unique id; partial due/delivery index |
| completion_receipts | event_id, task_id, attempt, result_id, received_at, applied_at | Unique event_id; index task/result; duplicate processing has no second transition |
| source_observations | id, execution_id, source/canonical URL, publisher, hash, timestamps, artifact_ref, locator, extraction_method | FK execution; index hash/canonical URL; retrieval time is not publication time |
| canonical_places | id, names/aliases, locality, point, access_points, provider IDs, identity claim refs | PostGIS geometry(Point,4326), GiST point; unique provider namespace+ID when supplied |
| claims | id, subject/place, category, typed value, verification, freshness, validity interval, scope, policy_version | FK place optional; indexes subject/category/validity; contradictory claims preserved |
| claim_evidence | claim_id, observation_id, support_type, locator | Composite PK; FKs claim and observation |
| media_artifacts | id, owner_id, execution_id, object_key/version, hash, mime,size, duration, timestamps, expires_at | FK owner/execution; unique owned key/version; expiry index |
| media_analyses | id, execution_id, artifact_id, observation_refs, candidates, outcome | FK artifact/execution; index execution |
| route_versions | id, route_family_id, execution_id, owner_id, version, previous_id, request, restriction_snapshot, result_ref | Unique (route_family_id,version); index owner/execution |
| trip_plans | id, trip_id, execution_id, version, result_ref, assessment | Unique (trip_id,version); immutable historical plans |
| job_events | id, execution_id, sequence, event_type, safe_payload, timestamp | Unique (execution_id,sequence); cursor query index |

Private observations/claims carry execution/owner linkage; canonical global places contain only public reviewed identity data. Never promote a user upload/GPS/private claim into a public cache.

Use provider-cache tables only if necessary; Redis cache loss is safe. Raw large content stays in S3. Selected LangGraph PostgreSQL adapter owns checkpoint-table migrations; pin its version. Do not hand-invent incompatible adapter table schemas.

Record accepted result IDs in graph state. If checkpoint storage and custom execution tables do not share one transaction, recovery reconciles persisted results into an idempotent graph update; never assume atomicity without proving it. Migrations run once in a serialized job before dependent deploys; no destructive table replacement as a routine refactor.


## Additional identity and concurrency records
| Table | Fields and constraints | Purpose |
|---|---|---|
| routes | id, owner_id, current_version, created_at; owner FK | Stable route family represented by public route_id; route_versions.route_family_id references routes.id. |
| api_idempotency | owner_id, operation, key, request_hash, response_json, expires_at; unique owner/operation/key | Durable retries without duplicate trip/job creation. Never store presigned URL past its expiry as an indefinitely reusable response. |
| budget_reservations | id, execution_id, task_id, estimated_cost_minor, actual_cost_minor nullable, currency, status; unique task_id | Reserve before parallel dispatch, settle/release once, prevent fan-out overspending. |

These supporting records close the API/replan/budget invariants; they do not introduce additional Lambda functions. Implement them with the owning modules' migrations/repositories.

## Column and relationship rules
- IDs use UUID; dates use date, instants timestamptz, versions/attempts positive integers. Money is bigint minor units plus currency. Size/duration/distance must be nonnegative; coordinates are validated before insert.
- Executions belong to users; tasks belong to executions; results belong to task attempts. Trip plans reference both trip and execution. Enforce matching owner for cross-record writes in one transaction; a foreign key alone does not prove ownership.
- source_observations and claims remain owner/execution scoped. claim_evidence links immutable evidence to assertions without flattening contradictory sources. Evidence links must not cross private owners.
- canonical_places store reviewed public identity only. Names are not unique. Provider IDs need their own namespace/id unique mapping when multiple providers identify one place. Duplicate place identities require review rather than destructive merging.
- JSONB holds schema-versioned typed bodies, not arbitrary unvalidated dictionaries. Keep searchable identity/status/version/deadline/owner columns outside JSONB.
- Artifact references retain bucket/key, object version when used, hash, MIME, size and ownership. An expired/deleted artifact has a tombstone; it is never silently interpreted as an empty result.
- tasks.result_id starts null. Insert the result before setting the accepted pointer in a fenced completion transaction. Create cyclic foreign keys in a subsequent migration or use deferred checks; never disable integrity globally.

## Required indexes
Use B-tree indexes for owner/time listings, execution task lookup and cursor sequences; partial indexes for undelivered outbox events and active lease/deadline scans. Add GiST on canonical_places.point. Test query plans with realistic data rather than indexing every JSON field.

For precise 2–3 km or 5–7 km suggestions, query meters with `ST_DWithin(point::geography, query_point::geography, radius_m)` and a matching geography expression index if that is the chosen access pattern. Filter an annular band by both minimum and maximum distance. Nearby straight-line distance is not road/walking distance; calculate accessible travel distance with the routing adapter afterward.

Unique identities: issuer+auth_subject, execution+task idempotency key, task+attempt, completion event ID, route family+version, trip+plan version and execution+event sequence. Treat null provider IDs carefully; do not let incomplete identities collide.

## Critical transactions and recovery
| Operation | Atomic writes / checks |
|---|---|
| Accept job | Validate owner and idempotency key; persist execution and response identity; return only after commit. |
| Dispatch task | Fence execution lease/context; reserve budget; insert task and dispatch outbox row. Publish after commit. |
| Complete worker | Fence task attempt/lease; persist result and completion outbox row. Ack only after durable commit. |
| Apply completion | Insert deduplication receipt; check attempt/context/cancellation; update accepted pointer and execution progress. Replay has no second side effect. |
| Replan | Lock/check routes.current_version; insert immutable route version and advance pointer using compare-and-swap. Keep completed history. |
| Answer user | Check pending question and context version; persist accepted answer/event once; resume through durable scheduling. |
| Cancel | Fence execution transition; record cancellation and prevent new dispatch/success publication. |
| Settle budget | Transition reservation once; reconcile actual usage; record overruns rather than concealing them. |

The queue is at-least-once. Outbox relay can publish twice; consumers must deduplicate. Use lease tokens plus attempt/context fencing so stale workers cannot overwrite newer results. Timeouts alone do not prove ownership of a lease. Sequence allocation for job_events is transactional per execution.

Checkpoint and application commits may be separate. Persist accepted result IDs and use a reconciliation process to replay missing graph transitions idempotently. Prove shared-transaction support before relying on it. Never hold a database transaction open while waiting for a model, search API or browser.

## Repository and migration ownership
Each function/module owns its repositories and queries under `backend/src/functions/<module>/`; shared `backend/src/config/db.py` only manages connectivity/configuration. API owns HTTP/idempotency/owner reads; orchestrator owns executions/tasks/outbox/receipts/budgets and graph reconciliation; evidence owns observations and evidence persistence; media modules own their artifact/analysis writes; optimizer owns immutable route results. Shared table access uses explicit contracts, not a global repository full of unrelated queries.

Keep migration fragments with their owning module and run a single ordered migration manifest in serialized CI/CD. Deploy compatible additive schema before code; destructive migration requires explicit data/backfill/rollback review. Do not run competing migrations in worker cold starts. Pin checkpoint adapter versions and use its supported setup/migration process.

## Retention, deletion and isolation
Retention durations are configuration requiring review before production; no unsupported permanent retention promise. Media/raw-page expiry deletes object data and updates tombstones. Preserve safe result provenance only within the approved retention window. Deleting a user cancels active work and removes owned records/artifacts through an auditable resumable process; private rows must not become orphaned public data.

Use least-privilege database roles and mandatory owner predicates. If row-level security is enabled, test owner context reset with pooled connections and worker access; RLS supplements correct application queries. Secrets stay outside SQL/logs. Backup/restore tests must include database rows and referenced object availability.

## Validation gate
Test migrations from empty DB and previous released schema; unique/FK/check failures; cross-owner references; concurrent idempotency/replans; duplicate and stale completions; cancellation race; outbox crash recovery; expired artifacts; nearby distance bands; checkpoint reconciliation; backup restore. Use real PostgreSQL/PostGIS integration tests, not SQLite substitutes for these features. No migration, database performance or recovery test is claimed complete by this document.
