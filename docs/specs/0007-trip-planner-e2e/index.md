# 0007. Trip Planner End to End Execution, Language Model Integration, and Weather Advisory

**Date**: 2026-10-29
**Status**: In Progress

## Summary

This specification defines the complete end to end execution milestone for the SWENA trip planner. It coordinates traveler brief parsing, language model place discovery, deterministic candidate eligibility, weather advisories, asynchronous route optimization, budget computation, and atomic proposal publication under LangGraph checkpointing.

Language model integration uses a provider neutral abstract port with deterministic offline fixtures in Slice 1. This keeps Slice 1 completely independent of external cloud credentials or paid API calls, while allowing hosted adapters (such as AWS Bedrock Claude, direct Anthropic Claude, or OpenAI) to plug in cleanly once provider choices and credentials are confirmed.

Candidate scheduling resolves ambiguity by enforcing a strict gate: unverified candidates never enter the route solver and appear only as editorial suggestions. Places with verified existence but unknown opening hours enter the solver with an assumed daytime window that is explicitly labeled in state and schedule as assumed hours, never as verified facts.

Route optimization preserves the proven queue based task dispatch and worker lease fencing lifecycle, avoiding uncompressed Lambda ZIP package bloat while mapping task payloads and results to the existing worker and reconciliation interfaces. Publication of the finalized proposal executes inside a single database transaction with pessimistic row locking and version checks to handle concurrent execution races safely.

A resilient partial result policy guarantees that external outages or solver infeasibility produce informative partial proposals rather than crashing the execution. Measurable evaluation scenarios with separate offline and live gates ensure rigorous verification.

## Requirements

**User stories**:
- As a traveler, I want to submit a travel brief and receive a complete, evidence backed itinerary with scheduled stops, route maps, budget estimates, and weather advisories so that I can plan my journey with confidence.
- As a traveler, I want clear distinctions between verified place facts, editorial recommendations, and unquoted budget items so that I understand where estimates or uncertainties exist.
- As an API client or frontend application, I want an inspectable itinerary endpoint and map visualization that return structured partial results when third party providers encounter issues, keeping job failures distinct from valid incomplete plans.
- As an operations engineer, I want lean Lambda ZIP packages, robust worker lease fencing, bounded retries, and offline fixtures so that test suites run reliably without external network dependencies or unexpected cloud spend.

