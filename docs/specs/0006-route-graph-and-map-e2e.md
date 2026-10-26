# 0006. Route Graph, Map GeoJSON, and End to End Vertical Slice

**Date**: 2026-10-25
**Status**: Accepted

## Summary

This specification defines the route orchestration graph, GeoJSON map visualization, and complete end to end execution slice for SWENA travel planning. It coordinates the asynchronous route optimizer worker inside a dedicated LangGraph state machine with transactional checkpointing, supporting independent itinerary optimization and mid trip replanning. A new route map endpoint compiles PostGIS stop geometries and JSONB leg geometries into standards compliant GeoJSON FeatureCollections, styling driving, walking, detour, and parking legs for direct browser rendering. The frontend CorridorMap component is extended to consume this GeoJSON stream, enabling travelers to inspect stops, parking transitions, and detour schedules visually with full attribution and safe popup rendering.

## Context

The route optimizer built in specification 0005 solves multi stop Vehicle Routing Problems with Time Windows, parking hub injection, road closure avoidance, and mid trip replanning. However, the optimizer currently runs as an isolated worker claimed from SQS queues, without formal graph orchestration coordinating its lifecycle within the platform execution engine. The platform needs a stateful workflow graph that manages the complete route lifecycle, tracks execution checkpoints, and exposes user facing interfaces for map visualization and route revision tracking.

Travelers and downstream planning agents cannot easily interpret raw JSON stop coordinates and polyline strings without spatial visualization. A complete travel itinerary consists of distinct transport phases, including highway driving, pedestrian precinct walking, parking transitions, and detour segments avoiding road closures. Without an authoritative GeoJSON API surface, frontend clients must assemble disjointed geometry strings, duplicating styling logic and risking coordinate discrepancies.

Furthermore, dynamic mid trip replanning requires clear lineage visibility. When an active road restriction or schedule delay triggers a reroute, the traveler needs to inspect what changed between the original plan and the updated itinerary. The system must record direct parent route lineage, preserve historical stop timelines, and expose version comparison data so travelers understand why specific stops were reordered or dropped, even for standalone routes created without an overarching trip record.

Finally, architectural phase 8 mandates an end to end vertical slice proving that a route optimization request moves seamlessly from API acceptance through orchestrator graph execution, worker constraint solving, PostGIS geometry assembly, and interactive map display on live databases and real browser sessions.

## Requirements

**User stories**:
- As a traveler, I want to view my optimized multi stop itinerary on an interactive map so that I can see the exact driving paths, walking legs, parking locations, and schedule timings.
- As a traveler encountering a road closure or delay, I want to submit my live location for a mid trip replan so that I receive an updated itinerary avoiding the obstacle while preserving completed stops.
- As an API client or frontend application, I want a single GeoJSON endpoint for any route so that I can render styled map layers without custom geometry parsing or styling heuristics.

