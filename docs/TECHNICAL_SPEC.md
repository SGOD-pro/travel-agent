# Technical specification — implementation owner

Read only the numbered section needed. This file owns module dependencies, API/DTO/event/database details, algorithms, agent execution, security and error protocols. Phase 0 turns contracts into code; prose is not a completed implementation.

## Section navigation
1. Module packages, external APIs and models — dependencies and credential gates.
2. Contracts — message/DTO shapes, units, enums and limits.
3. Public API — endpoints and ownership behavior.
4. Database schema — records, keys, relationships and indexes.
5. Execution/agents/evidence — dispatch, handoffs, resume and memory.
6. Algorithms/errors — routes, media, budgets, retries and fallbacks.
7. Security/retention — auth, network validation and lifecycle.

## 1. Module packages, external APIs and models

Names below are implementation selections/candidates, not a claim that every version is pinned or every API is free. Pin compatible versions during each module build; test native wheels in the target Lambda environment.

| Module | Python packages or Node dependencies | External services | Output / build gate |
|---|---|---|---|
| api | fastapi, mangum, pydantic, pydantic-settings, boto3, psycopg[binary]; PyJWT[crypto] for configured JWT/JWKS auth | API Gateway HTTP API, S3 presigned uploads | Job IDs, results; choose trusted issuer/audience before protected live API |
| orchestrator | langgraph, langgraph-checkpoint-postgres, psycopg[binary,pool], pydantic, boto3, httpx | Bedrock Converse, Aiven PostgreSQL, SQS, S3, optional Upstash REST | Plans, tasks, checkpoints; export/check package size |
| decision_router | onnxruntime, tokenizers, numpy, pydantic, boto3, psycopg[binary]; audited minimal LAYA inference adapter | Versioned ONNX/tokenizer artifacts in S3 | Typed choices/scores; no guessed upstream helper names |
| evidence_collector | httpx, scrapling, pydantic, boto3, psycopg[binary] | SerpAPI HTTP search; permitted source pages | Source observations, citations, NEEDS_BROWSER request |
| browser | playwright-core, @sparticuz/chromium or -min, TypeScript, ajv, pg, @aws-sdk/client-s3 | Approved public URLs, S3 | Rendered observations; matched Chromium/Playwright packaging benchmark |
| media_extractor | numpy, Pillow, ImageHash, boto3, pydantic, psycopg[binary]; FFmpeg/FFprobe binaries | S3 uploads or permitted public media fetch | Audio + selected frames/timestamps; duration/resolution/runtime caps |
| media_analyzer | rapidocr + onnxruntime, numpy, boto3, pydantic, psycopg[binary]; OpenCV headless only if required by chosen OCR build | Amazon Transcribe asynchronous jobs; optional Mapillary/KartaView through orchestrator tools | OCR/ASR observations; selected OCR languages must be evaluated |
| optimizer | ortools, pydantic, httpx, boto3, psycopg[binary]; shapely/pyproj only if needed for geometry checks | Configured Valhalla adapter | Route alternatives, validity, fuel; production endpoint/support gate |

### Travel-specific tools inside orchestrator
| Capability | Packages | Provider / limitation |
|---|---|---|
| Places and nearby POIs | httpx; PostGIS SQL via psycopg | Nominatim-compatible geocoder, Overpass; not guaranteed complete Indian street/pandal coverage |
| Weather | httpx | Open-Meteo candidate; commercial terms must be approved; dates beyond forecast horizon use labeled seasonal guidance |
| Festivals, foods, hidden places, precautions | Existing evidence tasks | SerpAPI + official/local sources + Scrapling; no universal reliable closures/crowd API assumed |
| Street imagery verification | httpx | Mapillary Graph API token; KartaView coverage/service contract must be checked before enabling |
| Ranking/schedule/budget | Python datetime, zoneinfo, decimal; shared typed contracts | Deterministic rules; LLM explanations cannot change calculated amounts |
| Flights/stays/transport discovery | Existing evidence/provider adapters | Initially editorial/indicative links. Amadeus/Duffel/Hotelbeds remain deferred until separately approved integration and credentials |

### Models
Nova Lite through Bedrock is the initial hosted planning/evidence-interpretation/synthesis choice; Nova Micro is optional simple text routing work only after evaluation. Model IDs are configurable and must exist in selected region/account. Hosted inference costs money; Lambda includes SDK clients, not their model weights.