**Acceptance criteria**:
- **AC-1**: `TripPlannerGraph` compiles as an end to end LangGraph workflow coordinating brief parsing, place research, candidate validation, evidence collection, weather advisory fetching, route optimization dispatch and resume, budget computation, and proposal publication under transactional checkpointing.
- **AC-2**: Language model integration conforms to a provider neutral abstract port (`HostedLlmClientPort`) supporting deterministic offline test fixtures via `LLM_PROVIDER="fixture"` in Slice 1. The port declares typed Pydantic contracts (`ResearchRequest`, `ResearchResponse`, `RankingRequest`, `RankingResponse`), configurable request socket timeouts (default 15 seconds), and bounded retries (maximum 2 retries with exponential backoff). The architecture permits future hosted cloud adapters (AWS Bedrock Claude, direct Anthropic, OpenAI) to plug into the factory without modifying graph nodes.
- **AC-3**: Language model proposals remain bounded to candidate place suggestions and descriptions. A coordinate check alone proves neither existence nor operating hours. Candidates are registered in `canonical_places` with `is_candidate=True` and uncertainty `EDITORIAL_DISCOVERY`. Candidate places without verified existence never enter the route optimizer solver as stops, and are presented in the itinerary view only as editorial recommendations cards. Places with verified existence (confirmed via source observations or geocoder authority) but unknown opening hours enter the solver with an assumed daytime window (09:00 to 18:00) that is explicitly labeled in state and schedule as `HOURS_ASSUMED` (never presented as verified hours). Places with verified existence and verified operating hours enter as confirmed stops. Contradicted places are excluded entirely.
- **AC-4**: Route optimization preserves the canonical execution engine lifecycle: `TripPlannerGraph` emits a task specification for worker `OPTIMIZER` with action `solve` containing payload mapped to `RouteRequest` with `execution_id`, `owner_id`, and `trip_id`. The workflow halts at interrupt boundary `WAITING_TASKS`. The independent route optimizer worker claims the task under `WorkerFencingService.claim_task` with lease duration of 120 seconds, verifying the parent execution cancellation barrier (`ExecutionModel.status == "CANCELLED"` or `now >= deadline_at`). Upon completion, the worker produces `RouteOptimizerWorkerOutput` and completes the task atomically via `WorkerFencingService.complete_task`. Outbox delivery triggers `reconciliation.reconcile_task_event`, which acquires a serialized parent execution lock (`with_for_update`), deduplicates receipts via `ReceiptModel`, updates the LangGraph checkpoint idempotently, and resumes the graph to link `route_id` to the trip and state without bundling OR Tools into the orchestrator Lambda ZIP.
- **AC-5**: The generated route map is accessible through `GET /api/v1/optimization/routes/{route_id}/map`, returning standards compliant GeoJSON FeatureCollections for rendering on the trip details page.
- **AC-6**: Weather integration consumes the Open Meteo forecast API under non commercial terms (requiring commercial licensing for production deployment), supporting hourly temperature, precipitation, humidity, and weather codes with required attribution ("Weather data by Open Meteo.com"). For departure dates beyond the 16 day forecast horizon, the service returns an informational advisory rather than presenting historical averages as a live forecast. An offline fixture adapter is provided for hermetic execution.
- **AC-7**: Budget calculation labels fuel costs as `ESTIMATED` based on vehicle profiles, lodging as `ESTIMATED` based on stay duration benchmarks, verified entrance fees as `QUOTED`, and unquoted tolls or fees as explicit `UNKNOWN` entries with documented reasons, never defaulting unknown prices to zero.
- **AC-8**: Endpoint `GET /api/v1/trips/{trip_id}/itinerary` returns the synthesized travel proposal, including daily schedules, place candidate cards with provenance, corridor weather advisories, budget lines, and linked route identifier. Proposal publication executes inside a single database transaction: acquiring a pessimistic row lock on `trips` (`SELECT ... FOR UPDATE`), verifying that `trips.current_version` equals the planning execution base version, and committing both `TripDeltaModel` and the updated `trips` record (`current_route_id`, `itinerary_json`, and state). If `trips.current_version` has advanced due to a concurrent brief update, publication is rejected, recording a `PROPOSAL_SUPERSEDED` event and marking the execution as superseded without overwriting trip data.
- **AC-9**: When external providers encounter failures, the workflow follows a resilient partial result policy:
  (a) Language model failure or timeout exhausts bounded retries and falls back to deterministic brief destination stops, producing a valid partial itinerary.
  (b) Weather API failure or timeout marks `weather_status` as `WEATHER_UNAVAILABLE`, includes an informational advisory caution, and allows the trip proposal to complete.
  (c) Route solver infeasibility due to overconstrained stop windows preserves diagnostics and produces a valid partial plan with dropped stops diagnostics without a road polyline.
  (d) Routing provider timeout or outage (such as Valhalla failure) falls back to straight line Euclidean distance matrix with speed estimates, flags the route as `DEGRADED_ROUTING`, and annotates the itinerary with `ROUTING_UNAVAILABLE_ESTIMATED`.
  (e) Absent required evidence or search budget exhaustion leaves unverified candidates out of the solver schedule, retaining them as editorial discovery cards.
  (f) Cancellation or deadline expiry halts execution immediately at the nearest cooperative barrier with status `CANCELLED`, publishing no proposal.
  (g) Worker retries: if a worker exhausts retries (`attempts >= max_attempts`), task transitions to `FAILED`. For non critical enrichments (weather, dynamic evidence), the workflow proceeds with partial results. For critical route solver tasks where straight line fallback also fails, the execution transitions to `FAILED` with a structured error summary in job events. Only unhandled database errors, unrecoverable worker crashes, or cancelled executions fail the parent job.