**Acceptance criteria**:
- **AC-1**: `RouteOptimizerGraph` compiles as a dedicated LangGraph state machine with nodes for route initialization, solver task dispatch, worker result waiting, GeoJSON map hydration, and replan reconciliation, persisting checkpoint state safely using `TransactionBoundCheckpointer`.
- **AC-2**: Asynchronous endpoint `POST /api/v1/optimization/routes` reserves owner scoped API idempotency atomically at acceptance, initiates `RouteOptimizerGraph` under execution workflow `ROUTE_OPTIMIZER_GRAPH`, returns HTTP 202 Accepted with canonical `job_id`, and enables status polling through `GET /api/v1/jobs/{job_id}`.
- **AC-3**: Execution lifecycle conforms to dispatch, interrupt, worker completion, reconciliation, resume, and completion. Workflow `ROUTE_OPTIMIZER_GRAPH` remains outside standalone optimizer terminalization, transitioning from `WAITING_TASKS` to `RUNNING` via `EXECUTION_RESUME` upon receipt confirmation, resuming the graph to load the persisted route using `RouteOptimizerWorkerOutput.route_id` before marking the execution `SUCCEEDED`.
- **AC-4**: Durable job to route discovery is provided without relying on paginated timeline events, enabling clients and internal services to resolve the resulting `route_id` directly through indexed `routes.execution_id` queries.
- **AC-5**: Endpoint `GET /api/v1/optimization/routes/{route_id}/map` returns a standards compliant GeoJSON FeatureCollection assembling PostGIS stop geometries (`route_stops.location`) and JSONB leg geometries (`route_legs.geometry`), with detour provenance derived from applied restrictions (`route_restrictions.was_applied`) and route diagnostics, styled with simplestyle presentation properties.
- **AC-6**: Mid trip replanning endpoint `POST /api/v1/optimization/routes/{route_id}/replans` accepts live traveler coordinates, freezes visited stops, checks active road restrictions, computes an updated route version linked via `parent_route_id`, supports standalone routes without `trip_id`, enforces optimistic concurrency against stale versions, and returns HTTP 202 Accepted.
- **AC-7**: Lineage endpoint `GET /api/v1/optimization/routes/{route_id}/versions` returns the chronological version history of a route with parent pointers, distance and duration deltas, and dropped stop differences.
- **AC-8**: Infeasible or partial routes persist requested stops, skip reasons, and constraint diagnostics in PostgreSQL, returning HTTP 200 OK from the map endpoint with stop points marked with failure properties and top level error metadata, keeping job failure distinct from a valid infeasible route outcome.
- **AC-9**: Strict tenant ownership fencing is enforced across all route endpoints, returning HTTP 403 Forbidden for cross tenant access attempts and HTTP 404 Not Found when a route does not exist.
- **AC-10**: The frontend Leaflet CorridorMap component consumes the GeoJSON FeatureCollection directly, rendering multi color paths (driving in blue, walking in emerald, detours in amber), custom stop pins with visit sequence numbers, parking icons, safe text escaped popups, and proper map attribution for OpenStreetMap and CARTO.
- **AC-11**: An automated browser end to end suite verifies the vertical slice across real local frontend, backend, and PostgreSQL instances using an injected provider, alongside separately gated live Valhalla tests, covering parking, walking, vehicle return, changed road detours, partial routes, infeasible constraints, and cross owner isolation.

## Options considered

### Option 1: Dedicated RouteOptimizerGraph with dynamic PostGIS GeoJSON assembly and Leaflet map extension

Implement a standalone LangGraph workflow (`RouteOptimizerGraph`) in the orchestrator, managed under execution workflow `ROUTE_OPTIMIZER_GRAPH`. Assemble map GeoJSON dynamically from `route_stops` and `route_legs` tables using PostGIS stop points and JSONB leg polylines with property hydration. Extend the existing Leaflet CorridorMap frontend component to render the GeoJSON FeatureCollection with styled layers.

**Pros**:
- Modular design allows the route graph to run standalone or compose cleanly as a subgraph inside future trip planner workflows.
- PostGIS dynamic assembly eliminates redundant stored JSON columns and ensures geometries remain synchronized with relational tables.
- Reuses existing proven Leaflet and Tailwind components in the frontend with zero added third party map libraries.
- Preserves clean lifecycle separation: `ROUTE_OPTIMIZER_GRAPH` remains outside standalone optimizer terminalization, allowing post solver graph nodes to execute before job completion.

**Cons**:
- Requires coordinating state handoffs between orchestrator outbox events, worker queues, and graph resumption.

### Option 2: Monolithic TripPlannerGraph embedding route solving directly inside trip research

Incorporate route solving steps directly into the existing `trip_planner_graph`, treating route optimization as a mandatory downstream step of destination research without a standalone route workflow.

**Pros**:
- Combines trip planning and route optimization into a single unified LangGraph state machine.

**Cons**:
- Prevents standalone route optimization for travelers who already have chosen destinations and only want sequencing.
- Heavy coupling complicates testing and violates single responsibility boundaries between research and optimization.
- Mid trip replanning would force re evaluation of entire trip research graphs.

### Option 3: Stateless REST API calling worker queues directly without LangGraph orchestration

Expose route optimization and replanning as basic CRUD endpoints that queue SQS tasks directly without LangGraph state graphs or transactional checkpointing.

**Pros**:
- Simpler initial implementation with fewer graph abstraction layers.

**Cons**:
- Lacks durable checkpoint rollback and structured execution resumption on worker retries.
- Cannot easily compose into multi stage trip planner workflows in later phases.
- Fails phase 8 architectural requirements for graph based execution.

## Decision

**Chosen option**: Option 1: Dedicated RouteOptimizerGraph with dynamic PostGIS GeoJSON assembly and Leaflet map extension.

This approach delivers a modular, independently runnable route optimization workflow that integrates cleanly with the orchestrator engine while providing rich GeoJSON map visualization for web clients.