LAYA is a bounded non-generative decision engine. The upstream package may pull Torch/Transformers: do not blindly ship laya[onnx]. Export outside Lambda, audit minimal imports and compare CPU decision quality before deployment. Quantization method must be chosen from the pinned release and evaluated; no per-channel default is assumed.

FFmpeg + pHash/blur/brightness filtering removes redundant/poor frames without an LLM. It cannot semantically identify buildings/nature/roads by itself. Optional MobileNetV3 ONNX classifier is an evaluated candidate, not a mandatory v1 dependency or proof of location. ASR initially uses Transcribe; no large Whisper/Gemma/20B model weights in ZIP Lambda.

Frontend: Next.js, React, TypeScript, mapcn components + maplibre-gl, existing shadcn/ui and selected GSAP motion. No frontend LLM. Select a licensed tile/style source; do not assume a map library includes unlimited free tiles. Voice v1.1 uses MediaRecorder, optional browser VAD, hosted ASR and existing workflow; no mandatory new Lambda.

### Credential and provider checklist
| Configuration | Needed before |
|---|---|
| AWS region and IAM role; local AWS_PROFILE if used | AWS live smoke tests; use normal SDK credential chain |
| DATABASE_URL | Durable execution integration tests |
| SERPAPI_API_KEY | Real evidence search smoke test |
| BEDROCK_MODEL_ID + model access | LLM live smoke test |
| LAYA artifact URI/version/checksum | CPU inference benchmark |
| ROUTING_BASE_URL + capabilities | Real route/closure smoke test |
| GEOCODER_BASE_URL + provider policy | Geocoding smoke test |
| MAPILLARY_ACCESS_TOKEN | Optional imagery verification |
| Weather endpoint/license; map tile/style configuration | Weather/map release tests |
| JWT issuer/audience/JWKS URL | Authenticated API release |
| Upstash REST URL/token | Optional shared cache/rate limit |

Missing keys block live integration only, not fixture development. Never paste secrets into documentation/source/chat.

### Free/open boundaries
Nominatim public endpoint policy: max 1 request/second for the whole application, identifying User-Agent, attribution, caching, no client autocomplete or distributed bulk search. Prefer configurable compatible provider for production. Read https://operations.osmfoundation.org/policies/nominatim/ before deliberately enabling the public endpoint.

Overpass/OSM data is open; public endpoints have capacity limits and attribution/license requirements. SerpAPI, Bedrock, Transcribe, AWS and hosted database usage are not inherently free. Open-Meteo free endpoint is for non-commercial use; verify commercial/self-host terms. Imagery coverage and credentials vary.

### Primary references checked during this revision
- https://github.com/D4Vinci/Scrapling
- https://github.com/NandhaKishorM/laya
- https://github.com/RapidAI/RapidOCR
- https://github.com/Sparticuz/chromium
- https://serpapi.com/search-api
- https://operations.osmfoundation.org/policies/nominatim/
- https://nominatim.org/release-docs/latest/api/Search/
- https://valhalla.github.io/valhalla/api/matrix/api-reference/
- https://github.com/valhalla/valhalla/blob/master/docs/docs/api/route/api-reference.md
- https://open-meteo.com/en/docs
- https://open-meteo.com/en/pricing
- https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html


## 2. Contracts — implement these first

Pydantic v2 is the source of truth. Export versioned JSON Schema and generated TypeScript; do not manually maintain conflicting frontend/browser types. Reject unknown fields on public/task inputs. The following specification must become executable schemas and fixtures in Phase 0.

### Common conventions
UUID strings for IDs. UTC RFC3339 timestamps plus IANA timezone on user travel windows. WGS84 lat [-90,90], lon [-180,180]; GeoJSON uses [lon,lat]. Distance in meters, duration in integer seconds. Money: currency + integer minor units, nullable unknown values; never float currency. Lists have explicit bounded sizes. ArtifactRef is an internal S3 key/version/checksum/mime/size, never an arbitrary fetch URL. All cross-module messages have schema_version='1.0'.

### TaskEnvelope
| Field | Type / requirement |
|---|---|
| task_id, execution_id, trace_id | UUID; required |
| parent_task_id | UUID or null |
| workflow | TRAVEL / ROUTE / MEDIA |
| worker | EVIDENCE / BROWSER / DECISION / MEDIA_EXTRACT / MEDIA_ANALYZE / OPTIMIZE |
| action | Registered bounded action enum for selected worker |
| attempt | Integer >=1; retries preserve logical task_id |
| idempotency_key | Nonempty stable key within owner+execution scope |
| deadline_at | UTC timestamp, required |
| payload | Typed discriminated payload; exactly one inline payload or artifact reference |
| budget | Remaining calls/tokens/minor-unit cost and iteration limits; nonnegative |
| context_version | Integer >=1; immutable plan version being executed |