- **AC-10**: Strict tenant isolation is enforced across all trip, route, and itinerary endpoints, returning HTTP 403 Forbidden for cross tenant access attempts and HTTP 404 Not Found for non existent resources.
- **AC-11**: The Next.js frontend trip page renders the complete itinerary view, displaying stop timelines, place evidence cards with uncertainty tags, the Leaflet CorridorMap, the CorridorWeatherAdvisory component with attribution, and a printable brief view.
- **AC-12**: Comprehensive test coverage includes unit tests for language model contracts and weather parsers, PostgreSQL lifecycle integration tests with transactional rollback, offline end to end graph execution, and measurable scenario evaluation suites separated into offline and live gates:
  (a) Food versus business scenario: verifies that a food focused brief generates place recommendations exclusively with dining or street food categories, asserting 1.0 precision and exactly 0 corporate office locations.
  (b) Category relevance scenario: verifies that specialized briefs (such as heritage tours) match designated category tags with precision at or above 0.8.
  (c) Unknown cost scenario: verifies that routes through unquoted toll plazas or unpriced attractions generate explicit `UNKNOWN` budget line items with documented reasons, and that budget totals exclude unknown amounts rather than guessing zero.
  (d) Distant dates scenario: verifies that departure dates beyond 16 days return `OUT_OF_FORECAST_HORIZON` without calling the forecast API.
  (e) Infeasible route scenario: verifies that impossible stop windows produce `INFEASIBLE` route records with clear diagnostic reasons.
  (f) Offline gate: runs in default continuous integration with hermetic fixtures.
  (g) Connected live gate: gated under `@pytest.mark.connected` and `RUN_CONNECTED_EVAL=1` to validate live external APIs when credentials are provided.

## Decision

**Chosen option**: Option 1: Provider neutral language model client with offline fixtures in Slice 1, canonical queue based route optimizer dispatch under lease fencing, Open Meteo weather client with 16 day horizon gating, and pessimistic locking proposal publication.

We implement a provider neutral abstract port for language models, with deterministic offline fixtures in Slice 1. This decouples local engineering and testing from external credentials or cloud provider billing, while maintaining structured Pydantic contracts ready for hosted adapters (AWS Bedrock Claude, direct Anthropic, OpenAI) when confirmed.

Candidate scheduling enforces a clean boundary: unverified place proposals never enter the route solver and remain in the proposal as editorial recommendations. Places with verified existence but unknown hours enter the solver with default daytime visiting windows tagged explicitly as assumed hours.

Route optimization continues to run through the proven queue based task dispatch and lease fencing worker, keeping the orchestrator Lambda ZIP lean, respecting parent cancellation barriers, and utilizing receipt deduplication during checkpoint reconciliation.

Publication executes inside a single database transaction with pessimistic row locking (`SELECT FOR UPDATE` on `trips`) and version checking, guaranteeing that stale proposals never overwrite modified briefs.

The resilient partial result policy ensures graceful degradation for provider timeouts, outages, and missing evidence, with clear failure boundaries for unrecoverable errors.

## Feature design

**Data model sketch**:

1. Schema migration extending `trips`:
- `trips.current_route_id`: UUID nullable, foreign key pointing to `routes.id` with `ON DELETE SET NULL`, indexed for lookups.
- `trips.itinerary_json`: JSONB nullable, storing the latest synthesized proposal payload for direct reads.

2. State contract extensions in `TripPlanningState`:
```python
class TripPlanningState(TypedDict, total=False):
    trip_id: str
    owner_id: str
    version: int
    brief: dict[str, Any]
    discovered_places: list[dict[str, Any]]
    validated_candidates: list[dict[str, Any]]
    ranked_places: list[dict[str, Any]]
    weather_advisories: list[dict[str, Any]]
    weather_status: str
    route_id: str | None
    route_status: str | None
    optimized_schedule: dict[str, Any]
    budget_lines: list[dict[str, Any]]
    budget_summary: dict[str, Any]
    proposal: dict[str, Any]
    current_step: str
    status: str
    errors: list[str]
    execution_id: str
    task_outputs: dict[str, Any]
    browser_requests: list[dict[str, Any]]
    pending_tasks: list[dict[str, Any]]
    dispatched_browser_urls: list[str]
    laya_decisions: list[dict[str, Any]]
    evidence_routing_action: str
    static_search_completed: bool
    required_evidence_satisfied: bool
    remaining_search_budget: int
    provider_diagnostics: dict[str, Any]
```