## Rationale

A dedicated `RouteOptimizerGraph` satisfies the architectural requirement that capabilities in SWENA can run either independently or composed together. Travelers frequently need standalone route sequencing and mid trip replanning for existing destinations without triggering full generative trip research. Wrapping the route solver in a LangGraph state machine provides transactional execution checkpoints, atomic rollbacks on transaction aborts, and standardized job status polling.

Dynamic GeoJSON assembly using PostGIS `ST_AsGeoJSON` for stop points and direct extraction of pre computed JSONB line strings from `route_legs.geometry` is vastly superior to persisting pre computed GeoJSON blobs in the database. The `route_stops` table stores authoritative PostGIS geometries with GiST spatial indexes, while `route_legs` stores turn by turn provider line strings. Generating the GeoJSON FeatureCollection on read time guarantees total consistency, avoids schema duplication, and executes in single digit milliseconds for typical itineraries of 5 to 25 stops.

Extending the existing Leaflet CorridorMap component maintains UI design consistency across the application. The frontend already includes Leaflet, custom CartoDB dark matter tile configuration, and Tailwind styling. Consuming standards compliant GeoJSON directly allows the frontend map to display multi color travel modes, closure detours, and parking transitions with minimal code complexity while strictly preserving tile attribution and text escaping in popups.

## Feature design

### 1. Data Model and Schema Reconciliation

Database table `routes` receives an Alembic migration adding `parent_route_id`:
- `parent_route_id`: UUID, nullable, foreign key referencing `routes(id)` on delete set null.
- Index created on `parent_route_id` for efficient lineage traversal.
- `trip_id`: UUID, nullable, foreign key referencing `trips.id` on delete set null. When `trip_id` is null, the route is a standalone route whose version tree is linked solely via `parent_route_id`.
- Unique constraint `uq_routes_trip_version` on `(trip_id, route_version)` applies when `trip_id IS NOT NULL`. For standalone routes where `trip_id IS NULL`, version sequencing is governed by `parent_route_id` and optimistic concurrency checks.

Tables `route_stops`, `route_legs`, and `route_restrictions` remain unchanged, matching actual specification 0005 implementations:
- `route_stops`: `id` (UUID PK), `route_id` (UUID FK), `place_id` (UUID FK nullable), `stop_order` (int), `name` (string), `location` (Geometry Point 4326), `stop_type` (string), `is_must_visit` (bool), `is_completed` (bool), `is_skipped` (bool), `skip_reason` (string nullable), `planned_arrival_at` (timestamptz nullable), `planned_departure_at` (timestamptz nullable), `dwell_seconds` (int), `wait_seconds` (int).
- `route_legs`: `id` (UUID PK), `route_id` (UUID FK), `leg_order` (int), `from_stop_id` (UUID FK), `to_stop_id` (UUID FK), `transport_mode` (string), `departure_at` (timestamptz), `arrival_at` (timestamptz), `distance_m` (int), `travel_seconds` (int), `geometry` (JSONB GeoJSON LineString), `turn_instructions` (JSONB), `traffic_basis` (string), `parking_hub_id` (UUID FK nullable).
- `route_restrictions`: `id` (UUID PK), `route_id` (UUID FK), `claim_id` (UUID FK nullable), `restriction_type` (string), `geometry` (Geometry 4326), `allowed_modes` (JSONB), `effective_start` (timestamptz nullable), `effective_end` (timestamptz nullable), `was_applied` (bool), `unresolved_reason` (string nullable).

Detour provenance:
There are no artificial `is_detour` or `detour_reason` columns on `route_legs`. Detour provenance is derived directly from:
1. Associated `route_restrictions` where `was_applied == True`.
2. Warnings or diagnostic entries in `routes.warnings` and `routes.diagnostics` recorded during closure avoidance.
3. GeoJSON representation includes applied restrictions as distinct Polygon features so the map displays both the detour trajectory and the closed road exclusion polygon.

### 2. Graph Lifecycle, Dispatch, and Reconciliation

The execution lifecycle for `ROUTE_OPTIMIZER_GRAPH` coordinates across API, orchestrator, and worker:

1. **Acceptance and Idempotency**:
   - `POST /api/v1/optimization/routes` reserves owner scoped idempotency in `api_idempotency` with operation `route_optimization:solve`, key `Idempotency-Key` or generated UUID, and request hash.
   - Atomically within the same transaction, creates `ExecutionModel` with `workflow="ROUTE_OPTIMIZER_GRAPH"`, `status="CREATED"`, `context_version=1`.
   - Stages `EXECUTION_START` event in `outbox_events` and commits. Returns HTTP 202 Accepted with canonical `job_id` and `status_url: /api/v1/jobs/{job_id}`.

2. **Graph Initiation**:
   - `SqsGraphConsumer` receives `EXECUTION_START` and invokes `GraphRunner.run_execution(execution_id)`.
   - `RouteOptimizerGraph` starts at `initialize_route_node`, validates stops, resolves active restrictions from `ClaimVerificationPolicy`, and transitions execution to `RUNNING`.

3. **Solver Task Dispatch and Interrupt**:
   - Node `dispatch_solver_task_node` creates `TaskModel(worker="OPTIMIZER", action="solve")` with input payload in `payload_ref`.
   - Stages `TASK_DISPATCH` event in `outbox_events` targeting `ROUTE_OPTIMIZER_QUEUE_URL`.
   - Updates `ExecutionModel.status = "WAITING_TASKS"`.
   - Flushes checkpointer state atomically and interrupts graph execution, pausing until worker completion.

4. **Worker Execution**:
   - Optimizer worker claims task from SQS under `WorkerFencingService` lease fencing.
   - Solves VRPTW with OR Tools, hydrates geometries and closing windows via Valhalla, and saves `RouteModel`, `RouteStopModel`, `RouteLegModel`, and `RouteRestrictionModel` in PostgreSQL using `RouteRepository`.
   - Commits transaction atomically with task completion, writing `WorkerResultModel` containing `RouteOptimizerWorkerOutput` (carrying `route_id`, `route_version`, `status`, `total_distance_m`, `warnings`, `diagnostics`), and staging `TASK_COMPLETED` receipt in `outbox_events`.

5. **Completion Reconciliation**:
   - Outbox relay publishes `TASK_COMPLETED` receipt to SQS.
   - `CompletionReconciliationService.reconcile_receipt()` processes the receipt, updates checkpoint markers, and checks remaining tasks.
   - **Isolation from Standalone Optimizer Terminalization**: `reconciliation.py` contains terminalization logic for standalone workflows `("ROUTE_OPTIMIZATION", "ROUTE_REPLAN", "OPTIMIZER", "ROUTE_OPTIMIZER")`. Workflow `ROUTE_OPTIMIZER_GRAPH` is intentionally excluded from that list. When `remaining_count == 0` and `execution.status == "WAITING_TASKS"`, `reconciliation.py` transitions `execution.status = "RUNNING"`, stages `EXECUTION_RESUME` in `outbox_events`, and logs `GRAPH_RESUMED`.

6. **Graph Resume and Route Loading**:
   - `SqsGraphConsumer` receives `EXECUTION_RESUME` and calls `GraphRunner.resume_execution(execution_id)`.
   - Graph resumes at `finalize_route_node`.
   - The node retrieves the task output from graph state, parses `RouteOptimizerWorkerOutput`, extracts `output.route_id`, and queries `RouteRepository(session).get_route_by_id(output.route_id)` to load the full persisted route entity.
   - Verifies route status. If `RouteStatus.INFEASIBLE`, preserves the route and diagnostic records without failing the job execution.
   - Records job timeline event `ROUTE_OPTIMIZED` containing `{"route_id": str(route.id), "status": route.status, "version": route.route_version}`.
   - Graph reaches `END`. `GraphRunner` marks `ExecutionModel.status = "SUCCEEDED"`, stages `EXECUTION_SUCCEEDED` event, and records `JOB_COMPLETED`.

### 3. Durable Job to Route Discovery

To eliminate fragile reliance on paginated timeline events:
- `routes.execution_id` is a foreign key on `executions.id` and is indexed.
- The API provides direct, durable job to route lookup through two mechanisms:
  1. `GET /api/v1/jobs/{job_id}` includes `result_ref: {"route_id": str(route.id)}` in its response payload when `execution.status == "SUCCEEDED"`, populated by checking `routes.execution_id == job_id`.
  2. Dedicated query endpoint `GET /api/v1/optimization/routes/by-job/{job_id}` resolves the latest route version for that execution directly, returning HTTP 200 with the route or HTTP 404 if the job has not yet persisted a route.