Owner identity is attached by trusted API/server, never accepted as authoritative user input. Message IDs do not substitute for logical task IDs.

### WorkerResult and CompletionEvent
WorkerResult: task_id, execution_id, attempt, context_version, result_id UUID, status SUCCESS/PARTIAL/NEEDS_BROWSER/WAITING_EXTERNAL/FAILED, output typed union or ArtifactRef, warnings[], error nullable, provider_usage[], started_at, finished_at.
CompletionEvent: event_id UUID, task_id, execution_id, attempt, result_id, context_version, recorded_at, schema_version. Completion refers to a persisted result; it is not an untrusted full state update.

Error: code enum, safe_message, retry_class TRANSIENT/REPAIRABLE/USER_INPUT/TERMINAL, provider nullable, retry_after_seconds nullable. NEEDS_BROWSER includes approved-target candidates/reason; only orchestrator decides dispatch. WAITING_EXTERNAL includes hosted job ID and schedules later status reconciliation; worker does not wait for transcription to finish.

### Execution and decision
Execution: execution_id, owner_id, requested_capabilities[], status CREATED/RUNNING/WAITING_TASKS/WAITING_USER/SUCCEEDED/PARTIAL/FAILED/CANCELLED, context_version, checkpoint_id nullable, deadline_at, budget_used, pending_task_ids[], result_ref nullable, failure nullable.
DecisionEnvelope: decision_id, allowed_choice, confidence [0,1] or null, model_version, policy_version, short rationale/evidence IDs, abstained bool. Choices must come from request-specific enum. Low confidence → configured deterministic fallback or stronger reasoning/user question; not recursive LAYA calls forever.

### Evidence and place
SourceObservation: observation_id, source_url, canonical_url, publisher, retrieved_at, published_at nullable, content_hash, artifact_ref, excerpt locator, extraction_method. A copied article is not independent corroboration.
Claim: claim_id, subject_id, predicate/category, typed value, evidence_ids[], verification VERIFIED/PARTIALLY_VERIFIED/CONFLICTED/UNVERIFIED/UNAVAILABLE, freshness FRESH/STALE/UNKNOWN, valid_from/valid_until nullable, geographic_scope, policy_version, contradictions[], confidence nullable.
Verification requires attributable evidence matching subject/time/scope and policy tests. LLM interpretation is a proposed claim, never a verification transition. Critical closures need appropriate authority/geographic resolution. Unknown publication date is preserved, not invented.

PlaceCandidate: candidate_id, label, locality, coordinates nullable, entrance/access coordinates nullable, source_ids[], rank_basis, unresolved_fields[]. VerifiedPlace additionally has canonical place_id, verified identity/location claim_ids and access status PUBLIC/RESTRICTED/UNKNOWN. A verified identity does not imply verified hours/safety/prices.
DestinationInsight: insight_id, place_id/destination_id, category, claim_ids[], relevance_basis, applicability, nearby_band nullable, freshness/verification per claim. Categories retain the complete list in REQUIREMENTS.md.

MediaRequest: exactly one public_url or owned ArtifactRef; optional caption, hashtags[], estimated_location, user hints. MediaLocationResult: status VERIFIED/CANDIDATES/INSUFFICIENT_EVIDENCE/UNAVAILABLE, verified_places[], candidates[], observation_refs[], unresolved_questions[], warnings[]. Empty verified list is valid and must not be displayed as success.