3. Language model contract schemas in `backend/src/contracts/llm.py`:
- `PlaceProposal`: name, category, estimated latitude, estimated longitude, description, fit score, suggested stay minutes, price estimate INR (nullable).
- `ResearchRequest`: brief summary, origin name, destination names, traveler preferences, budget tier, transport mode.
- `ResearchResponse`: candidate places list, regional travel notes, recommended pace.
- `RankingRequest`: candidate places list, trip brief, traveler constraints.
- `RankingResponse`: ranked places list with fit scores and selection rationales.

4. Weather contract schemas in `backend/src/contracts/weather.py`:
- `StopWeatherCondition`: StrEnum (`sunny`, `cloudy`, `rainy`, `foggy`).
- `MonsoonRisk`: StrEnum (`low`, `moderate`, `severe`).
- `WeatherAdvisoryStatus`: StrEnum (`AVAILABLE`, `OUT_OF_FORECAST_HORIZON`, `WEATHER_UNAVAILABLE`).
- `StopWeatherReport`: stop name, latitude, longitude, temperature celsius, condition, humidity percent, monsoon risk, ghat caution string (optional), forecast time.
- `CorridorWeatherAdvisoryReport`: status, stops list, overall caution, provider attribution string (`Weather data by Open Meteo.com`).

**State transitions and worker lifecycle**:

Workflow graph execution moves sequentially through twelve stages:
1. `PARSE_BRIEF`: Validates input brief against `TripBrief` schema.
2. `RESEARCH_PLACES`: Invokes language model client adapter to propose points of interest along the travel corridor.
3. `REGISTER_AND_FILTER_CANDIDATES`: Registers proposals in `canonical_places` with `is_candidate=True` and discards candidates farther than 100 kilometers from the corridor line. Unverified candidates remain flagged as `EDITORIAL_DISCOVERY`.
4. `RANK_CANDIDATES`: Scores candidates by brief interest alignment and stay duration feasibility.
5. `ROUTE_EVIDENCE_DECISION`: LAYA model evaluates whether candidate claims require external verification.
6. `AWAIT_EVIDENCE_RESULTS`: Interrupt barrier yielding execution until evidence tasks complete.
7. `ESCALATE_DYNAMIC_EVIDENCE`: Playwright browser fallback executes for dynamic pages requiring JavaScript rendering. Places with verified existence advance; candidates with unverified existence are filtered out of scheduling stops and preserved only as editorial recommendations. Places with verified existence but unknown hours receive assumed daylight windows labeled `HOURS_ASSUMED`.
8. `FETCH_WEATHER`: Open Meteo client checks departure date horizon (within 16 days) and retrieves forecast metrics for all validated stop locations.
9. `DISPATCH_ROUTING_TASK`: Formulates `RouteRequest` payload using validated stops, assigns `execution_id`, `owner_id`, and `trip_id`, emits pending task specification for worker `OPTIMIZER` with action `solve`, and halts at interrupt barrier `WAITING_TASKS`.
10. `AWAIT_ROUTING_RESULT`: Standalone worker claims task via `WorkerFencingService.claim_task` (120s lease, cancellation barrier check), solves route, and completes task via `WorkerFencingService.complete_task`. Outbox delivery triggers `reconciliation.reconcile_task_event`, which locks parent execution (`with_for_update`), deduplicates receipts via `ReceiptModel`, updates LangGraph checkpoint idempotently, and resumes graph. Graph extracts `route_id`, route status, and diagnostics.
11. `COMPUTE_BUDGET`: Computes fuel consumption (estimated), lodging (estimated), and explicit unknown lines for unverified tolls or entrance fees.
12. `PUBLISH_PROPOSAL`: Enters atomic database transaction:
    (a) Acquires pessimistic row lock: `SELECT * FROM trips WHERE id = :trip_id FOR UPDATE`.
    (b) Checks version: compares `trips.current_version` against `state["version"]`.
    (c) If version matches: atomically inserts `TripDeltaModel` and updates `trips` record (`current_route_id`, `itinerary_json`, state `planned`, updated timestamp).
    (d) If version does not match: detects concurrent modification, records `PROPOSAL_SUPERSEDED` event, and marks execution superseded without overwriting trip data.