### 4. Mid Trip Replanning and Version Lineage

Replanning supports both trip scoped and standalone routes:
- `POST /api/v1/optimization/routes/{route_id}/replans` accepts `RouteReplanRequest` with current traveler coordinates, timestamp, and completed stop IDs.
- **Optimistic Concurrency**: If `expected_previous_version` is provided in the request, it must match the current route version. If mismatched, returns HTTP 409 Conflict.
- **Lineage Traversal**: The newly generated route stores `parent_route_id = route_id` and increments `route_version = parent.route_version + 1`. If `trip_id` is present, it maintains sequential ordering under `(trip_id, route_version)`. If `trip_id` is null, the version tree is resolved recursively via `parent_route_id`.
- `GET /api/v1/optimization/routes/{route_id}/versions` traces parent links backward to root (v1) and forward to child replans, returning a version list with:
  - `route_id`, `route_version`, `parent_route_id`, `created_at`.
  - `distance_delta_m`: current minus parent total distance.
  - `duration_delta_seconds`: current minus parent total travel time.
  - `dropped_stop_ids`: stop IDs present in parent but skipped in current.
  - `replan_origin`: coordinates where the replan was initiated.

### 5. Infeasible Route Outcomes versus Job Failures

A strict boundary is maintained between system job failures and infeasible route outcomes:
- **System Job Failure (`execution.status = "FAILED"`)**: Occurs only on unhandled runtime crashes, database connection loss, unrecoverable provider outages, or execution deadline timeouts. No valid route is persisted.
- **Infeasible Route (`execution.status = "SUCCEEDED"`, `route.status = "INFEASIBLE"`)**: The solver ran to completion and mathematically proved that hard constraints (such as mandatory visit windows or missing parking hubs) cannot be satisfied. The route is saved to PostgreSQL with:
  - All requested stops saved in `route_stops`, preserving their locations, requested arrival windows, and setting `is_skipped = True` with `skip_reason`.
  - Diagnostics recorded in `routes.diagnostics` describing the exact constraint violation (e.g., `MANDATORY_STOP_WINDOW_BREACH` or `NO_VERIFIED_PARKING_HUB`).
  - Zero route legs saved.
  - Map endpoint returns HTTP 200 OK with GeoJSON containing Point features for all requested stops marked with error attributes and top level properties explaining the conflict, allowing the frontend map to display where the conflict occurred.

