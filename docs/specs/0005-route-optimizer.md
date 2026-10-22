# 0005. Route Optimizer and Multimodal Itinerary Planning

**Date**: 2026-10-22
**Status**: Proposed

## Summary

This specification defines the route optimization and multimodal itinerary planning engine for SWENA. The engine accepts multi stop travel requests with arrival deadlines, opening hours, dwell times, and transit modes, producing temporally valid itineraries that minimize travel friction. A hybrid solver combines Google OR Tools constraint programming with Valhalla road and pedestrian routing matrices, enforcing strict priorities across must visit and optional stops.

The system handles real world travel constraints including pedestrian zones, road closures, and mid trip delays. When a destination lies within a pedestrian precinct, the planner injects verified parking hubs, converting vehicle transit into driving, parking, and walking legs. Verified spatial claims from specification 0002 inform time varying road restrictions so routes avoid closed passes or restricted traffic zones. Mid trip replanning preserves historical completed stops, takes the traveler current location as origin, and publishes an immutable incremented route version. All solving runs asynchronously under worker lease fencing, writing route geometries and schedules to PostgreSQL and PostGIS.

## Context

The SWENA travel intelligence platform orchestrates complex travel research across durable workflow execution (specification 0001), source evidence collection and claim verification (specification 0002), dynamic browser automation (specification 0003), and category decision routing (specification 0004). While these systems gather authoritative opening hours, place geometries, and local constraints, the platform currently lacks the ability to synthesize this verified knowledge into an executable, temporally feasible multi stop route.

Travelers routinely face complex routing dilemmas that simple point to point mapping engines cannot solve. A day itinerary often includes six to ten distinct stops across different neighborhoods, each stop having fixed opening hours, mandatory dwell durations, and varying transport accessibility. Some historic urban centers prohibit private cars entirely, requiring transitions between personal vehicles and walking. In mountainous or coastal areas, seasonal passes close on scheduled dates, and unexpected roadworks or weather hazards invalidate static shortest path calculations.

Allowing general large language models to construct travel itineraries causes hallucinated travel times, impossible physical sequencing, and ignored opening schedules. Conversely, simple travelling salesperson heuristics fail because they cannot account for time windows, vehicle access boundaries, multimodal parking transitions, or dynamic road closures. Without an authoritative optimization engine, generated itineraries frustrate travelers with missed reservations, inaccessible roads, and unrealistic schedules.

The route optimization capability must integrate cleanly with the existing platform infrastructure. Solves must execute asynchronously under worker lease fencing so heavy graph computations never block user facing web services. Calculated routes and turn by turn geometries must persist in PostgreSQL using PostGIS spatial types, enabling durable replanning and progress tracking throughout the trip lifecycle.

## Requirements

**User stories**:
- As a traveler, I want my multi stop itinerary organized in the most efficient sequence so that I spend minimal time transit and never arrive when a venue is closed.
- As a road trip driver, I want the planner to inject parking hubs before pedestrian destinations so that I do not get stranded at vehicle restricted city gates.
- As an active traveler experiencing unexpected delays, I want to replan my remaining day from my current live location without losing credit for stops I already completed.
- As a travel orchestrator, I want mathematical proof of route feasibility and explicit explanations when optional stops cannot fit within time budgets.