**API surface**:

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| /api/v1/trips/{trip_id}/plans | POST | None (uses stored brief) | job_id, status (HTTP 202) | Bearer (owner) | 404 not found, 403 forbidden, 409 conflict |
| /api/v1/trips/{trip_id} | GET | trip_id: UUID (path) | trip object, current_route_id, status | Bearer (owner) | 404 not found, 403 forbidden |
| /api/v1/trips/{trip_id}/itinerary | GET | trip_id: UUID (path) | schedule, places, weather, budget, route_id | Bearer (owner) | 404 not found, 403 forbidden, 409 in progress |
| /api/v1/trips/{trip_id}/weather | GET | trip_id: UUID (path) | weather advisory report, attribution | Bearer (owner) | 404 not found, 403 forbidden |

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| Candidate place discovery | Place names, categories, coordinates | Language model research prompt with brief destination corridor |
| Spatial candidate validation | Coordinates validity, distance check | PostGIS `ST_DWithin` spatial query against brief corridor line |
| Evidence verification | Place authority, operating hours | SerpAPI and Scrapling source observations stored in `source_observations` |
| Route geometry and legs | Driving polylines, walking paths, parking | Valhalla routing engine matrix via `RouteOptimizerWorkerService` |
| Stop schedule timestamps | Arrival time, departure time, dwell minutes | Google OR Tools constraint solver in `RouteOptimizerWorkerService` |
| Corridor weather advisory | Temperature, precipitation, monsoon risk | Open Meteo forecast API or `FixtureWeatherAdapter` |
| Fuel budget line | Fuel liters, estimated fuel expense | `RoadBudgetCalculator` math using vehicle profile and route distance (labeled `ESTIMATED`) |
| Toll budget line | Toll amount or explicit unknown reason | Highway toll tables or explicit unknown entry with reason (labeled `UNKNOWN`) |
| Lodging budget line | Hotel stay amount, nights, rooms | Destination stay days multiplied by room count benchmark (labeled `ESTIMATED`) |
| Weather attribution | Provider attribution text | Required static text: "Weather data by Open Meteo.com" |

**Key invariants**:
- Generated language model text is labeled with uncertainty `EDITORIAL_DISCOVERY` and never converted into verified evidence without corroborating `source_observations`.
- Candidate places without verified existence never enter the route solver and remain in the itinerary only as editorial recommendation cards.
- Verified places with unknown operating hours receive assumed daytime visiting windows explicitly labeled as `HOURS_ASSUMED`.
- Candidate places must lie within 100 kilometers of the corridor between origin and destinations.
- Every trip mutation and read is gated by the caller tenant identifier (`owner_id`).
- When external APIs fail, the workflow preserves partial findings and produces an inspectable incomplete itinerary rather than throwing an unhandled job exception.
- Unquoted costs remain explicit unknown budget entries with documented reasons rather than guessing zero or arbitrary numbers.
- If the trip brief version advances while planning is in progress, the execution rejects publication of the stale proposal and records a diagnostic supersession event.

**Security model**:
- All endpoints enforce tenant isolation: requests must present a valid Bearer token matching `trips.owner_id`.
- External language model API credentials and SerpAPI keys reside in environment variables or AWS IAM roles and are never returned in client responses or serialized to database logs.
- Weather and language model HTTP requests pass through SSRF filtering, prohibiting private subnets, localhost, and cloud metadata IP ranges.