### 6. API Surface

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/api/v1/optimization/routes` | POST | `RouteRequest` payload (stops, mode, window) | `AcceptedRouteResponse` (job_id, status_url) | Bearer JWT | 400 invalid, 401 unauth, 409 conflict |
| `/api/v1/optimization/routes/{route_id}` | GET | `route_id: UUID` path parameter | `OptimizedRoute` domain model | Bearer JWT | 401 unauth, 403 forbidden, 404 missing |
| `/api/v1/optimization/routes/by-job/{job_id}` | GET | `job_id: UUID` path parameter | `OptimizedRoute` domain model | Bearer JWT | 401 unauth, 403 forbidden, 404 missing |
| `/api/v1/optimization/routes/{route_id}/replans` | POST | `route_id: UUID`, `RouteReplanRequest` (coordinates, completed_stops) | `AcceptedRouteResponse` (job_id, status_url) | Bearer JWT | 400 invalid, 401 unauth, 403 forbidden, 404 missing, 409 conflict |
| `/api/v1/optimization/routes/{route_id}/map` | GET | `route_id: UUID`, optional `version: int` | `RouteMapGeoJsonResponse` (FeatureCollection) | Bearer JWT | 401 unauth, 403 forbidden, 404 missing |
| `/api/v1/optimization/routes/{route_id}/versions` | GET | `route_id: UUID` path parameter | `RouteVersionHistoryResponse` (version list, deltas, diffs) | Bearer JWT | 401 unauth, 403 forbidden, 404 missing |

### 7. GeoJSON Map Specification and Presentation

The response from `GET /api/v1/optimization/routes/{route_id}/map` conforms strictly to RFC 7946 GeoJSON and includes simplestyle presentation properties:

- **Top Level Properties**:
  - `route_id`: UUID string.
  - `route_version`: Integer.
  - `parent_route_id`: UUID string or null.
  - `status`: RouteStatus string (`OPTIMAL`, `FEASIBLE`, `PARTIAL`, `INFEASIBLE`).
  - `total_distance_m`: Integer.
  - `total_travel_seconds`: Integer.
  - `fuel_cost`: Decimal string or null.
  - `warnings`: List of warning strings.
  - `diagnostics`: List of diagnostic strings.

- **Stop Point Features**:
  - `geometry`: `{"type": "Point", "coordinates": [longitude, latitude]}` from `route_stops.location`.
  - `properties`:
    - `feature_type`: `"stop"`.
    - `id`: UUID string matching input stop ID.
    - `stop_order`: Integer sequence (1, 2, 3...).
    - `name`: Venue or destination name string.
    - `stop_type`: `"ORIGIN"`, `"VENUE"`, `"PARKING_HUB"`, `"RETURN_ORIGIN"`.
    - `planned_arrival_at`: ISO 8601 string or null.
    - `planned_departure_at`: ISO 8601 string or null.
    - `dwell_seconds`: Integer.
    - `wait_seconds`: Integer.
    - `is_skipped`: Boolean.
    - `skip_reason`: String or null.
    - `marker-color`: `"#2563eb"` (blue for origin/return), `"#059669"` (green for visited venue), `"#d97706"` (amber for parking hub), `"#dc2626"` (red for skipped or infeasible).
    - `marker-symbol`: Stop sequence number string or `"P"` for parking.

- **Leg LineString Features**:
  - `geometry`: `{"type": "LineString", "coordinates": [[lon, lat], ...]}` extracted directly from `route_legs.geometry`.
  - `properties`:
    - `feature_type`: `"leg"`.
    - `leg_order`: Integer.
    - `from_stop_id`: UUID string.
    - `to_stop_id`: UUID string.
    - `transport_mode`: `"DRIVE"` or `"WALK"`.
    - `distance_m`: Integer.
    - `travel_seconds`: Integer.
    - `traffic_basis`: String.
    - `stroke`: `"#3b82f6"` (blue for driving), `"#10b981"` (emerald for walking), `"#f59e0b"` (amber for route with applied road closure detour).
    - `stroke-width`: `4` (driving), `3` (walking).
    - `stroke-dasharray`: `"none"` for normal driving, `"5, 5"` for walking legs.

- **Restriction Polygon Features**:
  - Included when `route_restrictions` has records where `was_applied == True`.
  - `geometry`: Polygon or MultiPolygon GeoJSON extracted from `route_restrictions.geometry`.
  - `properties`:
    - `feature_type`: `"restriction"`.
    - `restriction_type`: `"ROAD_CLOSURE"`.
    - `fill`: `"#ef4444"`.
    - `fill-opacity`: `0.35`.
    - `stroke`: `"#b91c1c"`.
    - `description`: Closure notice string.

### 8. Frontend Architecture and Map Experience

The frontend client integrates with the route optimization vertical slice through:

1. **Submission**:
   - Client submits `RouteRequest` via `POST /api/v1/optimization/routes` with JWT Bearer authentication.
   - Receives HTTP 202 Accepted with `job_id` and `status_url`.
   - Stores `active_job_id` in session storage or URL query parameter (`?job_id=...`) for refresh recovery.

2. **Authenticated Polling and Refresh Recovery**:
   - Client polls `GET /api/v1/jobs/{job_id}` at exponential intervals (1s, 2s, 3s, max 5s).
   - Upon page refresh, if `job_id` exists in the URL or storage, polling resumes immediately.
   - If job status is `SUCCEEDED`, extracts `route_id` from `result_ref` (or queries `GET /api/v1/optimization/routes/by-job/{job_id}`), updates URL to `/trips/{trip_id}/route?route_id={route_id}`, and clears polling timer.
   - If job status is `FAILED`, renders an error card with retry button.

3. **Map Retrieval and Leaflet Rendering**:
   - Client fetches `GET /api/v1/optimization/routes/{route_id}/map`.
   - `CorridorMap.tsx` receives the GeoJSON FeatureCollection.
   - Tile layer attribution is strictly preserved: `attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'`.
   - Popups render safely: popup content is constructed using text escaped DOM elements or safe React components to prevent XSS from unescaped stop names or external sources.
   - Displays trip summary card with total distance, elapsed duration, fuel cost estimate, and status badge (`FEASIBLE`, `PARTIAL`, `INFEASIBLE`).