**Acceptance criteria**:
- **AC-1**: SWENA defines internal typed domain contracts (`RouteRequest`, `RouteStopInput`, `OptimizedRoute`, `ScheduledStopOutput`, `RouteLegOutput`, `RouteObjective`, `RouteStatus`, `SolverStatus`, `StopType`, `TrafficBasis`) in `contracts/routing.py` that encapsulate multi stop itinerary inputs and outputs.
- **AC-2**: The route optimizer executes a hybrid constraint solver combining Google OR Tools Operations Research solver with Valhalla routing matrices, enforcing time windows, dwell durations, capacity constraints, and user chosen objectives (`FASTEST`, `SHORTEST`, `BALANCED`).
- **AC-3**: The optimization engine ingests verified spatial claims from specification 0002, applying time varying penalties or total avoidance to road segments during active closure or restriction windows.
- **AC-4**: When a destination is flagged as pedestrian only or vehicle restricted, the solver automatically identifies a verified parking hub within walking threshold, injecting a `PARKING_HUB` stop and splitting the journey into driving, parking, walking, and return legs.
- **AC-5**: The solver enforces strict priority partitioning: must visit stops are hard constraints that must be fulfilled, while optional stops have penalty costs allowing graceful omission with explicit `skip_reason` codes; if must visit stops are temporally unreachable, the solver returns status `INFEASIBLE` with actionable repair proposals.
- **AC-6**: Route records, scheduled stops, legs with GeoJSON LineString geometries, and applied restrictions persist in PostgreSQL tables (`routes`, `route_stops`, `route_legs`, `route_restrictions`) with PostGIS spatial indices and foreign key integrity.
- **AC-7**: The routing adapter connects to Valhalla for road and pedestrian distance and duration matrices, providing defensive circuit breaking, socket timeouts, and offline mock fixtures for reproducible testing.
- **AC-8**: Mid trip replanning accepts current traveler coordinates, freezes completed stops with historical timestamps, treats the current position as the new origin, optimizes remaining stops, and writes an immutable incremented `route_version` referencing `previous_route_version`.
- **AC-9**: Route optimization tasks execute asynchronously via an `OPTIMIZE.solve` worker claimed through `WorkerFencingService` on SQS, exposing REST endpoints for asynchronous submission (`POST /api/v1/optimization/routes`), job polling (`GET /api/v1/jobs/{job_id}`), route retrieval (`GET /api/v1/optimization/routes/{route_id}`), and replanning (`POST /api/v1/optimization/routes/{route_id}/replans`).

## Options considered

### Option 1: Hybrid Google OR Tools solver with Valhalla road and walking matrices (Chosen)

Combine Google OR Tools vehicle routing solver with a local or containerized Valhalla routing engine. Valhalla computes realistic driving and pedestrian distance and duration matrices using OpenStreetMap road topologies, while OR Tools executes constraint programming across time windows, stop priorities, dwell durations, and multimodal transitions.

**Pros**:
- Handles complex vehicle routing problem with time windows (VRPTW) constraints natively with high mathematical efficiency.
- Valhalla provides unified multi modal routing (driving, walking, transit) with turn by turn geometries and local matrix generation.
- Full offline reproducibility and independence from external recurring API call costs or rate limits.
- Supports custom edge penalization and time based restrictions derived from verified platform claims.

**Cons**:
- Requires packaging or hosting Valhalla routing data files and the OR Tools native binary extensions.
- Solver configuration requires tuning hyperparameters (first solution strategy, local search metaheuristic, time limits).

### Option 2: Pure heuristic solver in Python (Nearest Neighbor with 2-Opt)

Implement an in process Python heuristic solver using simple greedy nearest neighbor construction followed by 2-Opt local search refinement, calculating point distances using haversine or basic routing APIs.

**Pros**:
- Zero external solver dependencies; pure Python implementation with minimal packaging footprint.
- Fast execution for very small stop counts (fewer than five stops).

**Cons**:
- Fails completely on time window feasibility, frequently stranding users at closed venues or violating arrival deadlines.
- Inability to handle multimodal vehicle transitions, parking hub injection, or complex priority dropping.
- Heuristic solutions degrade severely as stop counts exceed six destinations.

### Option 3: External proprietary routing API (Google Maps Route Optimization API)

Delegate all multi stop sequencing and matrix calculation to a managed cloud routing API such as Google Maps Platform Route Optimization API.

**Pros**:
- Fully managed cloud infrastructure with real time predictive traffic models worldwide.
- No local solver installation or map data maintenance required.

**Cons**:
- Expensive commercial pricing per optimized stop, creating prohibitive operational costs at scale.
- Black box optimization logic prevents custom parking hub injection or dynamic claim restriction weighting.
- External dependency introduces vendor lock in and network latency vulnerabilities for core itinerary planning.

## Decision

**Chosen option**: Option 1: Hybrid Google OR Tools solver with Valhalla road and walking matrices