**Configuration required**:
- `LLM_PROVIDER`: Language model provider identifier (`fixture` by default; extensible to hosted providers).
- `LLM_TIMEOUT_SECONDS`: Maximum request socket timeout in seconds (default `15`).
- `LLM_MAX_RETRIES`: Maximum retry attempts on transient failures (default `2`).
- `LLM_FIXTURE_DIR`: Optional custom directory path for language model JSON fixtures (default `None`).
- `WEATHER_PROVIDER`: Weather provider identifier (`open_meteo` or `fixture`; default `open_meteo`).
- `OPEN_METEO_BASE_URL`: Base URL for Open Meteo API (default `https://api.open-meteo.com/v1/forecast`).
- `OPEN_METEO_API_KEY`: Commercial subscription key for Open Meteo (optional for development; required for commercial production deployment).

**Critical test scenarios**:
- Happy path: Traveler submits brief, language model proposes points of interest, weather fetches metrics, route optimizer produces multi stop route, and synthesized itinerary is inspectable via API and frontend, verifies **AC-1**, **AC-4**, **AC-5**, **AC-7**, **AC-8**, **AC-11**.
- Failure case (Language model timeout): Language model times out; client exhausts bounded retries and falls back to deterministic brief destination stops, producing a valid partial itinerary, verifies **AC-2**, **AC-9**.
- Failure case (Weather API unavailable): Open Meteo returns HTTP 503; weather client records unavailable status, proposal includes weather advisory caution, and trip job succeeds with diagnostic flags, verifies **AC-6**, **AC-9**.
- Distant dates policy: Trip planned with departure date 30 days in future; weather returns `OUT_OF_FORECAST_HORIZON` without calling forecast API, proposal succeeds with seasonal notice, verifies **AC-6**, **AC-12**.
- Infeasible route outcome: Overconstrained stop time windows produce `INFEASIBLE` route status; graph completes with diagnostic breach explanation and partial plan without map polyline, verifies **AC-4**, **AC-9**, **AC-12**.
- Food versus business scenario: Kolkata foodie brief requesting authentic dining; assertion: candidate places belong exclusively to `restaurant`, `street_food`, or `cafe`, with exactly 0 corporate office locations and 1.0 precision, verifies **AC-12**.
- Category relevance scenario: Specialized architectural heritage brief; assertion: candidate places match heritage and architecture tags with precision at or above 0.8, verifies **AC-12**.
- Unknown cost scenario: Route passing unquoted toll plaza; assertion: budget contains explicit `UNKNOWN` line item with documented reason, total budget excludes unknown items without defaulting to zero, verifies **AC-7**, **AC-12**.
- Routing provider outage scenario: Valhalla routing engine failure; worker falls back to straight line distance matrix, marks route as `DEGRADED_ROUTING`, and annotates itinerary with `ROUTING_UNAVAILABLE_ESTIMATED`, verifies **AC-4**, **AC-9**.
- Auth and tenant isolation: Request for trip or route belonging to another tenant returns HTTP 403 Forbidden, verifies **AC-10**.

## Build plan

The build plan follows the Tracer Bullet approach, delivering thin, testable vertical slices from adapter foundation to full frontend and evaluation gates.

### Milestone Rollup

1. **Slice 1: Provider neutral language model client contracts, abstract port, and offline fixtures**, satisfies **AC-2**, **AC-3**.
2. **Slice 2: Open Meteo weather service, forecast horizon policy, and corridor weather aggregator**, satisfies **AC-6**.
3. **Slice 3: Place candidate registration, spatial corridor validation, and deterministic claim eligibility gate**, satisfies **AC-1**, **AC-3**.
4. **Slice 4: Route optimizer task dispatch, worker lifecycle mapping, fencing claim, interrupt boundary, reconciliation receipt deduplication, and trip linking**, satisfies **AC-4**, **AC-5**.
5. **Slice 5: End to end trip planner graph compilation, budget calculation, atomic proposal publication transaction with pessimistic locking and version checking, and resilient partial result policy**, satisfies **AC-1**, **AC-7**, **AC-8**, **AC-9**.
6. **Slice 6: REST API endpoints and Next.js frontend itinerary view integration**, satisfies **AC-8**, **AC-10**, **AC-11**.
7. **Slice 7: Gated evaluation suites with measurable assertions across offline and live gates**, satisfies **AC-12**.

