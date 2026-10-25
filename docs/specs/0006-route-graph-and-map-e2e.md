# 0006. Route Graph, Map GeoJSON, and End to End Vertical Slice

**Date**: 2026-10-25
**Status**: Proposed

## Summary

This specification defines the route orchestration graph, GeoJSON map visualization, and complete end to end execution slice for SWENA travel planning. It wraps the asynchronous route optimizer worker inside a dedicated LangGraph state machine with transactional checkpointing, supporting independent itinerary optimization and mid trip replanning. A new route map endpoint compiles PostGIS route geometries into standards compliant GeoJSON FeatureCollections, styling driving, walking, detour, and parking legs for direct browser rendering. The existing frontend CorridorMap component is extended to consume this GeoJSON stream, enabling travelers to inspect stops, parking transitions, and detour schedules visually.

## Context

The route optimizer engine built in specification 0005 solves multi stop Vehicle Routing Problems with Time Windows, parking hub injection, road closure avoidance, and mid trip replanning. However, the optimizer currently runs as an isolated worker claimed from SQS queues, with no formal graph orchestration coordinating its lifecycle within the platform execution engine. The platform needs a stateful workflow graph that manages the complete route lifecycle, tracks execution checkpoints, and exposes user facing interfaces for map visualization and route revision tracking.

Travelers and downstream planning agents cannot easily interpret raw JSON stop coordinates and polyline strings without spatial visualization. A complete travel itinerary consists of distinct transport phases, including highway driving, pedestrian precinct walking, parking transitions, and detour segments avoiding road closures. Without an authoritative GeoJSON API surface, frontend clients must assemble disjointed geometry strings, duplicating styling logic and risking coordinate discrepancies.

Furthermore, dynamic mid trip replanning requires clear lineage visibility. When an active road restriction or schedule delay triggers a reroute, the traveler needs to inspect what changed between the original plan and the updated itinerary. The system must record direct parent route lineage, preserve historical stop timelines, and expose version comparison data so travelers understand why specific stops were reordered or dropped.

Finally, architectural phase 8 mandates an end to end vertical slice proving that a route optimization request moves seamlessly from API acceptance through orchestrator graph execution, worker constraint solving, PostGIS geometry assembly, and interactive map display on live databases.

## Requirements

**User stories**:
- As a traveler, I want to view my optimized multi stop itinerary on an interactive map so that I can see the exact driving paths, walking legs, parking locations, and schedule timings.
- As a traveler encountering a road closure or delay, I want to submit my live location for a mid trip replan so that I receive an updated itinerary avoiding the obstacle while preserving completed stops.
- As an API client or frontend application, I want a single GeoJSON endpoint for any route so that I can render styled map layers without custom geometry parsing or styling heuristics.

**Acceptance criteria**:
- **AC-1**: `RouteOptimizerGraph` compiles as a dedicated LangGraph state machine with nodes for route initialization, solver task dispatch, worker result waiting, GeoJSON map hydration, and replan reconciliation, persisting checkpoint state safely using `TransactionBoundCheckpointer`.
- **AC-2**: Asynchronous endpoint `POST /api/v1/optimization/routes` initiates `RouteOptimizerGraph` under execution workflow `ROUTE_OPTIMIZER_GRAPH`, returns HTTP 202 Accepted with canonical `job_id`, and supports progress polling through `GET /api/v1/jobs/{job_id}`.
- **AC-3**: Endpoint `GET /api/v1/optimization/routes/{route_id}/map` returns a standards compliant GeoJSON FeatureCollection with LineString features for driving, walking, and detour legs, plus Point features for stops and parking hubs, adhering to simplestyle presentation attributes.
- **AC-4**: Mid trip replanning endpoint `POST /api/v1/optimization/routes/{route_id}/replans` accepts live traveler coordinates, freezes visited stops, checks active road restrictions, computes an updated route version linked via `parent_route_id`, and returns HTTP 202 Accepted.
- **AC-5**: Lineage endpoint `GET /api/v1/optimization/routes/{route_id}/versions` returns the chronological version history of a route with parent pointers, distance and duration deltas, and dropped stop differences.
- **AC-6**: Strict tenant ownership fencing is enforced across all route endpoints, returning HTTP 403 Forbidden for cross tenant access attempts and HTTP 404 Not Found when a route does not exist.
- **AC-7**: Infeasible or failed routes return HTTP 200 OK from the map endpoint with a valid GeoJSON FeatureCollection containing requested stop Points marked with error attributes and top level properties explaining the constraint violation.
- **AC-8**: The frontend Leaflet CorridorMap component consumes the GeoJSON FeatureCollection directly, rendering multi color paths (driving in blue, walking in emerald, detours in amber), custom stop pins with visit sequence numbers, parking icons, and popup cards showing arrival, wait, and dwell times.
- **AC-9**: An automated integration test suite verifies the complete vertical slice on live PostgreSQL, tracing execution from initial route submission through worker SQS completion, PostGIS geometry hydration, and map GeoJSON generation.