We will integrate Google OR Tools as the primary constraint programming solver and Valhalla as the multimodal matrix and path provider, executing asynchronously within dedicated worker tasks under PostgreSQL lease fencing.

## Rationale

Travel itinerary optimization is fundamentally a vehicle routing problem with time windows, stop priorities, and multimodal transitions. Heuristic methods (Option 2) cannot guarantee arrival within narrow opening hours or satisfy hard departure deadlines. Commercial cloud APIs (Option 3) introduce steep recurring costs, lack support for custom spatial claim restrictions, and cannot inject localized parking hubs dynamically.

Google OR Tools provides battle tested, highly optimized constraint programming primitives that solve multi stop scheduling with time windows in seconds. Pairing OR Tools with Valhalla provides realistic road and pedestrian network awareness. Valhalla generates accurate distance and duration matrices that reflect physical road speeds, one way systems, and pedestrian paths. This hybrid architecture ensures SWENA produces mathematically optimal, physically realistic travel schedules while maintaining complete control over domain rules, cost weighting, and privacy.

## Feature design

### Data model sketch

The route optimization engine persists data in four core tables in PostgreSQL with PostGIS extensions:

```text
Table: routes
  id: uuid (PK, required)
  owner_id: text (required, indexed)
  execution_id: text (nullable, indexed, FK to orchestrator execution)
  trip_id: text (nullable, indexed)
  route_version: integer (required, default 1)
  status: text (required, enum: FEASIBLE, PARTIAL, INFEASIBLE, UNAVAILABLE)
  solver_status: text (required, enum: OPTIMAL, FEASIBLE, TIME_LIMIT, INFEASIBLE, ERROR)
  objective: text (required, enum: FASTEST, SHORTEST, BALANCED)
  departure_at: timestamptz (required)
  return_deadline: timestamptz (nullable)
  actual_return_at: timestamptz (nullable)
  total_distance_m: integer (required, default 0)
  total_travel_seconds: integer (required, default 0)
  total_dwell_seconds: integer (required, default 0)
  total_wait_seconds: integer (required, default 0)
  total_elapsed_seconds: integer (required, default 0)
  driving_distance_m: integer (required, default 0)
  walking_distance_m: integer (required, default 0)
  fuel_amount_minor: integer (nullable, estimated fuel expense)
  fuel_currency: text (nullable, default 'USD')
  fuel_basis: text (nullable, explanation of consumption rate used)
  warnings: jsonb (required, default '[]'::jsonb)
  assumptions: jsonb (required, default '[]'::jsonb)
  previous_route_version: integer (nullable, tracks replan history)
  created_at: timestamptz (required, default now())
  updated_at: timestamptz (required, default now())
  Constraint: unique(trip_id, route_version) when trip_id is not null

Table: route_stops
  id: uuid (PK, required)
  route_id: uuid (required, FK to routes.id on delete cascade, indexed)
  place_id: text (nullable, indexed, FK to places.id)
  stop_order: integer (required, sequence index 0..N)
  name: text (required)
  location: geometry(Point, 4326) (required, spatial index)
  stop_type: text (required, enum: ORIGIN, DESTINATION, WAYPOINT, PARKING_HUB)
  is_must_visit: boolean (required, default true)
  is_completed: boolean (required, default false)
  is_skipped: boolean (required, default false)
  skip_reason: text (nullable, e.g. TIME_WINDOW_EXCEEDED, BUDGET_EXCEEDED)
  window_start: timestamptz (nullable)
  window_end: timestamptz (nullable)
  dwell_seconds: integer (required, default 0)
  planned_arrival_at: timestamptz (nullable)
  planned_departure_at: timestamptz (nullable)
  wait_seconds: integer (required, default 0)
  created_at: timestamptz (required, default now())
  Constraint: unique(route_id, stop_order)

Table: route_legs
  id: uuid (PK, required)
  route_id: uuid (required, FK to routes.id on delete cascade, indexed)
  leg_order: integer (required, sequence index 0..N-1)
  from_stop_id: uuid (required, FK to route_stops.id)
  to_stop_id: uuid (required, FK to route_stops.id)
  transport_mode: text (required, enum: CAR, WALK, TRANSIT)
  departure_at: timestamptz (required)
  arrival_at: timestamptz (required)
  distance_m: integer (required)
  travel_seconds: integer (required)
  geometry: jsonb (required, GeoJSON LineString coordinates)
  parking_hub_id: uuid (nullable, FK to route_stops.id for walking transitions)
  traffic_basis: text (required, enum: LIVE, MODELED, UNKNOWN)
  provider: text (required, default 'valhalla')
  provider_version: text (nullable)
  turn_instructions: jsonb (required, default '[]'::jsonb)
  created_at: timestamptz (required, default now())
  Constraint: unique(route_id, leg_order)

Table: route_restrictions
  id: uuid (PK, required)
  route_id: uuid (required, FK to routes.id on delete cascade, indexed)
  restriction_type: text (required, enum: ROAD_CLOSURE, VEHICLE_ZONE, TIME_WINDOW, DELAY)
  geometry: geometry(Geometry, 4326) (required, spatial polygon or linestring)
  allowed_modes: jsonb (required, default '[]'::jsonb)
  effective_start: timestamptz (nullable)
  effective_end: timestamptz (nullable)
  claim_id: text (nullable, FK to claims.id from specification 0002)
  was_applied: boolean (required, default false)
  unresolved_reason: text (nullable)
  created_at: timestamptz (required, default now())
```

