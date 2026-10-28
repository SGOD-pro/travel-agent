# 0007. Trip Planner End to End Execution, Hosted LLM Integration, and Weather Advisory

**Date**: 2026-10-29
**Status**: Proposed

## Summary

This specification defines the complete end to end execution milestone for the SWENA trip planner. It connects travel research, hosted language model generation, deterministic claim verification, weather advisories, and route optimization into one coherent LangGraph workflow. A configurable multi provider language model adapter supports Google Gemini as primary with OpenAI and Anthropic fallbacks, enforcing strict schema validation and bounded retries without letting generated text become verified evidence. Open Meteo provides free weather forecasts with required attribution and offline test fixtures. The route optimizer executes in process within the orchestrator worker to persist PostGIS geometries and link route identifiers directly to trips, presenting travelers with an inspectable itinerary on both the REST API and the Next.js frontend.

## Requirements

**User stories**:
- As a traveler, I want to submit a travel brief and receive a complete, evidence backed itinerary with scheduled stops, route maps, budget estimates, and weather advisories so that I can plan my journey with confidence.
- As a traveler, I want clear distinctions between verified place facts, editorial recommendations, and unquoted budget items so that I understand where estimates or uncertainties exist.
- As an API client or frontend application, I want an inspectable itinerary endpoint and map visualization that return structured partial results when third party providers encounter issues, keeping job failures distinct from valid incomplete plans.
- As an operations engineer, I want configurable language model providers, bounded retries, and offline fixtures so that test suites run reliably without external network dependencies or unexpected cloud spend.

**Acceptance criteria**:
- **AC-1**: `TripPlannerGraph` compiles as an end to end LangGraph workflow coordinating brief parsing, place research, ranking, evidence collection, weather advisory fetching, route optimization, budget computation, and proposal synthesis under transactional checkpointing.
- **AC-2**: Hosted language model client supports configurable providers (`gemini`, `openai`, `anthropic`, and `fixture`) via `LLM_PROVIDER`, enforcing typed Pydantic input and output schemas, configurable socket timeouts (default 15 seconds), and bounded retries (maximum 2 retries with exponential backoff).
- **AC-3**: Language model outputs remain strictly bounded to candidate place proposals, descriptive rationales, and ranking suggestions. Deterministic policy engines validate place coordinates against corridor spatial thresholds (within 100 kilometers) and operating hours. Generated text is tagged with `EDITORIAL_DISCOVERY` uncertainty and never converted into verified evidence without corroborating source observations.
- **AC-4**: Route optimization is invoked in process via `RouteOptimizerWorkerService`, solving time windows and parking hubs, persisting route records in PostGIS tables, and storing the resulting `route_id` on the trip record and graph state without duplicate queue hops.
- **AC-5**: The generated route map is accessible through the existing `GET /api/v1/optimization/routes/{route_id}/map` endpoint, returning standards compliant GeoJSON FeatureCollections for rendering on the trip details page.
- **AC-6**: Weather integration consumes the Open Meteo forecast API without requiring an API key, converting hourly temperature, precipitation, humidity, and weather codes into structured `StopWeather` advisories with ghat road warnings and required attribution. An offline fixture adapter is provided for hermetic execution.
- **AC-7**: Budget calculation computes exact vehicle fuel costs, estimated lodging, and explicit unknown entries for unquoted tolls or entrance fees, producing structured budget lines and category summaries.
- **AC-8**: Endpoint `GET /api/v1/trips/{trip_id}/itinerary` returns the synthesized travel proposal, including daily schedules, place candidate cards with provenance, corridor weather advisories, budget lines, and linked route identifier.
- **AC-9**: When third party providers (language model, weather, or routing) encounter outages or rate limits, the graph degrades gracefully: fallback heuristics generate candidate stops from brief destinations, weather advisories report unavailable status, and the itinerary returns HTTP 200 OK with diagnostic skip reasons, keeping job failure distinct from a valid partial plan.
- **AC-10**: Strict tenant isolation is enforced across all trip and itinerary endpoints, returning HTTP 403 Forbidden for cross tenant access attempts and HTTP 404 Not Found for non existent resources.
- **AC-11**: The Next.js frontend trip page renders the complete itinerary view, displaying stop timelines, place evidence cards with uncertainty tags, the Leaflet CorridorMap, the CorridorWeatherAdvisory component with attribution, and a printable brief view.
- **AC-12**: Comprehensive test coverage includes unit tests for language model contracts and weather parsers, PostgreSQL lifecycle integration tests with transactional rollback, offline end to end graph execution, and a separately gated connected evaluation suite for real external endpoints.

