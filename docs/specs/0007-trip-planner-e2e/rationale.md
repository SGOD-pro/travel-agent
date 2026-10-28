# Rationale: 0007. Trip Planner End to End Execution, Hosted LLM Integration, and Weather Advisory

## Context

The SWENA travel intelligence platform has successfully built its core execution foundation, evidence verification engine, browser escalation worker, remote LAYA decision router, and mathematical route optimizer across specifications 0001 through 0006. However, these systems currently operate as independent modules. The existing `trip_planner_graph` in the orchestrator remains an early skeleton: its destination discovery node merely copies brief destinations without research, its schedule solver runs an in memory mock bypassing the PostGIS route tables, and no weather service exists in the backend.

Travelers need a unified experience where a natural language brief turns into an inspectable, evidence backed journey. Producing a realistic itinerary requires combining multiple distinct domains: generative place discovery, deterministic spatial validation, source backed claim verification, road transit optimization with parking injection, weather condition forecasting, and granular budget calculation.

Several critical engineering forces shape this decision:
1. Generative reliability and drift: Language models excel at creative suggestion and regional knowledge synthesis, but frequently hallucinate coordinates, opening times, and travel distances. The architecture must prevent unverified model hallucinations from masquerading as authoritative travel facts.
2. Provider agility and cost control: Relying on a single proprietary model provider creates operational vulnerabilities, rate limit risks, and cost instability. The platform needs provider flexibility with deterministic offline fixtures for continuous integration.
3. Execution simplicity versus microservice sprawl: The route optimizer already exists as a thoroughly tested service with OR Tools and Valhalla integration. Invoking it inside the trip planner workflow must avoid unnecessary message queue hops, worker races, or redundant schema layers.
4. Transparent pricing and weather attribution: India road travel involves monsoon hazards, ghat road fog, and unquoted highway tolls. The system must represent weather risks honestly and document unknown costs explicitly rather than inventing figures.

## Options considered

### Option 1: Multi provider hosted language model adapter, in process route optimizer invocation, and Open Meteo weather client (Recommended)

Build a clean adapter port for hosted language models supporting Google Gemini as default with OpenAI and Anthropic adapters, configurable via environment variables (basis: your AGENTS.md, simplicity first). Invoke `RouteOptimizerWorkerService` directly in process within the orchestrator worker to solve constraints and persist PostGIS geometries, linking the resulting route identifier directly to the trip (basis: route optimizer service pattern in spec 0005). Integrate Open Meteo as the free weather forecasting provider with required attribution and offline test fixtures (basis: offline fixture substitution for hermetic tests).

**Pros**:
- Multi provider adapter eliminates vendor lock in, allowing rapid switching between models as capabilities and pricing shift.
- In process execution of the route optimizer preserves transactional database boundaries, avoids extra SQS latency, and reuses tested solver logic without code duplication.
- Open Meteo requires no API key for development, offers hourly forecast resolution across India, and pairs with offline fixtures for hermetic test suites.
- Strict separation ensures language model suggestions are tagged as editorial discoveries while PostGIS and policy engines handle spatial and factual verification.

**Cons**:
- In process solver execution means the orchestrator worker environment must include the OR Tools native dependencies.
- Free Open Meteo usage carries rate limits that may require local caching or commercial upgrade under high production concurrency.

### Option 2: Single provider OpenAI adapter with asynchronous SQS route optimizer task dispatch and commercial weather service

Couple the trip planner exclusively to OpenAI structured outputs, dispatch route optimization as an asynchronous task across SQS queues to the standalone route optimizer worker, and subscribe to a paid commercial weather service like OpenWeatherMap.

**Pros**:
- Complete decoupling of worker processes allows independent scaling of the orchestrator and solver Lambda containers.
- Commercial weather APIs offer formal service level agreements and higher unauthenticated rate limits.

**Cons**:
- Enforces hard vendor lock in to OpenAI, preventing cost optimization or failover to alternative providers during outages.
- Asynchronous SQS dispatch introduces unnecessary message relay latency, distributed state synchronization, and complex graph interruption mechanics for an operation that takes under three seconds.
- Commercial weather services require paid subscription keys before local development or automated continuous integration can run.

### Option 3: End to end generative language model synthesis without deterministic routing constraints or external weather verification

Prompt a large frontier language model to generate the complete itinerary, including daily stop schedules, driving durations, fuel costs, and weather expectations directly in one long JSON response, bypassing the OR Tools solver and external weather APIs.

**Pros**:
- Lowest architectural complexity with zero worker coordination, no PostGIS route tables, and no external weather client code.
- Fast initial prototype implementation with minimal backend development effort.

**Cons**:
- Generative models produce mathematically invalid travel schedules, hallucinated road distances, incorrect opening hours, and fictional weather forecasts.
- Eliminates verified source provenance and violates core platform architectural principles established in specifications 0001 through 0006.
- Inability to compute precise vehicle fuel consumption or detect pedestrian parking restrictions.

## Rationale

We recommend Option 1 because it delivers the optimal balance of architectural integrity, operational simplicity, and traveler trust. 

The primary lesson from earlier platform phases is that generative language models must be constrained by deterministic policy engines (basis: deterministic claim verification in spec 0002 and remote LAYA decision routing in spec 0004). By restricting language models to proposing candidate places and descriptive summaries while using PostGIS for spatial thresholding, OR Tools for schedule optimization, and Open Meteo for weather metrics, the platform guarantees that every itinerary element is verifiable.

Invoking `RouteOptimizerWorkerService` directly in process within the orchestrator worker honors the repository core principle of simplicity (basis: your AGENTS.md, minimum code that solves the problem). The route optimizer logic is already modularized and tested; routing it through intermediate SQS queues when called from the trip planner would add latency and failure modes without any operational benefit.

The multi provider language model design provides business resilience. Google Gemini offers exceptional speed, large context windows, and cost efficiency as the default provider, while OpenAI and Anthropic adapters ensure operational continuity. The inclusion of offline fixtures ensures all unit and lifecycle tests run hermetically in continuous integration without network access or cloud API spend.

## References

**Project sources**:
- `AGENTS.md`: Behavioral guidelines, simplicity first, surgical changes, and goal driven execution.
- `docs/specs/0001-orchestrator-execution-engine.md`: Orchestrator execution engine, transactional outbox, and checkpoint reconciliation.
- `docs/specs/0002-evidence-collection-and-verification.md`: Evidence provenance, canonical places, and deterministic claim verification.
- `docs/specs/0004-remote-laya-decision-router.md`: Remote decision routing, confidence gating, and deterministic fallback heuristics.
- `docs/specs/0005-route-optimizer.md`: Multimodal itinerary optimization, OR Tools solver, and PostGIS route persistence.
- `docs/specs/0006-route-graph-and-map-e2e.md`: Route optimizer graph, PostGIS GeoJSON assembly, and Leaflet CorridorMap visualization.

**Practices & standards**:
- Multi provider adapter pattern for third party API resilience and vendor independence.
- Bounded exponential backoff and socket timeouts for resilient distributed HTTP calls.
- Deterministic claim verification to prevent generative hallucinations from becoming verified facts.
- Offline fixture substitution for hermetic continuous integration testing without network dependencies.
- Tenant ownership isolation gating all database entities by authenticated owner identifier.
- Idempotency reservation keys preventing duplicate workflow executions.