### State transitions

Route status represents the mathematical and operational feasibility of the itinerary:

```text
Status Lifecycle:
  [Input Request]
        |
        v
  [Solver Execution]
        |
        +---> All must visit and optional stops scheduled ---> FEASIBLE
        |
        +---> All must visit scheduled, some optional dropped ---> PARTIAL
        |
        +---> Must visit stops unreachable or window conflict ---> INFEASIBLE
        |
        +---> Solver crash, network failure, or timeout with no solution ---> UNAVAILABLE

Solver Status values:
  OPTIMAL: Global optimum found within constraints.
  FEASIBLE: Valid schedule found, search terminated or time limit reached.
  TIME_LIMIT: Best feasible solution returned when calculation deadline elapsed.
  INFEASIBLE: No combination of stops can satisfy hard constraints.
  ERROR: Internal solver or matrix extraction failure.
```

### API surface

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/api/v1/optimization/routes` | POST | `stops: list[RouteStopInput]`, `departure_at: datetime`, `objective: str` | `job_id: str`, `status: str` | Bearer (JWT) | 400 invalid stops, 401 unauthorized, 422 unresolvable locations |
| `/api/v1/jobs/{job_id}` | GET | `job_id: str` (path) | `job_id: str`, `status: str`, `route_id: uuid`, `error: str` | Bearer (JWT) | 401 unauthorized, 404 job not found |
| `/api/v1/optimization/routes/{route_id}` | GET | `route_id: uuid` (path) | `OptimizedRoute` (full itinerary, stops, legs) | Bearer (JWT) | 401 unauthorized, 403 forbidden, 404 route not found |
| `/api/v1/optimization/routes/{route_id}/replans` | POST | `current_location: Point`, `completed_stop_ids: list[uuid]` | `job_id: str`, `status: str` | Bearer (JWT) | 400 invalid state, 401 unauthorized, 404 route not found |

### Value sourcing

| Action | Value produced or displayed | Source |
|---|---|---|
| Submit route optimization | `job_id` | Orchestrator execution task generator (`uuid4`) |
| Submit route optimization | `owner_id` | Authenticated JWT claims principal subject |
| Resolve road matrix | Distance and duration matrices | Valhalla matrix service queried with stop coordinates |
| Solve schedule | Planned arrival and departure times | Google OR Tools time dimension accumulators |
| Solve schedule | Wait seconds at stop | Derived: `max(0, window_start - arrival_at)` |
| Resolve stop timezone | Stop local timezone offset | Derived from stop coordinates via spatial lookup or request departure offset |
| Detect pedestrian zone | Injected `PARKING_HUB` place and location | Places repository query matching spatial vicinity and `parking` tag |
| Fallback for missing parking | Designated drop off coordinate and warning | Nearest vehicular road node designated as curb drop off with warning `NO_PARKING_HUB_FOUND` |
| Enforce road closures | Penalized edge list | Verified claims table from specification 0002 matching route bounds |
| Calculate fuel cost | `fuel_amount_minor` | Derived: `total_distance_m` multiplied by vehicle consumption rate |
| Replan remaining route | Frozen stop arrival timestamps | Stored historical `planned_arrival_at` from prior route version |
| Replan remaining route | New origin location | `current_location` coordinate from replan request payload |
| Replan remaining route | Incremented `route_version` | Derived: `previous_route_version + 1` |

### Key invariants

- **Deterministic stop sequencing**: Given identical input stops, matrices, and random seeds, the solver produces identical stop orders and timestamps.
- **Strict must visit adherence**: A route can never be returned as `FEASIBLE` or `PARTIAL` if any must visit stop is omitted from the schedule.
- **Timezone spatial derivation**: Venue opening windows and operational time constraints are evaluated against local venue solar time, resolved from stop coordinates or request timestamp offset.
- **Parking fallback safety**: If no verified parking hub exists within `DEFAULT_PARKING_SEARCH_RADIUS_METERS` of a pedestrian destination, the solver uses the closest vehicular road node as a drop off point and appends `NO_PARKING_HUB_FOUND` to the route warnings list.
- **Non overlapping time allocation**: For every leg and stop, `departure_at >= arrival_at + dwell_seconds + wait_seconds`. Teleportation between stops is physically impossible.
- **Multimodal boundary separation**: Driving legs can never terminate directly inside pedestrian precincts; walking legs must originate from a valid vehicle drop off or parking hub.
- **Immutable version history**: Updating or replanning a route never mutates existing historical rows in place; a new `route_version` record is created, preserving audit trails.
- **Tenant isolation**: Every route read, replan, and query must explicitly filter by `owner_id` matching the authenticated caller token.

### Security model

- **Authentication**: All API endpoints require a valid Bearer token signed by the platform identity provider.
- **Authorization**: Row level ownership enforcement ensures users can only view, poll, or replan routes where `owner_id` matches their verified subject claim.
- **SSRF protection**: Coordinates submitted to Valhalla or local routing services must pass geographic boundary validation, rejecting loopback, multicast, or private IP ranges.
- **Rate limiting**: Asynchronous submission endpoints are rate limited to twenty requests per minute per user to prevent denial of service through solver saturation.

### Configuration required

- `VALHALLA_ENDPOINT_URL`: HTTP URL for the Valhalla routing service instance.
- `ORTOOLS_SOLVER_TIME_LIMIT_SECONDS`: Maximum calculation budget allocated to OR Tools before yielding best feasible result (default 10 seconds).
- `DEFAULT_PARKING_SEARCH_RADIUS_METERS`: Search radius for locating candidate parking hubs around pedestrian destinations (default 1000 meters).
- `ESTIMATED_FUEL_COST_PER_KM_MINOR`: Fuel rate in minor currency units per kilometer for driving cost estimates (default 15 cents).

### Critical test scenarios

- **Happy path**: Submit six destinations with distinct opening hours and dwell times; solver orders stops, satisfies all time windows, computes turn by turn legs, and persists route with status `FEASIBLE`, verifying **AC-1**, **AC-2**, **AC-6**, **AC-7**.
- **Pedestrian parking injection**: Submit a destination located in a restricted historic center; solver detects vehicle barrier, injects nearest verified parking hub, and creates driving to hub, walking to destination, and walking back to car legs, verifying **AC-4**.
- **Optional stop graceful drop**: Submit eight stops where travel time exceeds the daily return deadline; solver includes all must visit stops, omits two optional stops with `skip_reason="TIME_WINDOW_EXCEEDED"`, and marks route `PARTIAL`, verifying **AC-5**.
- **Dynamic closure avoidance**: Introduce an active verified road closure claim covering a primary highway pass; solver diverts route around closed pass, verifying **AC-3**.
- **Mid trip dynamic replan**: Traveler reports 60 minute delay at stop two; replan freezes stop one and two, sets current position as origin, recalculates remaining four stops, and persists `route_version=2`, verifying **AC-8**.
- **Worker fencing and async polling**: Client posts optimization request, receives 202 Accepted with `job_id`, polls until completed, and fetches route; worker manages lease fence and handles transient failure without stalling, verifying **AC-9**.
- **Auth and tenant isolation**: User B attempts to fetch or replan a route owned by User A; server rejects request with 403 Forbidden, verifying **AC-9**.

## Build plan

The build plan follows the Tracer Bullet approach, delivering an end to end vertical thread through domain contracts, PostgreSQL persistence, core constraint solving, matrix calculation, and worker orchestration.

1. **Slice 1: Routing domain contracts and mock fixtures**: Implement typed Pydantic v2 schemas (`RouteRequest`, `RouteStopInput`, `OptimizedRoute`, `ScheduledStopOutput`, `RouteLegOutput`, enums) in `backend/src/contracts/routing.py` with offline mock solvers and fixture validation, satisfies **AC-1**.
2. **Slice 2: Database migration and PostGIS repository**: Create Alembic migration `0012_add_route_optimization.py` defining `routes`, `route_stops`, `route_legs`, and `route_restrictions` with spatial indices, and implement `RouteRepository` for atomic persistence and tenant filtering, satisfies **AC-6**.
3. **Slice 3: Google OR Tools constraint solver service**: Implement `OrToolsRoutingSolver` wrapping OR Tools TSP and VRPTW models, enforcing time windows, dwell durations, and objective cost optimization (`FASTEST`, `SHORTEST`, `BALANCED`), satisfies **AC-2**, **AC-5**.
4. **Slice 4: Valhalla matrix adapter and parking hub injection**: Implement `ValhallaClient` for driving and walking distance and duration matrices, and implement `MultimodalTransitionService` to inject verified parking hubs for pedestrian destinations, satisfies **AC-4**, **AC-7**.
5. **Slice 5: Dynamic restriction ingestion and spatial edge masking**: Implement `RouteRestrictionService` to query verified claims from specification 0002, mapping active closures into Valhalla edge avoidance polygons and solver cost penalties, satisfies **AC-3**.
6. **Slice 6: Mid trip replanning engine and versioning**: Implement `RouteReplanService` to freeze completed stops, inject current traveler GPS as the new departure origin, and persist incremented route versions referencing prior versions, satisfies **AC-8**.
7. **Slice 7: Fenced worker service, SQS integration, and REST API**: Implement `OPTIMIZE.solve` worker handler under `WorkerFencingService`, wire SQS task queues, and expose asynchronous REST endpoints (`POST /api/v1/optimization/routes`, `GET /api/v1/jobs/{job_id}`, `GET /api/v1/optimization/routes/{route_id}`, `POST /api/v1/optimization/routes/{route_id}/replans`), satisfies **AC-9**.

## Consequences

**Positive**:
- Replaces hallucinated large language model itineraries with mathematically verified, schedule compliant travel routes.
- Fully supports multimodal transitions, automatically bridging vehicle transit with pedestrian zones via verified parking hubs.
- Gracefully degrades when itineraries are over constrained, dropping optional stops with clear explanations rather than failing silently.
- Enables mid trip replanning that respects historical trip progress while adapting dynamically to unexpected delays.
- Decouples computation from HTTP request lifecycles via durable worker lease fencing and asynchronous job polling.

**Negative / tradeoffs**:
- Requires hosting and maintaining a Valhalla routing engine instance with OpenStreetMap extract data.
- Native binary dependencies for Google OR Tools require careful packaging in containerized worker environments.
- High stop counts (more than twenty stops) increase solver calculation latency, requiring strict calculation time limits.

**Neutral**:
- Adds four new database tables and PostGIS spatial dependencies to the PostgreSQL schema.
- Introduces new asynchronous polling endpoints to the platform REST API.

## Follow-up

- [ ] Benchmark Valhalla matrix extraction latency under concurrent worker load to determine memory sizing for routing containers.
- [ ] Investigate predictive traffic historical speed profiles in Valhalla to improve travel time accuracy during rush hours.
