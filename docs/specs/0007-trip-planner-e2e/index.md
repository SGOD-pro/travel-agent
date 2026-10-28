# 0007. Trip Planner End to End Execution, Hosted LLM Integration, and Weather Advisory

**Date**: 2026-10-29
**Status**: Proposed

## Summary

This specification defines the complete end to end execution milestone for the SWENA trip planner. It coordinates traveler brief parsing, hosted language model place discovery, deterministic candidate eligibility, weather advisories, asynchronous route optimization, budget computation, and atomic proposal publication under LangGraph checkpointing. The language model integration uses a provider neutral port with Anthropic Claude on AWS Bedrock via boto3 as the primary adapter and deterministic offline fixtures for local testing. Route optimization preserves the proven queue based task dispatch and lease fencing lifecycle, avoiding uncompressed Lambda ZIP package bloat while linking route records to trip executions. Open Meteo provides weather metrics under non commercial terms with a strict 16 day forecast horizon policy, while unverified prices and estimated fuel costs remain transparently labeled in the budget.

## Requirements

**User stories**:
- As a traveler, I want to submit a travel brief and receive a complete, evidence backed itinerary with scheduled stops, route maps, budget estimates, and weather advisories so that I can plan my journey with confidence.
- As a traveler, I want clear distinctions between verified place facts, editorial recommendations, and unquoted budget items so that I understand where estimates or uncertainties exist.
- As an API client or frontend application, I want an inspectable itinerary endpoint and map visualization that return structured partial results when third party providers encounter issues, keeping job failures distinct from valid incomplete plans.
- As an operations engineer, I want lean Lambda ZIP packages, robust worker lease fencing, bounded retries, and offline fixtures so that test suites run reliably without external network dependencies or unexpected cloud spend.

**Acceptance criteria**:
- **AC-1**: `TripPlannerGraph` compiles as an end to end LangGraph workflow coordinating brief parsing, place research, candidate validation, evidence collection, weather advisory fetching, route optimization dispatch and resume, budget computation, and proposal publication under transactional checkpointing.
- **AC-2**: Hosted language model client conforms to a provider neutral abstract port (`HostedLlmClientPort`) supporting Anthropic Claude on AWS Bedrock via boto3 and offline test fixtures via `LLM_PROVIDER`, enforcing typed Pydantic input and output schemas, configurable socket timeouts (default 15 seconds), and bounded retries (maximum 2 retries with exponential backoff).
- **AC-3**: Language model proposals remain bounded to candidate place suggestions and descriptions. A coordinate check alone does not prove existence or operating hours. Candidates are registered in `canonical_places` with `is_candidate=True` and uncertainty `EDITORIAL_DISCOVERY`. Only places with verified existence and operating hours from source observations enter scheduling as confirmed stops; unverified candidates receive default daytime visiting windows labeled as editorial assumptions, and contradicted places are excluded.
- **AC-4**: Route optimization preserves the canonical execution engine lifecycle: `TripPlannerGraph` emits a task specification for worker `OPTIMIZER` with action `solve`, transitions to `WAITING_TASKS` at an interrupt boundary, the independent route optimizer worker claims the task under lease fencing, computes the route, and upon completion the graph resumes to link `route_id` to the trip and state without bundling OR Tools into the orchestrator Lambda ZIP.
- **AC-5**: The generated route map is accessible through `GET /api/v1/optimization/routes/{route_id}/map`, returning standards compliant GeoJSON FeatureCollections for rendering on the trip details page.
- **AC-6**: Weather integration consumes the Open Meteo forecast API under non commercial terms (requiring commercial licensing for production deployment), supporting hourly temperature, precipitation, humidity, and weather codes with required attribution ("Weather data by Open Meteo.com"). For departure dates beyond the 16 day forecast horizon, the service returns an informational advisory rather than presenting historical averages as a live forecast. An offline fixture adapter is provided for hermetic execution.
- **AC-7**: Budget calculation labels fuel costs as `ESTIMATED` based on vehicle profiles, lodging as `ESTIMATED` based on stay duration benchmarks, verified entrance fees as `QUOTED`, and unquoted tolls or fees as explicit `UNKNOWN` entries with documented reasons, never defaulting unknown prices to zero.
- **AC-8**: Endpoint `GET /api/v1/trips/{trip_id}/itinerary` returns the synthesized travel proposal, including daily schedules, place candidate cards with provenance, corridor weather advisories, budget lines, and linked route identifier.
- **AC-9**: When third party providers encounter failures, the workflow degrades gracefully: language model failure falls back to brief destinations, weather failure produces a weather unavailable advisory, and route solver infeasibility preserves diagnostics while returning a valid partial plan. Only unhandled worker crashes or database failures trigger job failure.
- **AC-10**: Strict tenant isolation is enforced across all trip, route, and itinerary endpoints, returning HTTP 403 Forbidden for cross tenant access attempts and HTTP 404 Not Found for non existent resources.
- **AC-11**: The Next.js frontend trip page renders the complete itinerary view, displaying stop timelines, place evidence cards with uncertainty tags, the Leaflet CorridorMap, the CorridorWeatherAdvisory component with attribution, and a printable brief view.
- **AC-12**: Comprehensive test coverage includes unit tests for language model contracts and weather parsers, PostgreSQL lifecycle integration tests with transactional rollback, offline end to end graph execution, and a separately gated connected evaluation suite for real external endpoints.

