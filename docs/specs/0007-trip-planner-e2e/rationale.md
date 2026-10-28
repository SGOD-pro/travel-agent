# Rationale: 0007. Trip Planner End to End Execution, Language Model Integration, and Weather Advisory

## Context

The SWENA travel intelligence platform has successfully built its core execution foundation, evidence verification engine, browser escalation worker, remote LAYA decision router, and mathematical route optimizer across specifications 0001 through 0006. However, these systems currently operate as independent modules. The existing `trip_planner_graph` in the orchestrator remains an early skeleton: its destination discovery node merely copies brief destinations without research, its schedule solver runs an in memory mock bypassing the PostGIS route tables, and no weather service exists in the backend.

Travelers need a unified experience where a natural language brief turns into an inspectable, evidence backed journey. Producing a realistic itinerary requires combining multiple distinct domains: generative place discovery, deterministic spatial validation, source backed claim verification, road transit optimization with parking injection, weather condition forecasting, and granular budget calculation.

Several critical engineering forces shape this decision:
1. Generative reliability and drift: Language models excel at creative suggestion and regional knowledge synthesis, but frequently hallucinate coordinates, opening times, and travel distances. The architecture must prevent unverified model hallucinations from masquerading as authoritative travel facts. A coordinate proximity check alone proves neither existence nor operating hours. Candidate places lacking verified existence must never enter the mathematical route optimizer, while verified places with unknown hours must receive assumed daytime windows labeled explicitly as assumed hours.
2. Serverless packaging and lease fencing guarantees: The route optimizer worker depends on Google OR Tools, whose native C++ binaries exceed 90 megabytes uncompressed. Bundling OR Tools directly into the orchestrator Lambda ZIP would breach AWS Lambda uncompressed packaging ceilings (250 megabytes) and bypass the battle tested worker lease fencing, parent cancellation barriers, and scheduled crash recovery established in specifications 0001 and 0005.
3. Provider selection and decoupled contracts in Slice 1: Committing immediately to a specific hosted cloud model (such as AWS Bedrock, OpenAI, or direct Anthropic) requires cloud account configuration, IAM roles, or paid API keys before local tests can run. Establishing a provider neutral abstract port with deterministic offline fixtures in Slice 1 guarantees zero cost hermetic development and reproducible CI runs, allowing hosted cloud adapters to plug in once provider selection and credentials are confirmed.
4. Publication concurrency and locking: The trip planner produces proposals that update shared trip entities. If a traveler modifies their brief while a multi step execution runs, publishing the resulting itinerary would overwrite the traveler's newer brief. The publication stage must execute inside an atomic database transaction using pessimistic row locking (`SELECT FOR UPDATE`), compare base versions, and reject stale proposals with explicit supersession diagnostic events.
5. Resilient partial result policy: Road routing engines, geocoders, and weather services can experience transient network outages or rate limiting. Failing the entire planning job on any external outage degrades user experience. The platform must provide straight line Euclidean fallback matrices for routing outages, weather unavailable notices for weather API failures, and diagnostic breach explanations for overconstrained routes, reserving job failure strictly for unrecoverable database crashes or cancelled executions.
6. Transparent pricing and weather horizon reality: India road travel involves monsoon hazards, ghat road fog, and unquoted expressway tolls. The system must represent weather risks honestly within the valid 16 day meteorological forecast horizon without presenting historical averages as live forecasts. Furthermore, estimated fuel and lodging costs must remain clearly separated from unverified tolls, which must never be defaulted to zero.
7. Open Meteo licensing terms: Open Meteo provides free API access strictly for non commercial open source, research, and personal evaluation. Commercial production deployment requires an enterprise agreement or a self hosted container instance.

## Options considered

### Option 1: Provider neutral language model contracts with offline fixtures in Slice 1, canonical queue based route optimizer dispatch under lease fencing, Open Meteo weather client with 16 day horizon gating, and pessimistic locking proposal publication (Recommended)

Define a provider neutral abstract port `HostedLlmClientPort` implemented in Slice 1 with deterministic offline test fixtures (basis: your AGENTS.md, simplicity first). Dispatch route optimization asynchronously using the existing transactional outbox and worker lease fencing contract (`WorkerFencingService.claim_task`), yielding execution at an interrupt barrier until the independent route optimizer worker completes and returns `RouteOptimizerWorkerOutput` (basis: orchestrator execution engine in spec 0001 and route optimizer in spec 0005). Integrate Open Meteo with explicit 16 day horizon gating and non commercial terms acknowledgment, classify budget lines into quoted, estimated, and unknown categories, enforce pessimistic row locking on proposal publication, and apply straight line fallbacks on provider outages.

**Pros**:
- Preserves independent Lambda ZIP packaging: orchestrator ZIP remains lean while heavy OR Tools binaries remain isolated in the route optimizer package.
- Enforces parent cancellation barriers, task attempt ceilings, and lease recovery without bypassing the database fencing contract.
- Offline fixture adapters in Slice 1 enable fast, zero cost local testing and deterministic continuous integration runs without consuming third party API credits or requiring cloud accounts.
- Strict candidate eligibility prevents unverified language model hallucinations from entering the mathematical scheduling solver as confirmed facts, while preserving them as editorial recommendations.
- Pessimistic row locking on publication protects against lost updates and concurrent brief modifications.
- Resilient partial results ensure traveler briefs return useful itineraries even when external routing or weather providers experience outages.

**Cons**:
- Asynchronous routing dispatch introduces an interrupt boundary and transactional outbox relay cycle, adding a brief polling delay compared to an in process call.
- Open Meteo free tier terms prohibit commercial production usage, requiring an enterprise license key or self hosted instance prior to commercial launch.
- Hosted cloud provider adapters remain deferred until credentials and provider choices are finalized.

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

Starting Slice 1 with provider neutral contracts and offline fixtures follows the principle of simplicity first. By designing typed Pydantic contracts and a fixture adapter, we can build and verify the orchestrator research and ranking graph flow hermetically without incurring cloud API costs, managing AWS credentials, or facing rate limits. When a hosted provider is selected, implementing the adapter requires only plugging a concrete class into the existing factory without touching graph code.

The candidate scheduling gate directly resolves the hazard of generative hallucination. A coordinate or distance check merely proves that a suggested point is near the corridor; it does not prove the place exists, is open, or has safe parking. Excluding unverified candidates from the solver protects schedule integrity, while labeling places with unknown hours as `HOURS_ASSUMED` ensures travelers understand the difference between empirical opening hours and default daytime assumptions.

Pessimistic row locking during proposal publication prevents race conditions where a traveler updates their trip brief during background planning. Checking the version against the base version ensures stale proposals are gracefully superseded rather than silently overwriting fresh traveler intents.

Finally, resilient partial results and transparent budgeting establish traveler trust. Straight line distance fallbacks ensure routing provider outages degrade gracefully rather than aborting the trip, while distinguishing estimated fuel and lodging from unknown tolls ensures travelers are never misled by false precision.

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
- Pessimistic row locking (`SELECT FOR UPDATE`) and version checks preventing stale proposal overwrites.