## Decision

**Chosen option**: Option 1: Multi provider hosted language model adapter, in process route optimizer invocation, and Open Meteo weather client.

We implement a clean provider abstraction for hosted language models supporting Google Gemini as default with OpenAI and Anthropic adapters. Route optimization runs in process through the existing worker service to link routes directly to trips without redundant queue hops. Weather forecasts use Open Meteo with offline test fixtures.

## Feature design

**Data model sketch**:

1. Schema migration extending `trips`:
- `trips.route_id`: UUID nullable, foreign key pointing to `routes.id` with `ON DELETE SET NULL`, indexed for fast lookups.
- `trips.itinerary_json`: JSONB nullable, storing the latest synthesized proposal payload for direct reads.

2. State contract extensions in `TripPlanningState`:
```python
class TripPlanningState(TypedDict, total=False):
    trip_id: str
    owner_id: str
    version: int
    brief: dict[str, Any]
    discovered_places: list[dict[str, Any]]
    ranked_places: list[dict[str, Any]]
    weather_advisories: list[dict[str, Any]]
    route_id: str | None
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
- `PlaceProposal`: name, category, estimated latitude, estimated longitude, description, fit score, suggested duration minutes, price estimate INR (nullable).
- `ResearchRequest`: brief summary, origin name, destination names, traveler preferences, budget tier, transport mode.
- `ResearchResponse`: candidate places list, regional travel notes, recommended pace.
- `RankingRequest`: candidate places list, trip brief, traveler constraints.
- `RankingResponse`: ranked places list with fit scores and selection rationales.

4. Weather contract schemas in `backend/src/contracts/weather.py`:
- `StopWeatherCondition`: StrEnum (`sunny`, `cloudy`, `rainy`, `foggy`).
- `MonsoonRisk`: StrEnum (`low`, `moderate`, `severe`).
- `StopWeatherReport`: stop name, latitude, longitude, temperature celsius, condition, humidity percent, monsoon risk, ghat caution string (optional), forecast time.
- `CorridorWeatherAdvisoryReport`: stops list, overall caution, provider attribution string (`Weather data by Open Meteo.com`).

**State transitions**:

Workflow graph execution moves sequentially through eleven stages:
1. `PARSE_BRIEF`: Validates input brief against `TripBrief` schema.
2. `RESEARCH_PLACES`: Invokes hosted language model to propose points of interest along the travel corridor.
3. `VALIDATE_CANDIDATES`: Deterministic spatial filter discards candidates farther than 100 kilometers from the corridor line.
4. `RANK_CANDIDATES`: Scores candidates by brief interest alignment and dwell time feasibility.
5. `ROUTE_EVIDENCE_DECISION`: LAYA model evaluates whether candidate claims require external verification.
6. `AWAIT_EVIDENCE_RESULTS`: Waits for background search tasks if evidence collection was dispatched.
7. `ESCALATE_DYNAMIC_EVIDENCE`: Playwright browser fallback executes for dynamic pages requiring JavaScript rendering.
8. `FETCH_WEATHER`: Open Meteo client retrieves forecast metrics for all validated stop locations.
9. `SOLVE_ROUTE_AND_SCHEDULE`: In process invocation of `RouteOptimizerWorkerService` solves Vehicle Routing Problem with Time Windows and persists PostGIS routes.
10. `COMPUTE_BUDGET`: Computes fuel consumption, accommodations, and explicit unknown lines for unverified tolls or tickets.
11. `SYNTHESIZE_PROPOSAL`: Assembles schedule, map reference, weather, and budget into final proposal, recording `route_id` on the trip.

**API surface**:

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| /api/v1/trips/{trip_id}/plans | POST | None (uses stored brief) | job_id, status (HTTP 202) | Bearer (owner) | 404 not found, 403 forbidden, 409 conflict |
| /api/v1/trips/{trip_id} | GET | trip_id: UUID (path) | trip object, route_id, status | Bearer (owner) | 404 not found, 403 forbidden |
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
| Fuel budget line | Fuel liters, estimated fuel expense | `RoadBudgetCalculator` math using vehicle fuel profile and route distance |
| Toll budget line | Toll amount or explicit unknown reason | Highway toll tables or explicit unknown entry with unverified reason |
| Lodging budget line | Hotel stay amount, nights, rooms | Destination stay days multiplied by room count benchmark |
| Weather attribution | Provider attribution text | Required static text: "Weather data by Open Meteo.com" |

**Key invariants**:
- Generated language model text is labeled with uncertainty `EDITORIAL_DISCOVERY` and never converted into verified evidence without corroborating `source_observations`.
- Candidate places must lie within 100 kilometers of the corridor between origin and destinations.
- Every trip mutation and read is gated by the caller tenant identifier (`owner_id`).
- When external APIs fail, the workflow preserves partial findings and produces an inspectable incomplete itinerary rather than throwing an unhandled job exception.
- Unquoted costs remain explicit unknown budget entries with documented reasons rather than guessing zero or arbitrary numbers.

**Security model**:
- All endpoints enforce tenant isolation: requests must present a valid Bearer token matching `trips.owner_id`.
- External language model API keys (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) and SerpAPI credentials reside in environment variables and are never returned in client responses or serialized to database logs.
- Weather and language model HTTP requests pass through SSRF filtering, prohibiting private subnets, localhost, and cloud metadata IP ranges.

**Configuration required**:
- `LLM_PROVIDER`: Language model provider identifier (`gemini`, `openai`, `anthropic`, or `fixture`; default `gemini`).
- `LLM_MODEL`: Provider specific model name (default `gemini-2.5-flash` for Gemini, `gpt-4o-mini` for OpenAI, `claude-3-5-haiku-latest` for Anthropic).
- `GEMINI_API_KEY`: API key for Google Gemini (required when `LLM_PROVIDER=gemini`).
- `OPENAI_API_KEY`: API key for OpenAI (required when `LLM_PROVIDER=openai`).
- `ANTHROPIC_API_KEY`: API key for Anthropic (required when `LLM_PROVIDER=anthropic`).
- `LLM_TIMEOUT_SECONDS`: Maximum request socket timeout in seconds (default `15`).
- `LLM_MAX_RETRIES`: Maximum retry attempts on transient failures (default `2`).
- `WEATHER_PROVIDER`: Weather provider identifier (`open_meteo` or `fixture`; default `open_meteo`).
- `OPEN_METEO_BASE_URL`: Base URL for Open Meteo API (default `https://api.open-meteo.com/v1/forecast`).