4. **Replan and Version Selector**:
   - Replan panel allows user to simulate live position or use device geolocation.
   - User clicks "Replan Route", sending current coordinates and completed stop IDs to `POST /api/v1/optimization/routes/{route_id}/replans`.
   - Polls new `job_id` to completion and fetches updated map.
   - Version selector dropdown lists available versions fetched from `GET /api/v1/optimization/routes/{route_id}/versions`, allowing the traveler to toggle between original and detoured routes to inspect changes.

### 9. Value Sourcing

| Action | Value produced / displayed | Source |
|---|---|---|
| Map endpoint | Stop Point coordinates (`[lng, lat]`) | Database column `route_stops.location` extracted as Point |
| Map endpoint | Leg LineString coordinates (`[[lng, lat], ...]`) | Database column `route_legs.geometry` (`coordinates` key) |
| Map endpoint | Stop sequence number and visit times | Database columns `route_stops.stop_order`, `planned_arrival_at`, `planned_departure_at` |
| Map endpoint | Transport mode and leg metrics | Database columns `route_legs.transport_mode`, `distance_m`, `travel_seconds` |
| Map endpoint | Detour status and closure polygon | Database table `route_restrictions` (`was_applied == True`, `geometry`) |
| Map endpoint | Presentation styles (`stroke`, `marker-color`) | Derived deterministically from `transport_mode` and applied restrictions |
| Version endpoint | Parent route identifier | Database column `routes.parent_route_id` |
| Version endpoint | Distance and duration deltas | Computed from current versus parent `total_distance_m` and `total_travel_seconds` |
| Version endpoint | Dropped stops comparison | Set difference between parent visited stop IDs and current visited stop IDs |
| Job discovery | Resulting route identifier | Indexed lookup on `routes.execution_id` matching `job_id` |

### 10. Key Invariants

- Coordinate order in all GeoJSON responses is strictly `[longitude, latitude]` adhering to RFC 7946.
- Every leg LineString must derive from authoritative Valhalla provider routing; synthetic straight lines are forbidden in production outputs.
- Infeasible routes must still produce valid GeoJSON stop features with diagnostics rather than raising 500 server errors or empty arrays.
- Tenant isolation must be verified before any route or map data is returned; cross tenant requests receive HTTP 403 Forbidden.
- Replan versions must increment `route_version` and point directly to their immediate `parent_route_id`.

### 11. Security Model

- All route submission, polling, map retrieval, and replan endpoints require authenticated Bearer JWT tokens validated against SWYRA Auth JWKS.
- The `owner_id` extracted from the validated token must match `routes.owner_id` and `executions.owner_id`.
- Tenant boundaries return HTTP 403 Forbidden on mismatch; non existent routes return HTTP 404 Not Found.
- Sensitive traveler home or personal departure coordinates are protected behind authenticated endpoints and never exposed publicly.

### 12. Configuration Required

- Reuses existing configuration: `DATABASE_URL`, `SQS_QUEUE_URL`, `ROUTE_OPTIMIZER_QUEUE_URL`, and `VALHALLA_ENDPOINT_URL`. No new environment variables required.

### 13. Critical Test Scenarios

- **Happy Path Vertical Slice**: Submit multi stop itinerary -> `RouteOptimizerGraph` executes -> worker solves -> reconciliation resumes graph -> job succeeds -> map endpoint returns valid GeoJSON with driving and walking legs, verifying **AC-1**, **AC-2**, **AC-3**, **AC-4**, and **AC-5**.
- **Changed Road Detour and Replan**: Mid trip replan with verified road closure calculates detour around exclusion polygon -> links `parent_route_id` -> version history reflects distance and duration delta -> map renders detoured path and closure polygon, verifying **AC-5**, **AC-6**, and **AC-7**.
- **Multimodal Parking and Vehicle Continuity**: Venue in pedestrian zone triggers parking hub injection -> driving leg ends at parking hub -> walking leg connects parking to venue -> return leg returns to parked vehicle before driving home, verifying **AC-5** and **AC-10**.
- **PARTIAL Solution with Optional Stop Drop**: Itinerary with optional stop exceeding closing window drops optional stop -> returns `RouteStatus.PARTIAL` with detoured provider geometries on surviving legs, verifying **AC-5** and **AC-8**.
- **Infeasible Mandatory Constraint Outcome**: Mandatory stop closes before arrival -> worker persists infeasible route with diagnostics -> job execution marks `SUCCEEDED` -> map endpoint returns HTTP 200 with venue stop marked infeasible and error reason, verifying **AC-8**.
- **Tenant Fencing and Security Isolation**: User B attempts to access route map or version history belonging to User A -> receives HTTP 403 Forbidden, verifying **AC-9**.
- **Frontend Map and Browser E2E**: Playwright browser test drives real frontend, backend, and PostgreSQL -> submits route -> verifies polling animation -> inspects Leaflet map layers, stop pins, safe popups, and attribution without console errors, verifying **AC-10** and **AC-11**.
- **Connected Valhalla Verification**: Separately gated live provider suite under `RUN_CONNECTED_VALHALLA_TESTS=1` validates live Valhalla matrix calculation, turn by turn geometry decoding, and closure detour routing, verifying **AC-11**.

