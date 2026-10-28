# Rationale: 0007. Trip Planner End to End Execution, Hosted LLM Integration, and Weather Advisory

## Context

The SWENA travel intelligence platform has successfully built its core execution foundation, evidence verification engine, browser escalation worker, remote LAYA decision router, and mathematical route optimizer across specifications 0001 through 0006. However, these systems currently operate as independent modules. The existing `trip_planner_graph` in the orchestrator remains an early skeleton: its destination discovery node merely copies brief destinations without research, its schedule solver runs an in memory mock bypassing the PostGIS route tables, and no weather service exists in the backend.

Travelers need a unified experience where a natural language brief turns into an inspectable, evidence backed journey. Producing a realistic itinerary requires combining multiple distinct domains: generative place discovery, deterministic spatial validation, source backed claim verification, road transit optimization with parking injection, weather condition forecasting, and granular budget calculation.

Several critical engineering forces shape this decision:
1. Generative reliability and drift: Language models excel at creative suggestion and regional knowledge synthesis, but frequently hallucinate coordinates, opening times, and travel distances. The architecture must prevent unverified model hallucinations from masquerading as authoritative travel facts. A coordinate proximity check alone proves neither existence nor operating hours.
2. Serverless packaging and lease fencing guarantees: The route optimizer worker depends on Google OR Tools, whose native C++ binaries exceed 90 megabytes uncompressed. Bundling OR Tools directly into the orchestrator Lambda ZIP would breach AWS Lambda uncompressed packaging ceilings (250 megabytes) and bypass the battle tested worker lease fencing, parent cancellation barriers, and scheduled crash recovery established in specifications 0001 and 0005.
3. Provider selection and operational simplicity: Rather than prematurely building and maintaining three distinct third party model clients, the platform needs a provider neutral port implemented with a single robust cloud native adapter. Anthropic Claude on AWS Bedrock via boto3 utilizes AWS native IAM authentication, requires zero additional SDK dependencies in Lambda, and provides exceptional structured JSON output reliability.
4. Transparent pricing and weather horizon reality: India road travel involves monsoon hazards, ghat road fog, and unquoted expressway tolls. The system must represent weather risks honestly within the valid 16 day meteorological forecast horizon without presenting historical averages as live forecasts. Furthermore, estimated fuel and lodging costs must remain clearly separated from unverified tolls, which must never be defaulted to zero.
5. Open Meteo licensing terms: Open Meteo provides free API access strictly for non commercial open source, research, and personal evaluation. Commercial production deployment requires an enterprise agreement or a self hosted container instance.

## Options considered

### Option 1: Provider neutral language model client with AWS Bedrock Claude adapter, canonical queue based route optimizer dispatch under lease fencing, and Open Meteo weather client (Recommended)

Define a provider neutral port `HostedLlmClientPort` implemented in Slice 1 with Anthropic Claude on AWS Bedrock via boto3 and offline test fixtures (basis: your AGENTS.md, simplicity first). Dispatch route optimization asynchronously using the existing transactional outbox and worker lease fencing contract (`WorkerFencingService.claim_task`), yielding execution at an interrupt barrier until the independent route optimizer worker completes and returns `RouteOptimizerWorkerOutput` (basis: orchestrator execution engine in spec 0001 and route optimizer in spec 0005). Integrate Open Meteo with explicit 16 day horizon gating and non commercial terms acknowledgment, while classifying budget lines into quoted, estimated, and unknown categories.

**Pros**:
- Preserves independent Lambda ZIP packaging: orchestrator ZIP remains lean while heavy OR Tools binaries remain isolated in the route optimizer package.
- Enforces parent cancellation barriers, task attempt ceilings, and lease recovery without bypassing the database fencing contract.
- Anthropic Claude on AWS Bedrock requires no external API keys, authenticates via IAM execution roles, and adds zero package overhead since boto3 is native to the Lambda runtime.
- Offline fixture adapters enable fast, zero cost local testing and deterministic continuous integration runs without consuming third party API credits.
- Strict candidate eligibility prevents unverified language model hallucinations from entering the mathematical scheduling solver as confirmed facts.

**Cons**:
- Asynchronous routing dispatch introduces an interrupt boundary and transactional outbox relay cycle, adding a brief polling delay compared to an in process call.
- Open Meteo free tier terms prohibit commercial production usage, requiring an enterprise license key or self hosted instance prior to commercial launch.

### Option 2: In process invocation of RouteOptimizerWorkerService within orchestrator worker