### Routing
RouteRequest: start, end, stops[], departure_at + timezone, modes, objective FASTEST/SHORTEST/BALANCED, must_visit_ids[], optional_stop_ids[], dwell_seconds per stop, opening windows, max_walk_m, mobility limits, return_deadline nullable, fuel inputs nullable, completed_stop_ids[], current_position nullable, previous_route_version nullable, restrictions[].
Restriction: restriction_id, type ROAD_CLOSURE/VEHICLE_ZONE/TIME_WINDOW/DELAY, geometry or resolved edge IDs, allowed_modes, effective interval, claim_ids[], verification, freshness. Unresolved restriction geometry cannot silently become a hard mapped edge.
OptimizedRoute: status FEASIBLE/PARTIAL/INFEASIBLE/UNAVAILABLE, route_version, ordered_stops[], skipped_stops[{id,reason}], legs[], alternatives[], totals, restrictions_applied[], unresolved_restrictions[], warnings[], solver_status OPTIMAL/FEASIBLE/TIME_LIMIT/INFEASIBLE/ERROR, assumptions[]. Solver feasibility is not a proof of global optimality.
Leg: from/to ID, mode, departure/arrival UTC, distance_m, travel_seconds, geometry GeoJSON LineString, parking_hub nullable, provider/version, restriction snapshot, traffic_basis LIVE/MODELED/UNKNOWN. Stop records include arrival/departure and dwell/wait. Totals distinguish driving_m/walking_m, travel/dwell/wait/elapsed seconds, fuel estimate with supplied mileage/price and basis.

### Trip and budget
TripBrief: destination/origin, dates or flexible period, timezone, travelers, interests[], budget nullable, mobility/diet constraints, transport, required experiences, nearby bands [2000,3000] and [5000,7000] meters configurable.
TripPlan: plan_version, status COMPLETE/PARTIAL/INFEASIBLE, experiences[], day schedules[], DestinationInsight[], route_ref nullable, budget, missing_information[], assumptions[], citations[], provider labels, suggested_next_step nullable.
Budget: known_subtotal, estimated_subtotal, unknown_items[], currency, budget_limit nullable, assessment WITHIN/OVER/UNKNOWN, basis. Fuel = driving_km / km_per_litre × price_per_litre, using Decimal; walking consumes no fuel. Overnight boundaries use actual date/time/timezone, not naive day labels.
Provider labels: LIVE_OFFER / INDICATIVE_SEARCH / EDITORIAL_DISCOVERY / UNAVAILABLE. None implies booked or reserved.

### Phase 0 required fixtures
Valid and invalid examples for every union/enum; wrong coordinate order/range; unknown cost; stale-but-verified claim; ambiguous media; multi-day midnight route; replayed completion; unsupported version; unauthorized artifact. Add example task JSON and public API schemas with generated frontend types. These are implementation deliverables, not completed tests in this documentation package.

### Default execution limits — initial configurable values
Interactive request: max 40 external tool calls, 3 research iterations per missing claim group, 2 route repairs, 2 replans, 3 same-action repeats, and 10-minute job deadline. Ordinary provider HTTP timeout starts at 20 seconds; browser navigation at 30 seconds. These are initial settings, not performance promises; expensive media jobs may use a separately displayed longer asynchronous deadline. Per-task retries max 3 total attempts. Cost/token ceilings must be configured per environment before live LLM execution; absent ceiling blocks metered dispatch. Reserve estimated spend atomically before fan-out and reconcile actual usage; bounded overruns are reported because providers may not return precise usage immediately.

### Worker payload discriminators
| Worker action | Required payload | Required outcome |
|---|---|---|
| EVIDENCE.collect | queries[], claim_scope, allowed_sources, freshness_need | observations[], unavailable_sources[], browser_requests[] |
| BROWSER.render | validated target URL, approved extraction selectors/fields, timeout | DOM/metadata/screenshot artifact refs or denied/unavailable |
| DECISION.classify | state text, finite choices, criterion, model version | DecisionEnvelope |
| MEDIA_EXTRACT.extract | owned artifact or permitted URL, sampling limits | frame refs with timestamps, optional audio ref, metadata |
| MEDIA_ANALYZE.analyze | frame/audio refs, selected analysis types, language hints | typed observations or WAITING_EXTERNAL |
| OPTIMIZE.solve | RouteRequest with resolved places and restriction snapshot | OptimizedRoute |

Phase 0 expands these into discriminated Pydantic classes and bounded fields. Media status VERIFIED requires >=1 verified place; CANDIDATES requires >=1 candidate; INSUFFICIENT_EVIDENCE and UNAVAILABLE allow neither. Trip COMPLETE requires all accepted hard requirements satisfied. Route FEASIBLE requires final geometry/time/mode validation; unsupported hard restrictions cannot be buried in warnings.

### Initial evidence policy
Identity/location: require matching name/locality or independently attributable geospatial/visual support; confidence alone is insufficient. Critical festival closures: authoritative applicable advisory or explicit trusted on-ground confirmation, spatial resolution and effective interval required; otherwise retain unresolved/user-reported status. Source retrieval time is not event publication time. Weather expiration follows provider issue/valid times. Hours/fees and other claim classes have configurable maximum ages reviewed in Phase 4; absent class policy marks freshness UNKNOWN. Availability is not live unless the provider returns a current scoped offer. Never equate a manually selected location with externally verified identity.