## Build plan

The build plan follows the Tracer Bullet approach, establishing a thin end to end thread through every layer first, then expanding capabilities:

### Bounded First Development Slice (Slice 1): Core Submit -> Graph -> Worker -> Poll -> Map Vertical Slice
1. Database migration: Create Alembic migration adding `parent_route_id` nullable foreign key and index to table `routes`, satisfies **AC-6**.
2. Domain contracts and GeoJSON assembler: Implement `RouteMapGeoJsonResponse`, GeoJSON feature models with simplestyle attributes, and service assembling GeoJSON from `route_stops.location` (PostGIS Point) and `route_legs.geometry` (JSONB LineString), satisfies **AC-5**.
3. LangGraph route optimizer graph: Implement `RouteOptimizerGraph` compiling `initialize_route_node`, `dispatch_solver_task_node` (staging outbox task for `ROUTE_OPTIMIZER_QUEUE_URL` and setting `WAITING_TASKS`), and `finalize_route_node` (consuming `RouteOptimizerWorkerOutput.route_id`, loading route via `RouteRepository`, and marking `SUCCEEDED`), satisfies **AC-1**, **AC-3**.
4. REST API routes and durable discovery: Update `POST /api/v1/optimization/routes` with atomic owner scoped idempotency reservation and graph initiation; add `GET /api/v1/optimization/routes/{route_id}/map` and direct job to route lookup via indexed `routes.execution_id`, satisfies **AC-2**, **AC-4**, **AC-5**, **AC-9**.
5. Backend integration trace: Write automated integration test on live PostgreSQL proving complete lifecycle from `POST /api/v1/optimization/routes` through SQS task dispatch, worker claim, solve, reconciliation resume, and GeoJSON map generation, satisfies **AC-1**, **AC-2**, **AC-3**, **AC-4**, **AC-5**.

### Subsequent Development Slices
6. Slice 2: Mid trip replanning engine, parent lineage tracking, version deltas, and `GET /api/v1/optimization/routes/{route_id}/versions`, satisfies **AC-6**, **AC-7**.
7. Slice 3: Infeasible and partial route handling, constraint diagnostic preservation, and failure separation, satisfies **AC-8**.
8. Slice 4: Frontend CorridorMap GeoJSON layer integration, safe popups, tile attribution, polling hook, and refresh recovery, satisfies **AC-10**.
9. Slice 5: Browser end to end test suite across real local frontend, backend, and PostgreSQL with injected provider, plus connected Valhalla verification gate, satisfies **AC-11**.

## Consequences

**Positive**:
- Establishes a formal LangGraph state machine for route optimization with transactional checkpointing, clean resume semantics, and crash recovery.
- Provides a clean, standards compliant GeoJSON API that powers interactive map visualizations across web and mobile clients.
- Gives travelers clear visual explanations of parking transitions, walking legs, and detour reroutes with full version lineage.
- Closes Phase 8 of the platform development roadmap with a verified vertical slice.

**Negative / tradeoffs**:
- Adds one new foreign key column (`parent_route_id`) and index to the `routes` table.
- Graph execution adds minor orchestrator outbox dispatch overhead compared to direct worker invocation.

**Neutral**:
- Reuses existing PostGIS tables, JSONB leg geometries, and Valhalla client without requiring new cloud infrastructure or external map provider subscriptions.
- AWS deployment and later LLM, weather, and media capabilities remain deferred and outside scope.

## Follow-up

- [ ] Benchmark PostGIS and JSONB GeoJSON assembly latency under high concurrency with routes containing 30 or more stops.
- [ ] Evaluate vector tile generation (MVT) if future itineraries require rendering hundreds of simultaneous routes on region wide maps.
