# Live Backend Journey Verification Report, Spec 0007, November 18, 2026

**HEAD SHA**: `602380f5bdff270c64e463ff527a759292c8ed53`
**Overall Verdict**: **FAIL** (neither fresh unseeded road trip request reached proposal publication on live providers)
**Scope**: Real runner, dispatch, live workers, reconciliation, checkpoint resume, optimizer, weather, budget, and proposal publication with zero seeded evidence, zero fixture providers, zero manually selected search results, and zero direct graph node calls.

---

## 1. Local Transport Substitutions (Replacing AWS)

1. **Outbox to Worker Dispatch**: A local Python relay loop read `outbox_events` (`kind=TASK_DISPATCH`) from isolated PostgreSQL (`127.0.0.1:5432/acceptance_db`), marked `delivered_at`, and invoked real worker entrypoints (`evidence_collector.consumer.process_batch`, `browser.consumer.process_batch`, and `RouteOptimizerWorkerService.execute_task`) directly instead of AWS EventBridge Scheduler, SQS, and Lambda triggers.
2. **External Pre Step Geocoding**: Because no natural language brief parser or geocoder adapter exists in `backend/src/`, origin and destination coordinates were fetched live via `nominatim.openstreetmap.org` before constructing `TripBrief`.
3. **Runner Start Payload Hydration**: Because `POST /api/v1/trips/{trip_id}/plans` ([idempotency.py](file:///home/swyra/projects/travel-agent/backend/src/functions/api/idempotency.py#L115-L146)) writes `EXECUTION_START` outbox records without `brief`, `owner_id`, or initialized `budget` while [runner.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/runner.py#L302-L309) expects `brief` in `ExecutionStartEvent.payload`, the verification harness passed the fresh `brief`, `owner_id`, `trip_id`, and `QueryPlanningService.calculate_collector_budget` directly to `GraphRunnerService.handle_execution_start`.
4. **Live AWS Lambda Runtime Execution**: Remains **PENDING** (no AWS deployment performed).

---

## 2. Live Provider Readiness Check (Zero Credentials Printed)

| Provider | Configured Target / Model | Profile / Auth | Readiness |
|---|---|---|---|
| PostgreSQL | `127.0.0.1:5432/acceptance_db` (Alembic `0015_trip_route_itinerary`, 28 tables) | Local DB | **PASS** (PostgreSQL 16.4 connected) |
| AWS Bedrock | `amazon.nova-lite-v1:0` retrying to `apac.amazon.nova-lite-v1:0` in `ap-south-1` | `AWS_PROFILE=aws` (account ending `6027`) | **PASS** (STS verified; live `converse` succeeded) |
| SerpAPI | `https://serpapi.com/search` (`engine=google_maps`) | `SERPAPI_API_KEY` present in [backend/.env](file:///home/swyra/projects/travel-agent/backend/.env) | **PASS** (8 live queries across Runs 1 and 2 returned HTTP 200) |
| Scrapling HTTP | Live destination URLs selected directly from SerpAPI results | Public HTTP/HTTPS | **PASS** (`mysorepalace.karnataka.gov.in` and `mysururailmuseum.com` returned HTTP 200) |
| Remote LAYA | `exchanges-rentals-composed-back.trycloudflare.com` (`laya-rl-agent`) | `LAYA_API_KEY` present | **PASS** (connected probe succeeded in `1175ms`, `answer_confidence=0.5133`, `FALLBACK_LOW_CONFIDENCE` below `0.700`) |
| Valhalla | `https://valhalla1.openstreetmap.de` | Public HTTP | **PASS** (`/status` HTTP 200; live `/sources_to_targets` reached and returned HTTP 400 on 17 stop matrix) |
| Open Meteo | `https://api.open-meteo.com/v1/forecast` | Public HTTP | **PASS** (probe HTTP 200; not reached inside graph runs because optimizer failed upstream) |
| Geocoder | `https://nominatim.openstreetmap.org/search` | External pre step | **PASS** externally (HTTP 200 for Bengaluru, Mysuru, Madikeri); backend client **NOT IMPLEMENTED** |

---

## 3. Wiring Audit: HITL Clarification and Intake Paths

Direct code inspection of [graph.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L5134-L5200), [runner.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/execution/runner.py#L232-L793), [trips.py](file:///home/swyra/projects/travel-agent/backend/src/functions/api/routes/trips.py#L119-L399), and [jobs.py](file:///home/swyra/projects/travel-agent/backend/src/functions/api/routes/jobs.py#L72-L211):

* **Clarification question generation**: **NOT IMPLEMENTED** (`TripPlannerGraph` contains 14 nodes running straight from `PARSE_BRIEF` through `PUBLISH_PROPOSAL` with no ambiguity or clarification node).
* **Human pause (`WAITING_USER`)**: **NOT IMPLEMENTED** (`WAITING_USER` appears only as a read check string at [jobs.py](file:///home/swyra/projects/travel-agent/backend/src/functions/api/routes/jobs.py#L125); `TripPlannerGraph` only interrupts at `AWAIT_EVIDENCE_RESULTS` and `AWAIT_ROUTE_RESULTS`, transitioning strictly to `WAITING_TASKS`, which is a worker barrier and not HITL).
* **Answer submission API**: **NOT IMPLEMENTED** (no route exists in `trips.py` or `jobs.py` to submit traveler answers to clarification questions).
* **Resume from human answers**: **NOT IMPLEMENTED** (`GraphRunnerService.handle_execution_resume` only resumes after `CompletionReconciliationService` applies worker task completions).
* **Natural language brief parser and backend geocoder**: **NOT IMPLEMENTED** (`parse_and_validate_brief_node` requires structured `TripBrief` with numeric coordinates; `backend/src/` contains no geocoding HTTP client, only `geocoder_provenance` metadata fields).
* **API `POST /trips/{trip_id}/plans` to Runner start contract**: **FAIL** ([idempotency.py](file:///home/swyra/projects/travel-agent/backend/src/functions/api/idempotency.py#L115-L146) writes `EXECUTION_START` without `brief` or initialized collector `budget`, while `GraphRunnerService.handle_execution_start` expects `brief` in `event.payload`).

---

## 4. Capability Verdict Matrix

| Capability | Verdict | Observed Evidence |
|---|---|---|
| Clarification questions, human pause, answer submission, human resume | **NOT IMPLEMENTED** | Absent from `TripPlannerGraph`, `GraphRunnerService`, `trips.py`, and `jobs.py`. |
| Natural language brief intake and backend geocoder | **NOT IMPLEMENTED** | No NL parser or geocoder adapter in `backend/src/`; Nominatim called externally before `TripBrief`. |
| API `POST /trips/{trip_id}/plans` to `GraphRunnerService` start hydration | **FAIL** | `EXECUTION_START` outbox omits `brief`, `owner_id`, and `budget`. |
| Runner start, checkpoint persistence, and `WAITING_TASKS` dispatch | **PASS** | Both Run 1 (`be641924`) and Run 2 (`bd85e5a5`) acquired runner leases, persisted LangGraph checkpoints, and dispatched `TASK_DISPATCH` outbox events. |
| Bedrock LLM candidate discovery and ranking (`AWS_PROFILE=aws`) | **PASS** (with wiring defect) | Live `apac.amazon.nova-lite-v1:0` executed on `discover_candidates_node` (~`1057ms`, retried from base model ID to inference profile) and `rank_candidates_node` (~`3088ms`, ranked 17 candidates). However, `route_evidence_decision_node` ignored the 4 Bedrock planned queries because `llm_for_queries` was `None`. |
| Live SerpAPI search and durable budget accounting | **PASS** (with query routing defect) | 4 live queries executed per run, 4/4 reservations created and reconciled (`200/200 INR` minor units spent, `0` uncertain, `0` reserved). However, all queries used `engine=google_maps` and covered only `destinations[0]`. |
| Live static extraction (`SCRAPLING_HTTP`) | **PASS** | Fetched real SerpAPI selected URLs (`mysorepalace.karnataka.gov.in` and `mysururailmuseum.com`, HTTP 200, 1000 char excerpts). |
| Multi destination road trip journey (Run 1: Bengaluru to Mysuru and Madikeri) | **FAIL** | Crashed on resume in `route_evidence_decision_node` with `InsufficientRequiredEvidenceError` because `QueryPlanningService` only queried Mysuru and skipped Madikeri. |
| Remote LAYA decision router | **PASS** (separate connected gate) / **BYPASSED** in runs | Graph runs hit `DETERMINISTIC_SINGLETON` (only 1 eligible choice). Direct connected probe to `exchanges-rentals-composed-back.trycloudflare.com` succeeded (`model_version=laya-rl-agent`, `latency_ms=1175`, `answer_confidence=0.5133`, `FALLBACK_LOW_CONFIDENCE` below `0.700`, policy fallback to `DISPATCH_BROWSER_ESCALATION`). |
| Claim ingestion and candidate eligibility (Run 2: Bengaluru to Mysuru) | **FAIL** (quality defects) | Ingested 16 claims (`SERPAPI_SEARCH` only), promoted highway pin `"Mysore Rd"`, 5 hotels, and 5 restaurants as mandatory sightseeing stops, duplicated unverified `"Mysuru"` twice in `editorial_recommendations`, and created zero `claim_evidence` links for both `SCRAPLING_HTTP` pages. |
| Live Valhalla physical road routing and OR Tools optimization | **FAIL** | Dispatched 17 stops (`17 x 17 = 289` matrix cells) to `valhalla1.openstreetmap.de/sources_to_targets`; live Valhalla returned HTTP 400 `Exceeded max locations: 100.000000`. Unhandled `ValhallaRoutingError` left task `CLAIMED` until lease expiry, and all 16 stops had `is_must_visit=True` with 1 hour dwell inside one 9 hour Day 1 window. |
| Task lease expiry recovery, retry backoff, and fencing | **PASS** | Stale lease failure rejected (`LeaseFencingError`), `TaskRecoveryService.recover_expired_leases` recovered attempt 1 to `PENDING` with backoff (`TaskInRetryBackoffError`), and attempt 2 permanently failed via `WorkerFencingService.fail_task(retryable=False)`. |
| Degraded partial proposal resilience on optimizer failure | **FAIL** | `CompletionReconciliationService` marked execution `FAILED` (`reason=tasks_failed_or_cancelled`, `graph_resumed=False`) instead of advancing `consume_route_result_node` to publish a partial proposal without road geometry. |
| Live Open Meteo weather node (`fetch_weather`) | **BLOCKED** | Provider reachable externally (HTTP 200), but graph terminated as `FAILED` before reaching `fetch_weather`. |
| Budget computation (`compute_budget`) and source backed pricing | **BLOCKED** | Graph terminated before `compute_budget`; zero hotel or ticket prices were extracted into claims during evidence collection. |
| Road disruptions, official advisories, and toll information | **FAIL** / **PENDING** | Advisory query went to `google_maps` and returned highway pin `"Mysore Rd"`. Official advisory gate remains **PENDING**; toll pricing calculation is not implemented. |
| Proposal synthesis and atomic publication (`publish_proposal`) | **BLOCKED** | Neither Run 1 nor Run 2 reached `publish_proposal`; `trip.state` remained `draft` and `itinerary_json` remained `null`. |
| Live browser fallback gate (`BROWSER` worker) | **PENDING** | Zero `NEEDS_BROWSER` observations or `BROWSER` tasks were emitted by the live runs; gate remains **PENDING**. |
| Matching page promotion gate (`SCRAPLING_HTTP` to `SUPPORTS` claim) | **PENDING** | Both fetched pages (`mysorepalace.karnataka.gov.in` Kannada title and `mysururailmuseum.com` English title `MYSURU RAIL MUSEUM` with timings in excerpt) produced `0` `claim_evidence` links; gate remains **PENDING**. |
| Official road advisory gate | **PENDING** | No official government road advisory source was retrieved or classified; gate remains **PENDING**. |

---

## 5. Orchestration Timeline

### Run 1: 3 Day Multi Destination Road Trip (Bengaluru to Mysuru and Madikeri)
* **Natural language request**: Plan a 3 day road trip by petrol car from Bengaluru to Mysuru and Coorg (Madikeri) starting 2 days from now (`2026-11-20` to `2026-11-22`) for 2 adults with a moderate budget, interested in heritage palaces, local food, coffee estates, and scenic viewpoints.
* **IDs**: `execution_id=be641924-f9fe-4367-a327-485cd066be7c`, `trip_id=369b649c-08cd-43e5-9f62-4496aed20be1`, `evidence_task_id=94259dcc-9d64-4a81-9ea6-0427b0229b59` (`attempt=1`), `worker_result_id=f89754ce-c2bc-410a-9bfe-dbc128a06d5f`, `laya_decision_id=fac477dd-bc28-4010-ba89-1ce92bd25b90`.

| Time (UTC) | Event / Node | Provider / Model / Worker | Outcome, Latency, Cost, Fallback |
|---|---|---|---|
| `00:08:07.404` | External pre step geocode | Nominatim (`Bengaluru`, `Mysuru`, `Madikeri`) | HTTP 200 for all 3 (`12.9768, 77.5901`; `12.3041, 76.6530`; `12.4211, 75.7382`) |
| `00:08:10.074` | `EXECUTION_STARTED` (`seq=1`) -> `PARSE_BRIEF` -> `DISCOVER_CANDIDATES` | AWS Bedrock (`amazon.nova-lite-v1:0` -> retry `apac.amazon.nova-lite-v1:0`) | Retried to APAC inference profile; completed in ~`1.2s`; returned 0 candidate places and 4 unused query notes |
| `00:08:11.326` | `ROUTE_EVIDENCE_DECISION` (`SEARCH_COMPLETION`) | `LayaPolicyValidator` (`deterministic_singleton`) | `DETERMINISTIC_SINGLETON`, `allowed_choices=[DISPATCH_EVIDENCE_SEARCH]`, `latency=0ms`; planned 4 Mysuru only queries (Madikeri omitted) |
| `00:08:11.359` | `TASKS_DISPATCHED` (`seq=2`) -> interrupt `AWAIT_EVIDENCE_RESULTS` | `GraphRunnerService` | Status `WAITING_TASKS`, 1 `EVIDENCE` task (`94259dcc`) |
| `00:08:11.528` to `00:08:18.395` | `EVIDENCE` worker search (4 queries) | Live SerpAPI (`engine=google_maps`) | 4 HTTP 200 queries (`1861ms`, `2458ms`, `1564ms`, `698ms`); 16 search observations; 4/4 reservations reconciled (`200/200 INR` minor units spent) |
| `00:08:18.973` to `00:08:20.907` | `EVIDENCE` worker static extraction (2 URLs) | Live `ScraplingExtractionAdapter` | `https://mysorepalace.karnataka.gov.in/` (HTTP 200) and `http://mysururailmuseum.com/` (301 -> 200); total 18 `source_observations` persisted |
| `00:08:21.175` | `TASK_COMPLETED` (`seq=3`) -> `GRAPH_RESUMED` (`seq=4`) -> `EXECUTION_RESUMING` (`seq=5`) | `CompletionReconciliationService` -> `GraphRunnerService` | Reconciled `f89754ce`, resumed from checkpoint `1f1e2e42-a2bb-6a9f-8004-15f61778f09b` |
| `00:08:21.470` | `ROUTE_EVIDENCE_DECISION` -> `EXECUTION_FAILED` (`seq=6`) | `LayaPolicyValidator.handle_zero_choices` | **FAIL**: `InsufficientRequiredEvidenceError` (matched 1/2 destinations: Mysuru matched, Madikeri never queried; `execution.status=FAILED`, `claims=0`) |

### Run 2: 2 Day Single Destination Road Trip (Bengaluru to Mysuru)
* **Natural language request**: Plan a 2 day road trip by petrol car from Bengaluru to Mysuru starting 2 days from now (`2026-11-20` to `2026-11-21`) for 2 adults with a moderate budget, interested in heritage palaces, local food, and scenic viewpoints.
* **IDs**: `execution_id=bd85e5a5-8ec5-4d0d-b254-10096a86a166`, `trip_id=4a0bb982-c7d3-4e60-87aa-951b0a95218e`, `evidence_task_id=555d2a7e-9c98-4936-a10d-a73a42b3ff23` (`attempt=1`, `result_id=60dc21c8-c92b-4df6-b74f-de3cbc495abd`), `optimizer_task_id=aa1080dd-1ad7-459a-9ad5-10d2bb18fdf2` (`attempt=1` crashed, `attempt=2` permanently failed `result_id=dd089735-e4e7-4cfa-96ed-e68d200bb301`).

| Time (UTC) | Event / Node | Provider / Model / Worker | Outcome, Latency, Cost, Fallback |
|---|---|---|---|
| `00:21:41.309` | `EXECUTION_STARTED` (`seq=1`) -> `PARSE_BRIEF` -> `DISCOVER_CANDIDATES` | AWS Bedrock (`amazon.nova-lite-v1:0` -> retry `apac.amazon.nova-lite-v1:0`) | Retried to APAC profile; completed in ~`1.3s` |
| `00:21:42.664` | `ROUTE_EVIDENCE_DECISION` (`SEARCH_COMPLETION`) | `LayaPolicyValidator` (`58a15061`) | `DETERMINISTIC_SINGLETON` -> `DISPATCH_EVIDENCE_SEARCH` (`latency=0ms`) |
| `00:21:42.754` | `TASKS_DISPATCHED` (`seq=2`) -> interrupt `AWAIT_EVIDENCE_RESULTS` | `GraphRunnerService` | Status `WAITING_TASKS`, 1 `EVIDENCE` task (`555d2a7e`) |
| `00:21:42.941` to `00:21:44.471` | `EVIDENCE` worker search (4 queries) + 2 `SCRAPLING_HTTP` fetches | Live SerpAPI (`545ms`, `142ms`, `128ms`, `429ms`) + Scrapling | 18 observations persisted (`16 SERPAPI_SEARCH`, `2 SCRAPLING_HTTP`); `200/200 INR` minor units spent and reconciled |
| `00:21:46.488` | `TASK_COMPLETED` (`seq=3`) -> `GRAPH_RESUMED` (`seq=4`) -> `EXECUTION_RESUMING` (`seq=5`) | `CompletionReconciliationService` -> `GraphRunnerService` | Reconciled `60dc21c8`, resumed into `ROUTE_EVIDENCE_DECISION` |
| `00:21:46.768` | `ROUTE_EVIDENCE_DECISION` (`EVALUATION`) | `LayaPolicyValidator` (`9017ed54`) | `DETERMINISTIC_SINGLETON` -> `PROCEED_DETERMINISTIC_EVALUATION` (`latency=0ms`, `unhandled_browser_requests=0`) |
| `00:21:47.155` to `00:21:47.290` | `EVALUATE_CANDIDATES` (claim ingestion and eligibility) | PostgreSQL `ClaimIngestionService` | Ingested 16 claims (`16 SERPAPI_SEARCH` links, `0 SCRAPLING_HTTP` links); 16 eligible stops (including `"Mysore Rd"`, 5 hotels, 5 restaurants); 2 duplicate `"Mysuru"` editorial entries |
| `00:21:47.631` to `00:21:50.719` | `RANK_CANDIDATES` -> `DISPATCH_ROUTE_OPTIMIZER` (`seq=6`) | AWS Bedrock (`apac.amazon.nova-lite-v1:0`) | Retried to APAC profile (~`3.0s`); ranked 17 places; dispatched `OPTIMIZER` task `aa1080dd` with 17 stops (`17 x 17 = 289` matrix cells) |
| `00:21:52.258` | `OPTIMIZER` worker (`attempt=1`) | Live Valhalla (`valhalla1.openstreetmap.de/sources_to_targets`) | **FAIL**: HTTP 400 `Exceeded max locations: 100.000000` (~`1.4s`); unhandled `ValhallaRoutingError` left task `CLAIMED` |
| `00:36:23` to `00:40:29` | Lease recovery -> `attempt=2` permanent failure -> `TASK_FAILED` (`seq=7`) -> `EXECUTION_FAILED` (`seq=8`) | `TaskRecoveryService` -> `WorkerFencingService` -> `CompletionReconciliationService` | Expired lease recovered with retry backoff; `fail_task(retryable=False)` applied; reconciler marked execution `FAILED` (`tasks_failed_or_cancelled`, `graph_resumed=False`); `fetch_weather`, `compute_budget`, `synthesize_proposal`, `publish_proposal` never ran |

### Separate Connected Remote LAYA Gate Probe
* **Endpoint**: `exchanges-rentals-composed-back.trycloudflare.com`, `phase=BROWSER_ELIGIBILITY`, `eligible_choices=[DISPATCH_BROWSER_ESCALATION, PROCEED_DETERMINISTIC_EVALUATION]`
* **Observed Outcome**: `model_version=laya-rl-agent`, `latency_ms=1175`, `confidence=0.0005`, `answer_confidence=0.5133`, `status=FALLBACK_LOW_CONFIDENCE`, `rationale="Resolved answer confidence 0.513 below threshold 0.700"`, policy fallback selected `DISPATCH_BROWSER_ESCALATION`.

---

## 6. Raw Proposal Output and Observed Output Quality Defects

### Raw Published Proposal (Run 1 and Run 2)
```json
{
  "run_1": {
    "execution_id": "be641924-f9fe-4367-a327-485cd066be7c",
    "trip_id": "369b649c-08cd-43e5-9f62-4496aed20be1",
    "execution_status": "FAILED",
    "trip_state": "draft",
    "current_route_id": null,
    "itinerary_json": null,
    "failure_reason": "Insufficient required destination evidence to evaluate trip; candidate existence or budget exhaustion never substitutes for verified coordinates"
  },
  "run_2": {
    "execution_id": "bd85e5a5-8ec5-4d0d-b254-10096a86a166",
    "trip_id": "4a0bb982-c7d3-4e60-87aa-951b0a95218e",
    "execution_status": "FAILED",
    "trip_state": "draft",
    "current_route_id": null,
    "itinerary_json": null,
    "failure_reason": "tasks_failed_or_cancelled (Valhalla client error 400 for /sources_to_targets: Exceeded max locations: 100.000000 on 17 stops)"
  }
}
```

### Concrete Output Quality and Wiring Defects Observed

1. **Multi destination planning crashes on resume (Blocking)**: `QueryPlanningService.plan_destination_queries()` queries only `destinations[0]` (`Mysuru`), leaving `destinations[1]` (`Madikeri`) unqueried while `route_evidence_decision_node` requires verified evidence for every destination.
2. **Unbounded stop dispatch breaks live Valhalla and creates mathematically infeasible schedules (Blocking)**: `dispatch_route_optimizer_node` sent all 16 verified places plus origin (`17` locations, `289` matrix cells > public Valhalla's `100` limit) as `stop_type=DESTINATION`, `is_must_visit=True`, `dwell_seconds=3600`, all compressed into a single Day 1 9 hour window (`2026-11-20T03:30:00Z` to `12:30:00Z`).
3. **Hotels, restaurants, and a highway pin promoted as mandatory sightseeing stops**: All 5 budget hotels (`Hotel Mysore Royale`, `Hotel Rio Meridian`, `Aishwarya Le Royal`, `Hotel Kaveri`, `Hotel Sunshine`), all 5 restaurants, and the highway pin `"Mysore Rd"` (returned when the road advisory query ran on `engine=google_maps`) were dispatched as 1 hour mandatory sightseeing stops.
4. **Road advisory query misrouted to `google_maps` engine**: Sending `"Bengaluru to Mysuru road travel advisory route conditions weather road closures November"` to `engine=google_maps` yielded a map pin for `"Mysore Rd"` instead of web or official road disruption notices.
5. **Optimizer failure aborts execution instead of degrading to partial proposal**: `RouteOptimizerWorkerService` propagates unhandled `ValhallaRoutingError`, and `CompletionReconciliationService` terminates the entire execution as `FAILED` on a failed optimizer task rather than resuming `consume_route_result_node` to publish a partial proposal with weather, budget, and verified stops.
6. **Scraped pages never promoted to supporting claims**: `http://mysururailmuseum.com/` returned English page title `"MYSURU RAIL MUSEUM"` with explicit opening hours (`"Timings : 10.00 am - 18.00pm"`) in its excerpt, yet received `0` `claim_evidence` links (all 16 claims relied solely on `SERPAPI_SEARCH`).
7. **Bedrock query planning call wasted**: `discover_candidates_node` calls Bedrock (`~1.06s` plus inference profile retry) and receives 4 tailored search queries in `regional_travel_notes`, but `route_evidence_decision_node` ignores them and passes `llm_for_queries=None` into `QueryPlanningService`.
8. **Duplicate unverified anchor city in `editorial_recommendations`**: `"Mysuru"` appeared twice in `editorial_recommendations`.
9. **API to Runner start contract mismatch**: `POST /api/v1/trips/{trip_id}/plans` omits `brief`, `owner_id`, and `budget` from `EXECUTION_START`, preventing direct API triggered runs without manual hydration.
10. **Credential logging hygiene warning**: `SerpApiAdapter` sends `api_key` as a GET query parameter, so default `httpx` INFO logging emits the raw API key in stdout logs unless `httpx` logging is restricted or redacted.

---

## 7. Next Bounded Correction Prompt

```text
/develop 0007 live optimizer dispatch bounds and multi destination query planning

Inspect current HEAD (602380f), docs/reviews/2026-11-18-live-backend-journey-verification.md,
backend/src/functions/orchestrator/evidence/query_planner.py,
backend/src/functions/orchestrator/graphs/trip_planner/graph.py, and
backend/src/functions/route_optimizer/service.py.

Fix the two blocking live journey defects in one bounded slice without adding new architecture:

1. Multi destination query coverage and advisory engine separation:
   - Update QueryPlanningService to distribute bounded queries across all brief destinations
     (so every destination in brief.destinations receives evidence coverage) and route road
     advisory queries to web search (engine="google") rather than engine="google_maps" so road
     pins like "Mysore Rd" are never ingested as sightseeing stops.

2. Bounded, feasible route optimizer dispatch and Valhalla error degradation:
   - In dispatch_route_optimizer_node, separate sightseeing stops from lodging/editorial items,
     exclude road advisory observations from route stops, cap stops dispatched to Valhalla to
     keep the square matrix within Valhalla's 100 location limit (at most 9 stops plus origin),
     and set optional stops (is_must_visit=False) with multi day windows spanning brief.start_date
     to brief.end_date rather than cramming 16 mandatory stops into Day 1.
   - In RouteOptimizerWorkerService, catch ValhallaRoutingError cleanly and complete with
     ROUTING_ENGINE_UNAVAILABLE degraded output (preserving resilient partial proposal publication)
     instead of crashing unhandled.

Add focused unit and isolated PostgreSQL integration regressions for multi destination query
planning, advisory engine separation, Valhalla <= 100 matrix capping, multi day feasible stop
windows, and ValhallaRoutingError graceful degradation.

Run /check verify -> /check review -> /sync, then re-run the live Bengaluru to Mysuru and
Madikeri journey to inspect the first published live proposal.
```