**Critical test scenarios**:
- Happy path: Traveler submits brief, language model proposes points of interest, weather fetches metrics, route optimizer produces multi stop route, and synthesized itinerary is inspectable via API and frontend, verifies **AC-1**, **AC-4**, **AC-5**, **AC-7**, **AC-8**, **AC-11**.
- Failure case (Language model timeout): Hosted language model times out; client exhausts bounded retries and falls back to deterministic brief destination stops, producing a valid partial itinerary, verifies **AC-2**, **AC-9**.
- Failure case (Weather API unavailable): Open Meteo returns HTTP 503; weather client records unavailable status, proposal includes weather advisory caution, and trip job succeeds with diagnostic flags, verifies **AC-6**, **AC-9**.
- Auth and tenant isolation: Request for trip or route belonging to another tenant returns HTTP 403 Forbidden, verifies **AC-10**.

## Build plan

The build plan follows the Tracer Bullet approach, delivering thin, testable vertical slices from adapter foundation to full frontend and evaluation gates.

### Milestone Rollup

1. **Slice 1: Hosted language model client adapter and offline fixtures**, satisfies **AC-2**, **AC-3**.
2. **Slice 2: Open Meteo weather service and corridor weather aggregator**, satisfies **AC-6**.
3. **Slice 3: Place research, ranking, and deterministic claim verification integration**, satisfies **AC-1**, **AC-3**.
4. **Slice 4: Route optimizer in process workflow integration and persistence linking**, satisfies **AC-4**, **AC-5**.
5. **Slice 5: End to end trip planner graph compilation and itinerary synthesis**, satisfies **AC-1**, **AC-7**, **AC-8**, **AC-9**.
6. **Slice 6: REST API updates and Next.js frontend itinerary view integration**, satisfies **AC-8**, **AC-10**, **AC-11**.
7. **Slice 7: Gated connected live scenario evaluation suite**, satisfies **AC-12**.