## Options considered

### Option 1: Dedicated RouteOptimizerGraph with dynamic PostGIS GeoJSON assembly and Leaflet map extension

Implement a standalone LangGraph workflow (`RouteOptimizerGraph`) in the orchestrator, managed under execution workflow `ROUTE_OPTIMIZER_GRAPH`. Assemble map GeoJSON dynamically from `route_stops` and `route_legs` tables using PostGIS `ST_AsGeoJSON` with property hydration. Extend the existing Leaflet CorridorMap frontend component to render the GeoJSON FeatureCollection with styled layers.

**Pros**:
- Modular design allows the route graph to run standalone or compose cleanly as a subgraph inside future trip planner workflows.
- PostGIS dynamic assembly eliminates redundant stored JSON columns and ensures geometries remain synchronized with relational tables.
- Reuses existing proven Leaflet and Tailwind components in the frontend with zero added third party map libraries.

**Cons**:
- Requires coordinating state handoffs between orchestrator outbox events and the worker queue.

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

Dynamic PostGIS GeoJSON assembly using `ST_AsGeoJSON` queries is vastly superior to persisting pre computed GeoJSON blobs in the database. The `route_stops` and `route_legs` tables already store authoritative PostGIS geometries with GiST spatial indexes. Generating the GeoJSON FeatureCollection on read time guarantees total consistency, avoids schema duplication, and executes in single digit milliseconds for typical itineraries of 5 to 25 stops.

Extending the existing Leaflet CorridorMap component maintains UI design consistency across the application. The frontend already includes Leaflet, custom CartoDB dark matter tile configuration, and Tailwind styling. Consuming standards compliant GeoJSON directly allows the frontend map to display multi color travel modes, closure detours, and parking transitions with minimal code complexity.

## Feature design

**Data model sketch**:
- Database table `routes` receives an Alembic migration adding `parent_route_id`:
  - `parent_route_id`: UUID, nullable, foreign key referencing `routes(id)` on delete set null.
  - Index added on `parent_route_id` for fast lineage queries.
  - Unique constraint `uq_routes_trip_version` on `(trip_id, route_version)` preserved alongside existing fields `departure_at`, `return_deadline`, `total_distance_m`, `total_travel_seconds`, and `fuel_amount`.
- Tables `route_stops`, `route_legs`, and `route_restrictions` remain unchanged:
  - `route_stops`: `id`, `route_id`, `place_id`, `stop_order`, `name`, `location` (Geometry Point 4326), `stop_type`, `is_must_visit`, `is_completed`, `is_skipped`, `skip_reason`, `planned_arrival_at`, `planned_departure_at`, `dwell_seconds`, `wait_seconds`.
  - `route_legs`: `id`, `route_id`, `leg_order`, `from_stop_id`, `to_stop_id`, `transport_mode`, `departure_at`, `arrival_at`, `distance_m`, `travel_seconds`, `geometry` (JSONB GeoJSON LineString), `turn_instructions`, `traffic_basis`.
  - `route_restrictions`: `id`, `route_id`, `claim_id`, `restriction_type`, `geometry` (Geometry Polygon 4326), `description`.