## 3. Public API — asynchronous, owner-scoped

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


### Example task envelope
```json
{
  "schema_version": "1.0",
  "task_id": "22222222-2222-4222-8222-222222222222",
  "execution_id": "11111111-1111-4111-8111-111111111111",
  "trace_id": "33333333-3333-4333-8333-333333333333",
  "parent_task_id": null,
  "workflow": "ROUTE",
  "worker": "EVIDENCE",
  "action": "collect",
  "attempt": 1,
  "idempotency_key": "closure-research-v1",
  "deadline_at": "2026-10-06T18:00:00Z",
  "payload": {
    "queries": ["official Kolkata festival traffic restriction advisory"],
    "claim_scope": {"locality": "Kolkata", "travel_date": "2026-10-06"},
    "allowed_sources": ["official", "attributable-local"],
    "freshness_need": "travel-window"
  },
  "budget": {"remaining_tool_calls": 3, "remaining_tokens": 1000, "remaining_cost_minor": 100, "currency": "INR", "remaining_iterations": 1},
  "context_version": 1
}
```
This is a fixture illustrating shape, not an actual festival assertion or live request. Phase 0 locks payload/budget field names as executable schemas; no consumer independently guesses them. TaskEnvelope action is the action suffix (`collect`), worker supplies its namespace; table notation EVIDENCE.collect denotes both.

## 4. Database schema baseline

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

## 5. Execution, agents and evidence protocol

**Agent role is a scoped task, not an unbounded autonomous process.** Planner proposes a typed research/task plan; category researchers collect applicable weather/food/place/access evidence; interpreter proposes normalized claims; deterministic verifier commits status; ranker/schedule/budget nodes calculate; synthesizer explains the accepted result. All relevant category tasks may share evidence workers. LAYA operates on explicit finite choices; low confidence selects configured fallback/reasoning/user input within remaining budget.

Orchestrator obtains execution lease and loads checkpoint → runs ready nodes → reserves task budgets → persists tasks/outbox → relay publishes SQS → worker claims task lease → performs bounded tools → transactionally persists result+completion outbox → acknowledges input → completion reconciler fences attempt/context, deduplicates and resumes graph. Scheduled relay/recovery runs even without traffic; it also polls submitted external ASR jobs in short invocations.

A send before outbox marking can duplicate events. A crash after result commit before ack replays a terminal task. Use fenced lease tokens and optimistic execution/route versions; late attempts cannot overwrite results. Visibility timeout exceeds intended processing window; start with batch size 1 or partial-batch failure reporting. Ack only after durable accepted outcome. DLQ recovery marks terminal tasks so joins cannot wait forever. Cancellation prohibits new tasks and final success publication; in-flight tools may finish but results cannot resurrect cancelled jobs.

Evidence: preserve source URL/content hash/locator and timestamps → deduplicate copied sources → interpret claims → verify support, identity, scope, authority, freshness and conflicts. Official/date-scoped sources prioritized for critical closures. A model guess, user location selection or copied articles are not independent verification. User reports are retained with their source basis and may justify precautionary avoidance without becoming official closure facts.

Memory is owner+execution scoped: preferences, accepted discoveries, visited stops and brief are context; observations/claims preserve their own verification. In-memory state is transient; recovery uses PostgreSQL/checkpoints. No sensitive personal profile or hidden chain-of-thought persistence is required.

## 6. Algorithms and error protocol

### Route and schedule
1. Resolve canonical place, locality and accessible entrances; ambiguity pauses for input.
2. Research applicable restrictions and map spatial extent/effective intervals.
3. Check provider capabilities; unsupported confirmed hard restriction blocks a feasible-route claim.
4. Build restricted road/walking costs. Include parking hubs, mode continuity and walk-back if the vehicle remains at hub.
5. Solve stop order/windows/dwell/return deadline with OR-Tools; optional stops have explicit skip reasons, required infeasible stops cannot disappear.
6. Fetch actual legs at planned departures; validate geometry and time of passage through restrictions. Static matrices are insufficient for time-varying closures: use time-bucket costs or bounded solve→route→validate→repair. Report heuristic solver status honestly.
7. Cost driving distance with Decimal fuel arithmetic; sum travel+dwell+wait; preserve unknown inputs. Return GeoJSON, alternatives and versioned explanations.
8. Reroute freezes completed history and starts at actual/confirmed current position; recompute affected paths and remaining order/windows, commit only against expected current version.

