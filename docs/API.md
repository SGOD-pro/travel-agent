# API reference — public HTTP contract owner

## Read this first
This is the planned V1 interface, not a claim that endpoints are implemented. This file owns HTTP paths, request handling, response envelopes and transport errors. [TECHNICAL_SPEC.md](../TECHNICAL_SPEC.md) owns reusable domain objects and worker messages; implement one Pydantic definition per object and generate OpenAPI and frontend types from it. Never maintain separate handwritten frontend DTOs. [DATABASE.md](DATABASE.md) owns persistence invariants.

FastAPI exposes public routes; independent worker Lambdas receive internal typed tasks, not public HTTP requests. The API validates and persists an accepted job before returning. LangGraph executes asynchronously. A job ID is the execution ID exposed to clients.

## Endpoint inventory — asynchronous, owner-scoped

Prefix /api/v1. JSON contracts derive from section 2 / Pydantic OpenAPI. Stable success/error fixtures must be generated during Phase 0. Workers never expose these routes.

| Endpoint | Input / output |
|---|---|
| GET /health | Liveness only; no secrets/dependency details |
| POST /uploads | Authenticated upload metadata → owned object key and short-lived presigned URL |
| POST /trips | TripBrief → persisted trip_id; no research starts yet |
| POST /trips/{trip_id}/plans | Accepted planning options → 202 {job_id, status_url} |
| POST /discovery/media | MediaRequest → 202 job |
| POST /optimization/routes | RouteRequest → 202 job |
| POST /optimization/routes/{route_id}/replans | Expected route_version, current position, completed stops and update → 202 job; version conflict 409 |
| GET /jobs/{job_id} | State, progress, missing inputs, result reference and safe errors |
| POST /jobs/{job_id}/responses | Question ID + expected context_version + typed answer → resume; stale response 409 |
| POST /jobs/{job_id}/cancel | Idempotent cancellation request; expose cancellation status |
| GET /jobs/{job_id}/events?cursor=... | Paginated persisted events, next_cursor; polling initially, no assumed persistent SSE connection |
| GET /jobs/{job_id}/result | Typed completed/partial result; signed artifact URLs only after ownership checks |

Creation/replan POST uses Idempotency-Key scoped to owner+action and canonical input hash; conflicting body reuse returns 409. Missing auth 401; unauthorized resource access uses a consistent 404/403 policy; malformed input 422; rate limit 429 with Retry-After; provider unavailable becomes job failure/partial rather than a fabricated 200 result. Poll interval uses server-supplied retry_after_seconds. Frontend distinguishes pending/waiting-user/partial/infeasible/unavailable outcomes.

A successful job response proves neither booking nor price lock. Compound capability requests are explicit accepted scope; the API stores it so orchestrator does not infer acceptance from its own suggestion.



## Request conventions
- `/health` is outside the version prefix; all other inventory paths are under `/api/v1`.
- Authenticate with `Authorization: Bearer <token>` using the configured issuer/audience. Derive owner identity server-side; do not accept `owner_id` from a client.
- IDs are UUIDs. Timestamps are RFC 3339 with an offset; retain an IANA timezone for local schedules and overnight windows. GeoJSON uses longitude then latitude. Distances are meters and durations seconds.
- Money uses an explicit currency and integer minor units; unknown amounts are null, never fabricated zero. Fuel inputs and calculation follow the domain contract.
- Reject unknown fields and unsupported schema versions. Validate lengths, coordinate ranges, stop counts, media size and configured execution limits before accepting work.
- Every mutating request uses JSON except the direct object-store upload. Never put credentials in a URL.