- Table `executions` reuses workflow identifier `ROUTE_OPTIMIZER_GRAPH` with `TransactionBoundCheckpointer`.

**State transitions**:
`RouteOptimizerGraph` manages the following state progression:
1. `INITIALIZE`: Validates route inputs, queries active road restrictions from claims, reserves API idempotency, transitions execution to `RUNNING`.
2. `DISPATCH_SOLVER`: Stages `OPTIMIZER` worker task in transactional outbox for solve or replan action, transitions execution to `WAITING_TASKS`.
3. `AWAIT_RESULT`: Resumes execution upon receiving `TASK_COMPLETED` receipt, unpacks `OptimizedRoute` domain result, and validates status.
4. `HYDRATE_MAP`: Queries PostGIS geometry for stops and legs, verifies spatial continuity, records job timeline event `ROUTE_OPTIMIZED`.
5. `RECONCILE_REPLAN`: For replanning requests, links `parent_route_id`, increments `route_version`, and marks execution `COMPLETED`.

**API surface**:
| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/api/v1/optimization/routes` | POST | `RouteRequest` payload (stops, mode, window) | `AcceptedRouteResponse` (job_id, status_url) | Bearer JWT | 400 invalid, 401 unauth, 409 conflict |
| `/api/v1/optimization/routes/{route_id}` | GET | `route_id: UUID` path parameter | `OptimizedRoute` domain model | Bearer JWT | 401 unauth, 403 forbidden, 404 missing |
| `/api/v1/optimization/routes/{route_id}/replans` | POST | `route_id: UUID`, `RouteReplanRequest` (coordinates, completed_stops) | `AcceptedRouteResponse` (job_id, status_url) | Bearer JWT | 400 invalid, 401 unauth, 403 forbidden, 404 missing |
| `/api/v1/optimization/routes/{route_id}/map` | GET | `route_id: UUID`, optional `version: int` | `RouteMapGeoJsonResponse` (FeatureCollection) | Bearer JWT | 401 unauth, 403 forbidden, 404 missing |
| `/api/v1/optimization/routes/{route_id}/versions` | GET | `route_id: UUID` path parameter | `RouteVersionHistoryResponse` (version list, deltas, diffs) | Bearer JWT | 401 unauth, 403 forbidden, 404 missing |

**Value sourcing**:
| Action | Value produced / displayed | Source |
|---|---|---|
| Map endpoint | Stop Point coordinates (`[lng, lat]`) | Database column `route_stops.location` via `ST_AsGeoJSON` or `extract_point` |
| Map endpoint | Leg LineString coordinates (`[[lng, lat], ...]`) | Database column `route_legs.geometry` (`coordinates` key) |
| Map endpoint | Stop sequence number and visit times | Database columns `route_stops.stop_order`, `planned_arrival_at`, `planned_departure_at` |
| Map endpoint | Transport mode and detour flag | Database columns `route_legs.transport_mode`, `is_detour`, `detour_reason` |
| Map endpoint | Presentation styles (`stroke`, `marker-color`) | Derived deterministically from `transport_mode` and `is_detour` properties |
| Version endpoint | Parent route identifier | Database column `routes.parent_route_id` |
| Version endpoint | Distance and duration deltas | Computed from current versus parent `total_distance_m` and `total_travel_seconds` |
| Version endpoint | Dropped stops comparison | Set difference between parent visited stop IDs and current visited stop IDs |
| Job polling | Execution status and timeline events | Database tables `executions.status` and `job_events` |

**Key invariants**:
- All coordinate arrays in GeoJSON outputs must use `[longitude, latitude]` order adhering to RFC 7946 GeoJSON standard.
- Every leg LineString must derive from authoritative Valhalla provider routing; synthetic haversine straight lines are forbidden in production map output.
- Replan versions must record an incremented `route_version` and point directly to their immediate `parent_route_id`.
- Tenant ownership must be checked before returning any route data; cross tenant requests must receive HTTP 403 Forbidden.
- Infeasible routes must still return valid GeoJSON FeatureCollections representing requested stops with error metadata rather than crashing or throwing 500 errors.

**Security model**:
- Every route query requires an authenticated Bearer JWT token validated against the platform JWKS.
- The `owner_id` extracted from the verified token must match `routes.owner_id`.
- Private traveler departure and return coordinates are protected behind authenticated tenant boundaries and never exposed without authentication.

**Configuration required**:
- No new environment variables required; reuses existing `DATABASE_URL`, `VALHALLA_BASE_URL`, and `ROUTE_OPTIMIZER_QUEUE_URL`.

**Critical test scenarios**:
- Happy path: Submitting a valid multi stop route request executes through `RouteOptimizerGraph`, completes solver execution, and returns a valid GeoJSON FeatureCollection with driving and walking legs, verifying **AC-1**, **AC-2**, **AC-3**, and **AC-9**.
- Mid trip replan lineage: Submitting a replan request with live traveler coordinates freezes completed stops, detours around a simulated road closure, links `parent_route_id`, and appears in the versions history endpoint, verifying **AC-4** and **AC-5**.
- Tenant security fencing: Querying a route map or version history belonging to another user returns HTTP 403 Forbidden, verifying **AC-6**.
- Infeasible route handling: A route with impossible time windows returns HTTP 200 OK from the map endpoint with stop points marked skipped and properties explaining the window violation, verifying **AC-7**.
- Frontend map rendering: The Leaflet CorridorMap renders driving, walking, detour, and parking features correctly without console errors or coordinate inversion, verifying **AC-8**.

## Build plan

1. Database migration: Create Alembic migration adding `parent_route_id` foreign key and index to table `routes`, satisfies **AC-4**, **AC-5**.
2. Domain contracts and GeoJSON assembler: Implement `RouteMapGeoJsonResponse`, GeoJSON feature models with simplestyle attributes, and PostGIS `ST_AsGeoJSON` assembly service, satisfies **AC-3**, **AC-7**.
3. LangGraph route optimizer graph: Implement `RouteOptimizerGraph` with nodes for request initialization, task dispatch, result awaiting, map hydration, and replan reconciliation under `TransactionBoundCheckpointer`, satisfies **AC-1**, **AC-2**.
4. API endpoints and orchestrator wiring: Update `POST /api/v1/optimization/routes` and `POST .../replans` to trigger the graph, and add `GET /api/v1/optimization/routes/{route_id}/map` and `GET .../versions` with tenant fencing, satisfies **AC-2**, **AC-3**, **AC-4**, **AC-5**, **AC-6**, **AC-7**.
5. Frontend CorridorMap GeoJSON integration: Extend `frontend/src/components/map/CorridorMap.tsx` with GeoJSON layer rendering, distinctive transit mode colors, stop marker popups, and route summary display, satisfies **AC-8**.
6. End to end vertical slice trace test: Implement comprehensive integration test verifying the complete workflow from API submission to map GeoJSON on live PostgreSQL, satisfies **AC-9**.

## Consequences

**Positive**:
- Establishes a formal LangGraph state machine for route optimization with transactional checkpointing and crash recovery.
- Provides a clean, standards compliant GeoJSON API that powers interactive map visualizations across web and mobile clients.
- Gives travelers clear visual explanations of parking transitions, walking legs, and detour reroutes with full version lineage.
- Closes Phase 8 of the platform development roadmap with a verified vertical slice.

**Negative / tradeoffs**:
- Adds one new foreign key column (`parent_route_id`) and index to the `routes` table.
- Graph execution adds minor orchestrator outbox dispatch overhead compared to direct worker invocation.

**Neutral**:
- Reuses existing PostGIS tables and Valhalla client without requiring new cloud infrastructure or external map provider subscriptions.

## Follow-up

- [ ] Benchmark PostGIS `ST_AsGeoJSON` query performance under high concurrency with routes containing 30 or more stops.
- [ ] Investigate vector tile generation (MVT) if future itineraries require rendering hundreds of simultaneous routes on region wide maps.