Experience ranking uses interests, source quality, freshness, geographic usefulness, accessibility, time/weather fit, costs and diversity. Hard constraints filter before preference scores; weights/configuration reviewed during implementation. Schedule includes meal/rest/transit/dwell/wait and local-day boundaries. Decline unsupported budget guarantees.

### Media
FFprobe validates media; FFmpeg extracts audio and candidate frames. pHash/blur/brightness filters retain useful timestamped samples without an LLM. An optional evaluated semantic classifier is distinct from those filters. OCR/ASR/vision branches can skip cleanly; joins track terminal task IDs. Hosted transcription submission returns WAITING_EXTERNAL and exits; scheduled reconciliation emits completion. Fuse observations, propose candidates, search/geocode/corroborate; return uncertainty if evidence does not identify a unique supported place.

### Errors and fallbacks
| Condition | Behavior |
|---|---|
| 429, transient 5xx, provider timeout | Bounded exponential backoff+jitter, honor Retry-After within deadline; account repeated metered calls |
| Invalid contract, denied target, unsupported schema | Terminal failure; no blind retry |
| Recoverable extraction/schema interpretation | Bounded repair with safe typed output; no arbitrary generated execution |
| Ambiguous place, missing hard constraint | WAITING_USER with typed question/context version |
| Source conflict/stale data | Re-research within budget; then conflicted/stale/partial output |
| Optional provider absent | Omit marked category; required dependencies yield unavailable/infeasible |
| Solver deadline | Validate incumbent; label feasible/time-limit or infeasible—never optimal without proof |
| S3/DB failure | Retry durability operation; do not ack/publicize false successful persistence |
| Browser access denied | Unavailable; offer permitted alternate source/upload |

Provider circuit breaker uses shared state per provider: initial 5 transient failures in 60 seconds → 60-second open → one leased half-open probe; reset on success. These are configurable initial values; per-instance counters alone are ineffective across Lambda replicas. Do not trip on malformed user input. Fallback uses only enabled registry providers with compatible capabilities/terms; no substitute fabricated data.

Freshness uses provider issue/validity times and configured claim-class ages; missing policy/date yields UNKNOWN. Retrieval alone does not prove source currency. Model report cannot change accepted math/claim state. Final schema/citation check rejects unsupported references; bounded repair or partial result.

## 7. Security and retention

- API verifies JWT signature/issuer/audience/expiry through configured trusted JWKS; choose issuer before live protected endpoints. Internal workers have no public HTTP routes.
- Every query/result/cancel/upload is owner-scoped; reject another user's execution/artifact reference even if ID is known. Owner comes from authenticated context.
- IAM least privilege per queue, bucket prefix and secret. DB credentials use roles/access patterns restricting owned writes; API views remain owner-scoped.
- URL policy validates protocol, host, port, all resolved IPs and each redirect. Block private/link-local/loopback/metadata destinations and user-supplied credentials. Recheck at connection to prevent DNS rebinding. Browser subrequests/downloads follow same network policy; process-local checks alone may be insufficient, so release requires tested egress enforcement.
- Uploads use short-lived owned presigned keys; verify actual MIME/header, checksum, size, duration, resolution and decompression limits before parsing. No arbitrary shell arguments: subprocess argv only; no shell=True or evaluated user expressions.
- Web text, captions, OCR and ASR are data, never system instructions. Tool registry/argument schema/policy approval constrain model proposals. Reject requests to reveal secrets/change policies from external content.
- Browser profiles are ephemeral; no personal authenticated sessions or CAPTCHA/access-control bypass. Dynamic public rendering is allowed; access denied returns UNAVAILABLE.
- Keep secrets out of logs; redact precise private GPS and sensitive media as appropriate. Default proposed retention: raw user media 7 days; job data 30 days; explicit saved trips until deletion. User deletion removes owned artifacts and linked personal records subject to documented legal obligations. Final lifecycle values reviewed before release.
- Cancellation stops new tasks; workers check cancellation/deadline at safe boundaries. Already-started API work may finish, but cancelled result cannot publish as job success.
- Tests: cross-owner access, forged/replayed completion, redirect SSRF, malicious webpage instructions, oversized media, expired token, cancellation race and secret-log inspection.