## Decision

**Chosen option**: Option 1: Provider neutral language model client with AWS Bedrock Claude adapter, canonical queue based route optimizer dispatch under lease fencing, and Open Meteo weather client with 16 day horizon gating.

We implement a provider neutral abstract port for language models, with Anthropic Claude on AWS Bedrock via boto3 as the primary adapter and offline fixtures for local testing. Route optimization continues to run through the proven queue based task dispatch and lease fencing worker, keeping the orchestrator Lambda ZIP lean and preserving cancellation barriers. Open Meteo provides weather forecasts with attribution and horizon checks, and the budget strictly distinguishes estimated from unknown costs.

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

**State transitions**:

Workflow graph execution moves sequentially through twelve stages:
1. `PARSE_BRIEF`: Validates input brief against `TripBrief` schema.
2. `RESEARCH_PLACES`: Invokes hosted language model to propose points of interest along the travel corridor.
3. `REGISTER_AND_FILTER_CANDIDATES`: Registers proposals in `canonical_places` with `is_candidate=True` and discards candidates farther than 100 kilometers from the corridor line.
4. `RANK_CANDIDATES`: Scores candidates by brief interest alignment and stay duration feasibility.
5. `ROUTE_EVIDENCE_DECISION`: LAYA model evaluates whether candidate claims require external verification.
6. `AWAIT_EVIDENCE_RESULTS`: Interrupt barrier yielding execution until evidence tasks complete.
7. `ESCALATE_DYNAMIC_EVIDENCE`: Playwright browser fallback executes for dynamic pages requiring JavaScript rendering.
8. `FETCH_WEATHER`: Open Meteo client checks departure date horizon (within 16 days) and retrieves forecast metrics for all validated stop locations.
9. `DISPATCH_ROUTING_TASK`: Emits pending task specification for worker `OPTIMIZER` with action `solve` and halts at interrupt barrier.
10. `AWAIT_ROUTING_RESULT`: Resumes upon worker completion receipt, clears pending tasks, and extracts `route_id`.
11. `COMPUTE_BUDGET`: Computes fuel consumption (estimated), lodging (estimated), and explicit unknown lines for unverified tolls or entrance fees.
12. `PUBLISH_PROPOSAL`: Verifies trip base version matches current version, records proposal in `trip_deltas`, links `current_route_id` on the trip, and marks execution succeeded.

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
| Candidate place discovery | Place names, categories, coordinates | Hosted language model research prompt with brief destination corridor |
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
- `LLM_PROVIDER`: Language model provider identifier (`bedrock` or `fixture`; default `bedrock`).
- `BEDROCK_MODEL_ID`: AWS Bedrock model identifier (default `anthropic.claude-3-5-haiku-20241022-v1:0`).
- `AWS_REGION`: AWS region for Bedrock runtime client (default `ap-south-1` or `us-east-1`).
- `LLM_TIMEOUT_SECONDS`: Maximum request socket timeout in seconds (default `15`).
- `LLM_MAX_RETRIES`: Maximum retry attempts on transient failures (default `2`).
- `WEATHER_PROVIDER`: Weather provider identifier (`open_meteo` or `fixture`; default `open_meteo`).
- `OPEN_METEO_BASE_URL`: Base URL for Open Meteo API (default `https://api.open-meteo.com/v1/forecast`).
- `OPEN_METEO_API_KEY`: Commercial subscription key for Open Meteo (optional for development; required for commercial production deployment).

**Critical test scenarios**:
- Happy path: Traveler submits brief, language model proposes points of interest, weather fetches metrics, route optimizer produces multi stop route, and synthesized itinerary is inspectable via API and frontend, verifies **AC-1**, **AC-4**, **AC-5**, **AC-7**, **AC-8**, **AC-11**.
- Failure case (Language model timeout): Hosted language model times out; client exhausts bounded retries and falls back to deterministic brief destination stops, producing a valid partial itinerary, verifies **AC-2**, **AC-9**.
- Failure case (Weather API unavailable): Open Meteo returns HTTP 503; weather client records unavailable status, proposal includes weather advisory caution, and trip job succeeds with diagnostic flags, verifies **AC-6**, **AC-9**.
- Distant dates policy: Trip planned with departure date 30 days in future; weather returns `OUT_OF_FORECAST_HORIZON` without calling forecast API, proposal succeeds with seasonal notice, verifies **AC-6**.
- Infeasible route outcome: Overconstrained stop time windows produce `INFEASIBLE` route status; graph completes with diagnostic breach explanation and partial plan without map polyline, verifies **AC-4**, **AC-9**.
- Auth and tenant isolation: Request for trip or route belonging to another tenant returns HTTP 403 Forbidden, verifies **AC-10**.