### Detailed Tasks for Slice 1: Provider Neutral Language Model Client Contracts and Offline Fixtures

- [x] 1. Define typed domain contracts in `backend/src/contracts/llm.py` for `PlaceProposal`, `ResearchRequest`, `ResearchResponse`, `RankingRequest`, `RankingResponse`, and provider configuration schemas, satisfies **AC-2**.
- [x] 2. Implement abstract port `HostedLlmClientPort` in `backend/src/functions/orchestrator/llm/port.py` declaring `research_places` and `rank_places` methods, satisfies **AC-2**.
- [x] 3. Implement `FixtureLlmClient` in `backend/src/functions/orchestrator/llm/adapters/fixture_client.py` reading structured responses from deterministic JSON fixtures in `backend/tests/fixtures/llm/`, satisfies **AC-2**, **AC-3**.
- [x] 4. Implement factory `create_llm_client` in `backend/src/functions/orchestrator/llm/factory.py` reading `LLM_PROVIDER` (defaulting to `fixture`) and returning the configured adapter, satisfies **AC-2**.
- [x] 5. Add deterministic test fixtures in `backend/tests/fixtures/llm/` covering candidate place research responses, candidate ranking responses, and edge case responses (such as empty candidates or distant points), satisfies **AC-2**, **AC-3**.
- [x] 6. Add unit tests in `backend/tests/unit/test_llm_client.py` verifying schema parsing, timeout handling, retry backoff configuration, and deterministic fixture responses, satisfies **AC-2**.

Verification commands for Slice 1:
```bash
cd backend && PYTHONPATH=src pytest tests/unit/test_llm_client.py -v
```

## Consequences

**Positive**:
- Travelers receive realistic, evidence backed travel plans combining generative place recommendations with mathematically rigorous routing, accurate weather forecasts, and transparent fuel budgets.
- Starting with provider neutral contracts and offline fixtures in Slice 1 ensures hermetic, zero cost local testing and immediate test execution without third party cloud dependencies or paid API keys.
- Strict candidate eligibility prevents unverified language model hallucinations from entering the mathematical scheduling solver as confirmed stops, while preserving discovered places as editorial recommendations.
- Preserving queue based route optimizer dispatch protects orchestrator Lambda ZIP size ceilings by keeping heavy OR Tools native binaries isolated in the route optimizer package.
- Pessimistic row locking and version checks during proposal publication guarantee that concurrent brief updates never result in overwritten or corrupted itineraries.
- The resilient partial result policy guarantees graceful degradation when external routing or weather providers encounter temporary outages.

**Negative / tradeoffs**:
- Open Meteo free tier terms prohibit commercial production usage, requiring an enterprise license key or self hosted instance prior to commercial launch.
- Two stage execution with asynchronous routing dispatch introduces an interrupt boundary and outbox relay cycle, adding a brief polling delay compared to an in process call.
- Hosted cloud provider adapters (Bedrock, Anthropic direct, OpenAI) remain deferred until API credentials and deployment choices are confirmed by the team.

**Neutral**:
- The Next.js frontend trip view transitions from static mock data to live API consumption, requiring running backend and database instances during local UI development.
- A new database migration adds `current_route_id` and `itinerary_json` columns to the `trips` table.

## Follow-up

- [ ] Add caching layer with Redis or PostgreSQL table for weather forecasts to avoid redundant Open Meteo requests for nearby coordinates on the same date.
- [ ] Evaluate commercial license options or self hosted Open Meteo Docker container for production scaling.
- [ ] Implement hosted cloud provider adapter (such as AWS Bedrock Claude or direct Anthropic) when credentials and billing are established.
- [ ] Design Phase 9 media extraction once trip planner end to end execution passes connected live scenario evaluation.

## Rationale

Reasoning and options: see [rationale.md](rationale.md).