Import and execute `RouteOptimizerWorkerService.execute_task` directly inside the orchestrator worker process, bypassing SQS queues and outbox event relay.

**Pros**:
- Slightly lower execution latency by avoiding message queue round trips.
- Simplifies graph control flow by removing the interrupt boundary.

**Cons**:
- Severe packaging defect: bundling OR Tools C++ libraries into the orchestrator ZIP adds ~90 megabytes, risking breach of AWS Lambda 250 megabyte limits when combined with LangGraph, Playwright, and Scrapling.
- Circumvents the platform lease fencing architecture: in process execution bypasses `WorkerFencingService.claim_task`, lease renewal, parent cancellation barriers, and scheduled recovery.
- Couples routing compute directly to the graph runner process, causing potential worker memory exhaustion on large route solving instances.

### Option 3: Monolithic generative language model trip synthesis without deterministic routing or external weather verification

Prompt a frontier language model to generate the complete itinerary, including daily stop schedules, driving durations, fuel costs, and weather expectations directly in one long JSON response, bypassing the OR Tools solver and external weather APIs.

**Pros**:
- Lowest architectural complexity with zero worker coordination, no PostGIS route tables, and no external weather client code.
- Fast initial prototype implementation with minimal backend development effort.

**Cons**:
- Generative models produce mathematically invalid travel schedules, hallucinated road distances, incorrect opening hours, and fictional weather forecasts.
- Eliminates verified source provenance and violates core platform architectural principles established in specifications 0001 through 0006.
- Inability to compute precise vehicle fuel consumption or detect pedestrian parking restrictions.

## Rationale

We recommend Option 1 because it delivers the optimal balance of architectural integrity, operational reliability, and traveler trust.

Preserving the queue based dispatch for route optimization is a critical architectural requirement. Bundling OR Tools into the orchestrator Lambda ZIP would violate our constraint of independent, lean Lambda deployments. More importantly, executing routing via `WorkerFencingService.claim_task` ensures that if a parent execution is cancelled or reaches its deadline, the solver halts immediately without wasting computational resources.

Using Anthropic Claude on AWS Bedrock via boto3 for our initial language model adapter is the most pragmatic choice for our serverless AWS stack. It leverages IAM role authentication rather than exposing third party API keys, introduces zero external wheel weight because boto3 is pre installed in the Lambda Python runtime, and delivers excellent structured JSON adherence. Scoping Slice 1 strictly to Bedrock Claude and offline fixtures avoids premature abstraction sprawl while keeping the domain contract provider neutral for any future adapters.

The evidence and candidate eligibility gate addresses the fundamental problem of generative hallucination. A coordinate or distance check merely proves that a suggested point is near the road; it does not prove the place exists, is open, or has safe parking. Registering proposals as candidates with `is_candidate=True` and `EDITORIAL_DISCOVERY` ensures that only places backed by verified source observations or geocoder provenance enter scheduling as confirmed stops, with unverified places clearly flagged as editorial suggestions.

Finally, transparent budgeting and weather gating establish traveler trust. Distinguishing estimated fuel and lodging from unknown tolls ensures travelers are never misled by false precision, while capping weather forecasts at 16 days respects meteorological limits and Open Meteo licensing constraints.

## References

**Project sources**:
- `AGENTS.md`: Behavioral guidelines, simplicity first, surgical changes, and goal driven execution.
- `docs/specs/0001-orchestrator-execution-engine.md`: Orchestrator execution engine, transactional outbox, and checkpoint reconciliation.
- `docs/specs/0002-evidence-collection-and-verification.md`: Evidence provenance, canonical places, and deterministic claim verification.
- `docs/specs/0004-remote-laya-decision-router.md`: Remote decision routing, confidence gating, and deterministic fallback heuristics.
- `docs/specs/0005-route-optimizer.md`: Multimodal itinerary optimization, OR Tools solver, and PostGIS route persistence.
- `docs/specs/0006-route-graph-and-map-e2e.md`: Route optimizer graph, PostGIS GeoJSON assembly, and Leaflet CorridorMap visualization.

**Practices & standards**:
- Provider neutral port and adapter architecture for external service decoupling.
- Parent first lease fencing and cooperative cancellation barriers for long running worker tasks.
- Transactional outbox pattern for reliable, at least once task dispatch.
- Deterministic claim verification to prevent generative hallucinations from becoming verified facts.
- Offline fixture substitution for hermetic continuous integration testing without network dependencies.
- Tenant ownership isolation gating all database entities by authenticated owner identifier.
- Idempotency reservation keys preventing duplicate workflow executions.