## Build plan

The build plan follows the Tracer Bullet approach, delivering thin, testable vertical slices from adapter foundation to full frontend and evaluation gates.

### Milestone Rollup

1. **Slice 1: Hosted language model client adapter (Bedrock Claude) and offline fixtures**, satisfies **AC-2**, **AC-3**.
2. **Slice 2: Open Meteo weather service, forecast horizon policy, and corridor weather aggregator**, satisfies **AC-6**.
3. **Slice 3: Place candidate registration, spatial corridor validation, and deterministic claim eligibility gate**, satisfies **AC-1**, **AC-3**.
4. **Slice 4: Route optimizer task dispatch, interrupt boundary, worker resume, and trip linking**, satisfies **AC-4**, **AC-5**.
5. **Slice 5: End to end trip planner graph compilation, budget calculation, and atomic proposal publication**, satisfies **AC-1**, **AC-7**, **AC-8**, **AC-9**.
6. **Slice 6: REST API endpoints and Next.js frontend itinerary view integration**, satisfies **AC-8**, **AC-10**, **AC-11**.
7. **Slice 7: Gated connected live scenario evaluation suite**, satisfies **AC-12**.

### Detailed Tasks for Slice 1: Hosted Language Model Client Adapter

1. Define typed domain contracts in `backend/src/contracts/llm.py` for `PlaceProposal`, `ResearchRequest`, `ResearchResponse`, `RankingRequest`, `RankingResponse`, and provider configuration schemas, satisfies **AC-2**.
2. Implement abstract port `HostedLlmClientPort` in `backend/src/functions/orchestrator/llm/port.py` declaring `research_places` and `rank_places` methods, satisfies **AC-2**.
3. Implement `FixtureLlmClient` in `backend/src/functions/orchestrator/llm/adapters/fixture_client.py` reading structured responses from deterministic JSON fixtures in `backend/tests/fixtures/llm/`, satisfies **AC-2**, **AC-3**.
4. Implement `BedrockClaudeClient` in `backend/src/functions/orchestrator/llm/adapters/bedrock_client.py` using boto3 `bedrock-runtime` Converse API with structured Pydantic schema validation, socket timeouts, and bounded retries, satisfies **AC-2**.
5. Implement factory `create_llm_client` in `backend/src/functions/orchestrator/llm/factory.py` reading `LLM_PROVIDER` and returning the configured adapter, satisfies **AC-2**.
6. Add unit tests in `backend/tests/unit/test_llm_client.py` verifying schema parsing, timeout handling, retry backoff, and fallback behavior, satisfies **AC-2**.

Verification commands for Slice 1:
```bash
cd backend && PYTHONPATH=src pytest tests/unit/test_llm_client.py -v
```

## Consequences

**Positive**:
- Travelers receive realistic, evidence backed travel plans combining generative place recommendations with mathematically rigorous routing, accurate weather forecasts, and transparent fuel budgets.
- Using Anthropic Claude via AWS Bedrock leverages AWS native IAM authentication without third party API key sprawl, and boto3 is already bundled in the standard AWS Lambda runtime.
- Preserving queue based route optimizer dispatch protects orchestrator Lambda ZIP size ceilings by keeping the heavy OR Tools native binaries isolated in the route optimizer package.
- Offline fixture adapters enable fast, zero cost local testing and deterministic continuous integration runs without consuming third party API credits.

**Negative / tradeoffs**:
- Open Meteo free tier terms prohibit commercial production usage, requiring an enterprise license key or self hosted instance prior to commercial launch.
- Language model structured outputs occasionally encounter validation errors, requiring bounded retries that slightly increase initial plan generation latency.
- Two stage execution with asynchronous routing dispatch introduces an interrupt boundary and outbox relay cycle, taking slightly longer than a naive direct function call.

**Neutral**:
- The Next.js frontend trip view transitions from static mock data to live API consumption, requiring running backend and database instances during local UI development.
- A new database migration adds `current_route_id` and `itinerary_json` columns to the `trips` table.

## Follow-up

- [ ] Add caching layer with Redis or PostgreSQL table for weather forecasts to avoid redundant Open Meteo requests for nearby coordinates on the same date.
- [ ] Evaluate commercial license options or self hosted Open Meteo Docker container for production scaling.
- [ ] Design Phase 9 media extraction once trip planner end to end execution passes connected live scenario evaluation.

## Rationale

Reasoning and options: see [rationale.md](rationale.md).