### Detailed Tasks for Slice 1: Hosted Language Model Client Adapter

1. Define typed domain contracts in `backend/src/contracts/llm.py` for `PlaceProposal`, `ResearchRequest`, `ResearchResponse`, `RankingRequest`, `RankingResponse`, and provider configuration schemas, satisfies **AC-2**.
2. Implement abstract port `HostedLlmClientPort` in `backend/src/functions/orchestrator/llm/port.py` declaring `research_places` and `rank_places` methods, satisfies **AC-2**.
3. Implement `FixtureLlmClient` in `backend/src/functions/orchestrator/llm/adapters/fixture_client.py` reading structured responses from deterministic JSON fixtures in `backend/tests/fixtures/llm/`, satisfies **AC-2**, **AC-3**.
4. Implement `GeminiLlmClient` in `backend/src/functions/orchestrator/llm/adapters/gemini_client.py` using `google-genai` or standard HTTP requests with structured JSON schema enforcement, socket timeouts, and bounded retries, satisfies **AC-2**.
5. Implement `OpenAiLlmClient` in `backend/src/functions/orchestrator/llm/adapters/openai_client.py` and `AnthropicLlmClient` in `backend/src/functions/orchestrator/llm/adapters/anthropic_client.py` matching the port interface, satisfies **AC-2**.
6. Implement factory `create_llm_client` in `backend/src/functions/orchestrator/llm/factory.py` reading `LLM_PROVIDER` and returning the configured adapter, satisfies **AC-2**.
7. Add unit tests in `backend/tests/unit/test_llm_client.py` verifying schema parsing, timeout handling, retry backoff, and fallback behavior, satisfies **AC-2**.

Verification commands for Slice 1:
```bash
cd backend && PYTHONPATH=src pytest tests/unit/test_llm_client.py -v
```

## Consequences

**Positive**:
- Travelers receive realistic, evidence backed travel plans combining generative place recommendations with mathematically rigorous routing, accurate weather forecasts, and transparent fuel budgets.
- Multi provider design prevents vendor lock in, allowing seamless switching between Google Gemini, OpenAI, and Anthropic through environment variables.
- Offline fixture adapters enable fast, zero cost local testing and deterministic continuous integration runs without consuming third party API credits.
- In process route optimizer execution avoids redundant SQS queue round trips and preserves PostGIS spatial tables as the single source of geometry truth.

**Negative / tradeoffs**:
- Language model structured outputs occasionally encounter validation errors, requiring bounded retries that slightly increase initial plan generation latency.
- Free weather forecasting from Open Meteo carries rate limits for heavy commercial volume, requiring future migration to commercial tiers or local caching when scaling.
- In process optimizer execution couples the orchestrator worker process to the OR Tools solver runtime in this milestone.

**Neutral**:
- The Next.js frontend trip view transitions from static mock data to live API consumption, requiring running backend and database instances during local UI development.
- A new database migration adds `route_id` and `itinerary_json` columns to the `trips` table.

## Follow-up

- [ ] Add caching layer with Redis or PostgreSQL table for weather forecasts to avoid redundant Open Meteo requests for nearby coordinates on the same date.
- [ ] Evaluate streaming language model generation for progressive UI rendering during lengthy research phases.
- [ ] Design Phase 9 media extraction once trip planner end to end execution passes connected live scenario evaluation.

## Rationale

Reasoning and options: see [rationale.md](rationale.md).