## Endpoint details
| Operation | Required input | Success / behavior |
|---|---|---|
| Upload grant | filename, declared content_type, size_bytes; optional sha256 | 201: upload_id, upload_url, required_headers, expires_at. Server creates an owned media_artifact record. Uploaded bytes are rechecked before extraction; a grant is not proof of valid content. |
| Create trip | TripBrief | 201: trip_id, created_at. Persist only; research requires the plans endpoint. |
| Start trip plan | accepted_capabilities and planning options; optional expected plan version | 202 job. Travel research covers sights, food, nearby discoveries, weather suitability, precautions, route and budget according to accepted scope. |
| Discover media | MediaRequest: owned upload_id resolved to ArtifactRef by server, or permitted source URL; optional captions, hashtags, estimated locality | 202 job. A failed URL extraction can request an upload; do not promise Instagram access. |
| Optimize route | RouteRequest, including place references/names plus locality, departure/timezone, travel modes and objective | 202 job. Resolve uncertain identities before solving; return a question when needed. |
| Replan | expected route_version, current_position, completed_stop_ids and changed restriction/stop data | 202 job. Freeze completed history, recompute the unfinished schedule and preserve old route versions. |
| Get job | job_id | 200: status, context_version, progress, missing_inputs, safe warnings/error, result_available, retry_after_seconds. |
| Answer question | question_id, expected context_version, typed answer | 200: job_id, status, context_version. Answer must match an outstanding question; accepted exactly once. |
| Cancel | job_id; optional reason | 200: job_id, status. Repeated cancel is safe. Already terminal jobs retain their terminal state. |
| Read events | opaque cursor; limit defaults 50, bounded 1–100 | 200: events, next_cursor. Each event has sequence, type, timestamp and safe payload. Polling reconnects without losing persisted events. |
| Read result | job_id | 200 typed result for a terminal job with an available result; 409 RESULT_NOT_READY otherwise. Terminal failure without a result returns the stored safe error. |

`MediaRequest` public upload_id is converted to the internal owned ArtifactRef; clients cannot nominate arbitrary bucket keys. A permitted URL is checked for SSRF, redirects, content type and access permission. Domain fields for TripBrief, RouteRequest and result objects remain defined in the technical spec.

### Asynchronous acceptance example
```json
{
  "job_id": "11111111-1111-4111-8111-111111111111",
  "status": "QUEUED",
  "status_url": "/api/v1/jobs/11111111-1111-4111-8111-111111111111"
}
```
Job lifecycle and domain outcome are different: a successfully completed execution may return an INFEASIBLE route or ambiguous media discovery. Persisted lifecycle values and transitions follow DIAGRAMS.md and the executable execution contract. Progress represents completed work, not a fabricated countdown.

## Idempotency and concurrent updates
Require `Idempotency-Key` on upload/trip/job creation and replan. Persist owner, operation, key, canonical request hash and response in a transactional idempotency record. Same key and same input return the original response; different input returns 409 IDEMPOTENCY_CONFLICT. Network retry must not create another paid job. Retention is configurable and must exceed the advertised retry window.

Questions use context_version; replans use route_version. Check versions transactionally with writes. A stale answer or replan returns 409 VERSION_CONFLICT with the current version, after ownership validation. Never merge incompatible updates silently. Cancellation and completion compete under a fenced execution transition; cancellation must not be undone by late worker results.

## Error envelope
```json
{
  "error": {
    "code": "VERSION_CONFLICT",
    "message": "The route changed. Refresh before replanning.",
    "retryable": false,
    "details": {"current_version": 3}
  },
  "request_id": "33333333-3333-4333-8333-333333333333"
}
```
| HTTP status | Meaning |
|---|---|
| 401 | Missing, invalid or expired authentication |
| 404 | Missing or another owner's resource; use this consistently to avoid disclosure |
| 409 | Idempotency/version conflict, invalid transition or result not ready |
| 413 | Request/media exceeds configured size |
| 422 | Invalid fields, unsupported mode/schema or impossible input syntax |
| 429 | Rate/concurrency limit; include Retry-After |
| 503 | Unable to accept durable work; do not return a job ID for uncommitted work |

Provider failures after acceptance are recorded in the job, with partial/unavailable results where valid. Error details must exclude secrets, raw scraped HTML, private source data and stack traces. Log request_id/trace_id for diagnosis.

## Result delivery
Trip output includes destination research, day schedules, evidence citations, costs and gaps. Route output includes ordered stops, road geometry, parking/walking legs, arrival times, skipped/infeasible points, alternatives, restriction basis and solver status. Media output includes candidate locations, supporting observations and confidence/verification distinction; user selection can initiate an explicitly accepted trip plan.

Sign private artifact URLs only after owner checks and expire them. Maps render provider geometry; they do not invent road connections or perform geocoding. No response implies a reservation, a guaranteed open road or live traffic unless supported and labeled.

## Implementation and validation gate
Phase 0: generate OpenAPI, DTOs and valid/invalid fixtures. API module: test authentication, owner isolation, all error codes, idempotent retries, version conflicts, cancellation races, cursor pagination and upload abuse. Integration: verify persist-before-202 and queue outage recovery. E2E: submit each workflow, answer a question, reconnect/poll, retrieve a real result and replan remaining stops. Test live provider behavior separately from fixture correctness. Deployment and test commands belong to DEVELOPMENT.md.
