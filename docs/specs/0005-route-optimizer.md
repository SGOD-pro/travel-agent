# 0005. Route Optimizer and Multimodal Itinerary Planning

**Date**: 2026-10-23
**Status**: Proposed

## Summary

This specification defines the route optimization and multimodal itinerary planning engine for SWENA. The engine accepts multi stop travel requests with arrival deadlines, opening hours, dwell times, and transit modes, producing temporally valid itineraries that minimize travel friction. A hybrid solver combines Google OR Tools constraint programming with an external Valhalla routing service, enforcing strict priorities across must visit and optional stops while preserving independent serverless Lambda ZIP packaging beneath the 250 MB ceiling.

The system handles real world travel constraints including pedestrian zones, road closures, and mid trip delays. When a destination lies within a pedestrian precinct, the planner injects verified parking facilities from canonical places and enforces vehicle continuity so travelers return to their parked vehicle before driving onward. Verified spatial claims inform hard road closures using Valhalla polygon exclusions and post routing geometry intersection checks. Mid trip replanning preserves historical completed stops, takes the traveler current live location as the new departure origin, and publishes an immutable incremented route version. All solving runs asynchronously under worker lease fencing, writing route geometries, schedules, and fuel costs to PostgreSQL and PostGIS.

## Context

The SWENA travel intelligence platform orchestrates complex travel research across durable workflow execution (specification 0001), source evidence collection and claim verification (specification 0002), dynamic browser automation (specification 0003), and category decision routing (specification 0004). While these systems gather authoritative opening hours, place geometries, and local constraints, the platform currently lacks the ability to synthesize this verified knowledge into an executable, temporally feasible multi stop route.

Travelers routinely face complex routing dilemmas that simple point to point mapping engines cannot solve. A day itinerary often includes six to ten distinct stops across different neighborhoods, each stop having fixed opening hours, mandatory dwell durations, and varying transport accessibility. Some historic urban centers prohibit private cars entirely, requiring transitions between personal vehicles and walking. In mountainous or coastal areas, seasonal passes close on scheduled dates, and unexpected roadworks or weather hazards invalidate static shortest path calculations.

Allowing general large language models to construct travel itineraries causes hallucinated travel times, impossible physical sequencing, and ignored opening schedules. Conversely, simple travelling salesperson heuristics fail because they cannot account for time windows, vehicle access boundaries, multimodal parking transitions, or dynamic road closures. In the existing prototype, `ItineraryScheduler` computes simplistic haversine distances at an assumed 50 km/h speed without opening hours, stop priorities, or realistic road networks.

Architectural principle AD-03 mandates that model outputs cannot become deterministic authority, and that computation must respect serverless Lambda ZIP packaging limits without requiring containerized ECS or ECR infrastructure. Heavy matrix calculations and road topology data must therefore be decoupled: Valhalla runs as an external configurable HTTP service with an explicit local Docker development path, while Google OR Tools constraint programming runs inside an independent serverless worker beneath the AWS Lambda 250 MB uncompressed ceiling. All route entities, foreign keys, and money calculations must integrate strictly with existing PostgreSQL tables, UUID types, and canonical money contracts.

## Requirements

**User stories**:
- As a traveler, I want my multi stop itinerary organized in the most efficient sequence so that I spend minimal time transit and never arrive when a venue is closed.
- As a road trip driver, I want the planner to route me to verified parking hubs before pedestrian destinations and guide me back to my vehicle before driving to the next stop.
- As an active traveler experiencing unexpected delays, I want to replan my remaining day from my current live location without losing credit for stops I already completed.
- As a travel orchestrator, I want mathematical proof of route feasibility and explicit explanations when optional stops cannot fit within time budgets.

**Acceptance criteria**:
- **AC-1**: SWENA defines internal typed domain contracts (`RouteRequest`, `RouteStopInput`, `OptimizedRoute`, `ScheduledStopOutput`, `RouteLegOutput`, `MatrixRequest`, `MatrixResponse`, `RouteObjective`, `RouteStatus`, `SolverStatus`, `StopType`, `TrafficBasis`) in `contracts/routing.py` that encapsulate multi stop itinerary inputs, provider matrices, and scheduled outputs.
- **AC-2**: The route optimizer executes a hybrid constraint solver combining Google OR Tools Operations Research solver with asymmetric duration and distance matrices, enforcing hard time windows, dwell durations, wait time accounting, optional stop drop penalties, and user chosen objectives (`FASTEST`, `SHORTEST`, `BALANCED`).
- **AC-3**: The optimization engine ingests verified spatial claims from specification 0002, applying strict road prohibition via Valhalla `exclude_polygons` and revalidating final leg geometries against active restriction intervals, treating unsupported restrictions as explicit and failing closed safely.
- **AC-4**: When a destination is flagged as pedestrian only or vehicle restricted, the solver automatically identifies a verified parking hub from `canonical_places`, injecting a `PARKING_HUB` stop and maintaining strict vehicle continuity where driving legs only depart from the stop where the vehicle is currently parked, never inventing parking permissions from raw road nodes.
- **AC-5**: The solver enforces strict priority partitioning: must visit stops are hard constraints that must be fulfilled, while optional stops have penalty costs allowing graceful omission with explicit `skip_reason` codes; if must visit stops are temporally unreachable, the solver returns status `INFEASIBLE` with actionable repair diagnostics.
- **AC-6**: Route records, scheduled stops, legs with GeoJSON LineString geometries, and applied restrictions persist in PostgreSQL tables (`routes`, `route_stops`, `route_legs`, `route_restrictions`) with UUID foreign keys to `canonical_places.id`, `claims.id`, `executions.id`, and `trips.id`, storing fuel costs as `Decimal` values with `Numeric(20, 6)` precision.
- **AC-7**: The routing adapter connects to an external Valhalla HTTP service for road and pedestrian matrices and turn by turn path geometries, specifying explicit coordinate order, distance in meters, duration in seconds, snapping behavior, socket timeouts, bounded retries, and offline mock fixtures for testing without cloud deployment.
- **AC-8**: Mid trip replanning accepts current traveler coordinates, freezes completed stops with historical arrival timestamps, treats the current position as the new departure origin, optimizes remaining stops, and writes an immutable incremented `route_version` referencing `previous_route_version` under optimistic concurrency controls.
- **AC-9**: Route optimization tasks execute asynchronously via an `optimizer` worker with action `solve` claimed through `WorkerFencingService` on SQS, respecting parent execution lease fences, deadlines, cooperative cancellation, and atomic route completion while reusing the existing `/api/v1/jobs/{job_id}` polling and execution identity.

## Options considered

### Option 1: Hybrid Google OR Tools solver with external Valhalla routing service (Chosen)

Run Google OR Tools constraint solver inside an independent serverless Lambda worker, querying an external Valhalla routing service over HTTP for road and walking matrices. Valhalla computes realistic driving and pedestrian distance and duration matrices using OpenStreetMap road topologies, while OR Tools executes constraint programming across time windows, stop priorities, dwell durations, and multimodal transitions.

**Pros**:
- Preserves independent serverless Lambda ZIP packaging beneath the 250 MB ceiling by keeping heavy OpenStreetMap map data and C++ tile routing graphs outside the Lambda deployment package.
- Handles complex vehicle routing problem with time windows (VRPTW) constraints natively with high mathematical efficiency.
- Supports strict topological road exclusion via Valhalla `exclude_polygons` for verified road closures.
- Valhalla provides unified multimodal routing (driving, walking) with turn by turn geometries and matrix generation.
- Clear offline development path via local Docker Valhalla container and canned offline fixture clients.

**Cons**:
- Requires operating an external Valhalla HTTP routing service for production deployment.
- Network latency over HTTP matrix requests adds between 50 and 200 milliseconds to worker execution time.

### Option 2: Pure heuristic solver in Python (Nearest Neighbor with 2-Opt)

Implement an in process Python heuristic solver using greedy nearest neighbor construction followed by 2-Opt local search refinement, calculating point distances using haversine approximations or basic routing APIs.

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
- Black box optimization logic prevents custom parking hub injection, vehicle continuity enforcement, or dynamic claim restriction weighting.
- External dependency introduces vendor lock in and network latency vulnerabilities for core itinerary planning.

## Decision

**Chosen option**: Option 1: Hybrid Google OR Tools solver with external Valhalla routing service

We will integrate Google OR Tools as the primary constraint programming solver within an independent serverless Lambda worker, and query Valhalla as an external configurable HTTP service for multimodal matrices and path geometries, executing asynchronously within dedicated worker tasks under PostgreSQL lease fencing.

## Rationale

Travel itinerary optimization is fundamentally a vehicle routing problem with time windows, stop priorities, and multimodal transitions. Heuristic methods (Option 2) cannot guarantee arrival within narrow opening hours or satisfy hard departure deadlines. Commercial cloud APIs (Option 3) introduce steep recurring costs, lack support for custom spatial claim restrictions, and cannot inject localized parking hubs dynamically.

Google OR Tools provides battle tested, highly optimized constraint programming primitives that solve multi stop scheduling with time windows in seconds. Packaging OR Tools wheels inside the Lambda worker consumes approximately 70 MB, which fits comfortably within the 250 MB uncompressed Lambda limit. Keeping Valhalla as an external HTTP service eliminates gigabytes of OpenStreetMap graph data from the application package, allowing the worker to remain serverless and packaging clean. Pairing OR Tools with Valhalla provides realistic road and pedestrian network awareness, accurate asymmetric travel times, and strict road closure exclusions.

## Feature design

### Serverless Lambda packaging and external Valhalla setup

The optimizer worker packages as a standalone Python 3.12 AWS Lambda function using standard ZIP packaging (via AWS SAM), adhering to the 250 MB uncompressed ceiling:

- **Packaging boundary**: The Lambda artifact contains application code, Pydantic v2 schemas, SQLAlchemy models, and the `ortools` Python wheel. It strictly excludes Valhalla binaries, OpenStreetMap planet files, and heavy GIS compilers.
- **External service configuration**: Valhalla is accessed via `VALHALLA_ENDPOINT_URL`. The application treats Valhalla as an external HTTP dependency identical to SerpAPI or remote LAYA.
- **Local development setup**: Developers run Valhalla locally via Docker:
  ```bash
  docker run -d --name valhalla -p 8002:8002 \
    -v $(pwd)/valhalla_tiles:/custom_tiles \
    -e custom_urls=https://download.geofabrik.de/asia/india/west-bengal-latest.osm.pbf \
    ghcr.io/gis-ops/docker-valhalla/valhalla:latest
  ```
- **Connected acceptance gate**: Integration suites run against live Valhalla only when `VALHALLA_ENDPOINT_URL` is set and its `/status` endpoint returns HTTP 200. When unconfigured or disabled, connected tests skip cleanly, and offline unit tests run against canned fixture matrices.

### Data model sketch

The route optimization engine persists data in four core tables in PostgreSQL with PostGIS extensions, reconciling all foreign keys with existing tables:

```text
Table: routes
  id: uuid (PK, required)
  owner_id: uuid (required, indexed, matches executions.owner_id and trips.owner_id)
  execution_id: uuid (nullable, indexed, FK to executions.id on delete set null)
  trip_id: uuid (nullable, indexed, FK to trips.id on delete set null)
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
  fuel_amount: numeric(20, 6) (nullable, estimated fuel expense matching budget_lines.amount)
  fuel_currency: text (required, default 'INR', matches contracts.money.Currency)
  fuel_basis: text (nullable, explanation of consumption rate and vehicle profile)
  warnings: jsonb (required, default '[]'::jsonb)
  assumptions: jsonb (required, default '[]'::jsonb)
  previous_route_version: integer (nullable, tracks replan history)
  created_at: timestamptz (required, default now())
  updated_at: timestamptz (required, default now())
  Constraint: unique(trip_id, route_version) when trip_id is not null

Table: route_stops
  id: uuid (PK, required)
  route_id: uuid (required, FK to routes.id on delete cascade, indexed)
  place_id: uuid (nullable, indexed, FK to canonical_places.id on delete set null)
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
  transport_mode: text (required, enum: CAR_PETROL, MOTORCYCLE_PETROL, WALK)
  departure_at: timestamptz (required)
  arrival_at: timestamptz (required)
  distance_m: integer (required)
  travel_seconds: integer (required)
  geometry: jsonb (required, GeoJSON LineString coordinates [[lon, lat], ...])
  parking_hub_id: uuid (nullable, FK to route_stops.id for walking transitions)
  traffic_basis: text (required, enum: LIVE, MODELED, UNKNOWN)
  provider: text (required, default 'valhalla')
  provider_version: text (nullable)
  data_version: text (nullable)
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
  claim_id: uuid (nullable, FK to claims.id on delete set null)
  was_applied: boolean (required, default false)
  unresolved_reason: text (nullable)
  created_at: timestamptz (required, default now())
```

### Typed matrix and path provider contracts

The routing adapter interfaces with Valhalla using explicit typed contracts:

- **Coordinate convention**: Domain models use `Point(latitude: float, longitude: float)` with validated latitude in `[-90, 90]` and longitude in `[-180, 180]`. In Valhalla HTTP JSON payloads, points serialize as `{"lat": latitude, "lon": longitude}`. In GeoJSON geometries, coordinates format as `[longitude, latitude]` per RFC 7946.
- **Physical units**: Distance is strictly integer meters (`distance_m`); duration is strictly integer seconds (`duration_s`).
- **Matrix asymmetry**: Matrix elements $M[i][j]$ are directional. Travel time and distance from stop A to stop B are calculated independently from stop B to stop A to reflect one way streets, prohibited turns, and elevation gradients.
- **Unreachable pair handling**: If Valhalla returns `null` distance or duration for pair $(i, j)$ due to disconnected road networks or bodies of water, the matrix element is marked `reachable=False`. The solver assigns an unreachable penalty cost ($\infty$ or drop penalty) and records an island warning.
- **Road snapping**: Coordinates snap to the nearest drivable or walkable road edge within a configurable radius (`snapping_radius_meters`, default 250m). If snapping fails, the adapter raises an explicit `UnroutableLocationError`.
- **HTTP resilience and timeouts**: Socket connect timeout is 2.0 seconds; read timeout is 5.0 seconds for matrices and 10.0 seconds for turn by turn route paths. The client executes at most 2 retries on 5xx server errors or transient connection resets with exponential backoff, and fails immediately with zero retries on 4xx client errors. Matrix requests enforce a batch ceiling of 50 locations per call.
- **Unavailable behavior**: If the Valhalla service is unreachable or unconfigured, the adapter fails closed, returning HTTP 503 or raising `RoutingProviderUnavailableError`. Offline mock fixtures (`ValhallaFixtureClient`) provide canned matrices for unit testing.

### Exact solver semantics

Google OR Tools constraint programming models multi stop scheduling as a Routing Problem with Time Windows (VRPTW):

- **Open versus return routes**:
  - Return route (round trip circuit): start node = 0, end node = 0. The time dimension enforces that arrival at end node does not exceed `return_deadline`.
  - Open route (one way or road trip with fixed terminus): start node = 0, end node is either fixed to the designated terminal hotel stop or open across all stops.
- **Time dimension accounting**:
  - The solver operates on an integer seconds scale starting from `departure_at` ($t_0 = 0$).
  - Service windows: venue opening and closing hours $[W_{start}, W_{end}]$ are converted to seconds relative to $t_0$.
  - IANA Timezones and DST: stop opening hours are defined in local venue time. The engine derives the IANA timezone (`Asia/Kolkata`, `Europe/Paris`, etc.) from the stop coordinates using spatial lookup or request attributes. Local opening hours are converted to UTC seconds relative to $t_0$, correctly accounting for Daylight Saving Time (DST) shifts on the scheduled visit date.
  - Dwell and wait accounting: each stop has a mandatory stay duration (`dwell_seconds`). If arrival occurs before $W_{start}$, the traveler waits: $wait\_seconds = W_{start} - arrival\_at$. The cumulative departure time satisfies $departure_i = arrival_i + wait_i + dwell_i$.
- **Priority partitioning and drop penalties**:
  - Must visit stops (`is_must_visit=True`): hard constraints without disjunction. The solver must include all must visit stops.
  - Optional stops (`is_must_visit=False`): added with `AddDisjunction([node_index], drop_penalty)`. The penalty cost is calibrated so the solver prefers inclusion whenever time permits, but drops optional stops when time budgets or opening windows are exceeded, recording an explicit `skip_reason`.
- **Objective functions and weights**:
  - `FASTEST`: arc cost = travel duration in seconds.
  - `SHORTEST`: arc cost = physical road distance in meters.
  - `BALANCED`: arc cost = $w_{time} \times duration\_s + w_{dist} \times distance\_m + w_{fuel} \times estimated\_fuel\_minor$.
- **Honest solver statuses**:
  - `OPTIMAL`: proven mathematical optimum found within constraints.
  - `FEASIBLE`: valid schedule found satisfying all hard constraints.
  - `TIME_LIMIT`: calculation time limit reached; best feasible schedule returned with warning `TIME_LIMIT_REACHED`. If no feasible solution was found before the deadline, returns `INFEASIBLE` with error `TIMEOUT_NO_FEASIBLE_SOLUTION`.
  - `INFEASIBLE`: hard constraints cannot be satisfied (e.g. travel time between must visit stops exceeds available day budget). Returns detailed diagnostics listing conflicting stops and windows.
  - `ERROR`: internal solver failure or invalid input parameters.

### Hard closure avoidance and restriction revalidation

Verified road closures and hazard claims from specification 0002 are enforced strictly:

- **Prohibition over penalties**: Road closures represent physical barriers. Cost penalties must never substitute for prohibition, because large penalties can still allow closed roads if the detour is long. Closures must be strictly excluded.
- **Valhalla exclusion interface**: The routing adapter converts verified closure polygon geometries into Valhalla `exclude_polygons` parameters. Valhalla completely forbids traversing edges intersecting excluded polygons during graph traversal.
- **Post routing geometry revalidation**: After Valhalla returns the GeoJSON LineString geometry for each route leg, the service performs an explicit spatial intersection check against all active `route_restrictions` geometries.
- **Temporal applicability**: Geometry intersections are checked only if the planned traversal time interval $[departure_i, arrival_i]$ overlaps the restriction active period $[effective\_start, effective\_end]$. If an overlap occurs with an active closure, the candidate route is rejected.
- **Unsupported restrictions**: If a restriction cannot be represented as a spatial polygon (e.g. complex vehicle weight or height limits unsupported by current map tiles), it is recorded with `was_applied=False` and an explicit `unresolved_reason`. The engine fails closed safely, marking the route `INFEASIBLE` with diagnostic details rather than generating an unverified route.

### Vehicle continuity and parking transitions

When traveling by private vehicle (`CAR_PETROL`, `MOTORCYCLE_PETROL`) to destinations in pedestrian only or vehicle restricted precincts:

- **Vehicle location tracking**: The route state tracks `vehicle_parked_at_stop_id`. A driving leg is valid only if its departure origin is the exact location where the vehicle is currently parked.
- **Parking hub injection**: The service searches `canonical_places` for verified parking places (`category="parking"`) within walking threshold (`DEFAULT_PARKING_SEARCH_RADIUS_METERS`, default 1000m). It injects a `PARKING_HUB` stop and splits the traversal into:
  1. Drive from previous stop to parking hub;
  2. Walk from parking hub to pedestrian destination;
  3. Walk back to the parking hub to retrieve the vehicle;
  4. Drive from parking hub to subsequent destination.
- **Walk chaining**: If multiple pedestrian destinations lie within the same walking cluster, the traveler walks between them without returning to the vehicle between stops, returning to the vehicle only when departing the pedestrian precinct.
- **No invented parking**: The system strictly forbids inventing parking permissions by snapping vehicles to random road nodes. If no verified parking place exists in `canonical_places` within the threshold, the planner marks the stop `INFEASIBLE` with diagnostic `NO_VERIFIED_PARKING_HUB`, allowing the traveler to select an alternate transport mode or relax constraints.

### Fuel and money calculations

Fuel expenses reuse the canonical domain models from `contracts/routing.py` and `contracts/money.py`:

- **Driving only fuel calculation**: Fuel consumption is calculated strictly on driving distance (`driving_distance_m`), explicitly excluding walking and public transit legs.
- **Fuel profile math**: Reuses `VehicleFuelProfile` and `RoadBudgetCalculator`:
  $$\text{fuel\_liters} = \left(\frac{\text{driving\_distance\_km}}{\text{mileage\_km\_per\_liter}}\right)\text{.quantize}(\text{Decimal}("0.000001"))$$
  $$\text{fuel\_cost} = (\text{fuel\_liters} \times \text{fuel\_price\_per\_liter})\text{.quantize}(\text{Decimal}("0.000001"))$$
- **Precision and storage**: `fuel_amount` stores as `Decimal` in PostgreSQL with `Numeric(20, 6)` precision. Currency defaults to `Currency.INR`.
- **Tolls and fees preservation**: Toll charges along route corridors are represented as `BudgetLine` items. If toll rates for a road corridor are unverified, the amount remains `None` with an explicit `unknown_reason`, preserving the zero hallucination invariant from `contracts/money.py`.

### Worker execution, fencing, and job API integration

Route optimization runs asynchronously within the orchestrator execution engine:

- **Worker identity**: The worker registers as `worker="optimizer"` and handles `action="solve"`.
- **Queue transport**: SQS queue transport routes optimizer tasks to a dedicated task queue without reverse imports.
- **Parent first lease fencing**: Before starting solver computations, the worker invokes `WorkerFencingService.claim_task(...)`, verifying that the parent execution lease is active and not expired before granting the task lease token.
- **Deadlines and cooperative cancellation**: The worker checks `execution.cancelled_at` and `execution.deadline_at`. If the execution was cancelled or the deadline elapsed, execution halts immediately without writing partial results.
- **Atomic route and task completion**: Within a single database transaction, the worker writes rows to `routes`, `route_stops`, `route_legs`, and `route_restrictions`, records `worker_results`, transitions `tasks.status = 'COMPLETED'`, and enqueues a completion outbox event for orchestrator reconciliation.
- **Idempotency**: Tasks enforce `idempotency_key` backed by the `uq_tasks_execution_idempotency` database constraint. Duplicate task dispatches return the existing result.
- **Owner isolation**: All queries, job polling, and route retrievals enforce `owner_id: UUID` matching the authenticated JWT user.
- **Concurrent immutable replanning**: Replanning creates a new `route_version = previous_route_version + 1`. The database unique constraint `unique(trip_id, route_version)` guarantees that concurrent replans fail safely, prompting a fresh read.
- **Reuse of existing job API**: Clients poll `GET /api/v1/jobs/{job_id}` where `job_id` matches `execution_id`. The response reflects task progress and timeline events. Once completed, clients retrieve the itinerary via `GET /api/v1/optimization/routes/{route_id}`.

### Compatibility with ItineraryScheduler

To ensure existing orchestrator graphs (specifically `backend/src/functions/orchestrator/graphs/trip_planner/graph.py`) continue executing without regression:

- `ItineraryScheduler` in `backend/src/functions/optimizer/solver.py` will serve as a backward compatible facade.
- `ItineraryScheduler.solve_schedule(...)` will delegate internally to `OrToolsRoutingSolver`, translating input stop dictionaries into `RouteStopInput` schemas and mapping solver outputs back into `OptimizedSchedule` with `stops: list[ScheduledStop]` and `legs: list[ScheduledLeg]`.
- Existing unit and integration tests covering `graph.py` and `compute_budget_node` remain fully functional while benefiting from improved constraint satisfaction.

### API surface

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/api/v1/optimization/routes` | POST | `stops: list[RouteStopInput]`, `departure_at: datetime`, `objective: str` | `job_id: uuid`, `status: str` | Bearer (JWT) | 400 invalid stops, 401 unauthorized, 422 unresolvable locations |
| `/api/v1/jobs/{job_id}` | GET | `job_id: uuid` (path) | `JobStatusResponse` (status, progress, timeline) | Bearer (JWT) | 401 unauthorized, 404 job not found |
| `/api/v1/optimization/routes/{route_id}` | GET | `route_id: uuid` (path) | `OptimizedRoute` (full itinerary, stops, legs) | Bearer (JWT) | 401 unauthorized, 403 forbidden, 404 route not found |
| `/api/v1/optimization/routes/{route_id}/replans` | POST | `current_location: Point`, `completed_stop_ids: list[uuid]` | `job_id: uuid`, `status: str` | Bearer (JWT) | 400 invalid state, 401 unauthorized, 404 route not found |

### Value sourcing

| Action | Value produced or displayed | Source |
|---|---|---|
| Submit route optimization | `job_id` | Orchestrator execution generator (`uuid.uuid4()`) |
| Submit route optimization | `owner_id` | Authenticated JWT claims subject (`AuthenticatedUser.id`) |
| Query road matrix | Asymmetric distance and duration matrices | External Valhalla `/sources_to_targets` endpoint |
| Solve schedule | Planned arrival and departure times | Google OR Tools time dimension accumulators added to departure timestamp |
| Solve schedule | Wait seconds at stop | Derived: `max(0, window_start - arrival_at)` |
| Resolve venue opening | Local venue opening windows | Derived from stop coordinates via spatial IANA timezone lookup |
| Detect pedestrian zone | Injected `PARKING_HUB` place and location | `canonical_places` repository query matching vicinity and `category="parking"` |
| Fallback for missing parking | Infeasible route status and diagnostic | Explicit failure code `NO_VERIFIED_PARKING_HUB` (never invented road nodes) |
| Enforce road closures | `exclude_polygons` parameter | Verified claims table from specification 0002 matching route bounds |
| Calculate fuel cost | `fuel_amount` | Derived via `RoadBudgetCalculator` from `driving_distance_m` and vehicle profile |
| Replan remaining route | Frozen stop arrival timestamps | Stored historical `planned_arrival_at` from prior route version |
| Replan remaining route | New origin location | `current_location` coordinate from replan request payload |
| Replan remaining route | Incremented `route_version` | Derived: `previous_route_version + 1` |

### Key invariants

- **Deterministic stop sequencing**: Given identical input stops, matrices, and random seeds, the solver produces identical stop sequences and timestamps.
- **Strict must visit adherence**: A route can never be returned as `FEASIBLE` or `PARTIAL` if any must visit stop is omitted from the schedule.
- **Vehicle continuity**: A driving leg can depart only from the stop where the vehicle is currently parked. Walking legs do not move vehicles.
- **No invented parking**: Parking hubs must exist in `canonical_places`. Raw road nodes cannot be designated as parking facilities without verified place records.
- **Prohibition over penalties**: Active road closures must be strictly avoided via topological exclusions, never bypassed via high cost penalties.
- **Driving only fuel accounting**: Fuel costs apply only to driving distance; walking and public transit distances consume zero fuel.
- **Immutable version history**: Updating or replanning a route creates a new `route_version` record referencing `previous_route_version`, preserving complete audit history.
- **Owner isolation**: Every route query, replan, and job status check must enforce `owner_id` matching the authenticated caller token.

### Security model

- **Authentication**: All API endpoints require a valid Bearer token signed by the platform identity provider.
- **Authorization**: Row level ownership enforcement ensures users can only view, poll, or replan routes where `owner_id` matches their verified subject claim.
- **SSRF protection**: Coordinates submitted to Valhalla or local routing services must pass geographic boundary validation, rejecting loopback, multicast, or private IP ranges.
- **Rate limiting**: Asynchronous submission endpoints are rate limited to twenty requests per minute per user to prevent denial of service through solver saturation.

### Configuration required

- `VALHALLA_ENDPOINT_URL`: HTTP URL for the external Valhalla routing service instance.
- `ORTOOLS_SOLVER_TIME_LIMIT_SECONDS`: Maximum calculation budget allocated to OR Tools before yielding best feasible result (default 10 seconds).
- `DEFAULT_PARKING_SEARCH_RADIUS_METERS`: Search radius for locating candidate parking hubs around pedestrian destinations (default 1000 meters).
- `VALHALLA_CONNECT_TIMEOUT_SECONDS`: Connection timeout for Valhalla HTTP requests (default 2.0 seconds).
- `VALHALLA_READ_TIMEOUT_SECONDS`: Read timeout for Valhalla matrix and route requests (default 5.0 seconds).

### Critical test scenarios

- **Happy path**: Submit six destinations with distinct opening hours and dwell times; solver orders stops, satisfies all time windows, computes turn by turn legs, and persists route with status `FEASIBLE`, verifying **AC-1**, **AC-2**, **AC-6**, **AC-7**.
- **Pedestrian parking and vehicle continuity**: Submit a destination located in a restricted historic center; solver detects vehicle barrier, injects nearest verified parking hub from `canonical_places`, creates drive to hub, walk to destination, and walk back to car legs, verifying **AC-4**.
- **Optional stop graceful drop**: Submit eight stops where travel time exceeds the daily return deadline; solver includes all must visit stops, omits two optional stops with `skip_reason="TIME_WINDOW_EXCEEDED"`, and marks route `PARTIAL`, verifying **AC-5**.
- **Hard road closure avoidance**: Introduce an active verified road closure claim covering a primary highway pass; solver strictly avoids the road via `exclude_polygons` and passes post routing geometry revalidation, verifying **AC-3**.
- **Missing parking hub safe failure**: Destination in pedestrian zone has zero verified parking places in `canonical_places` within 1000 meters; planner fails closed with `status="INFEASIBLE"` and diagnostic `NO_VERIFIED_PARKING_HUB` without inventing street parking, verifying **AC-4**, **AC-5**.
- **Mid trip dynamic replan**: Traveler reports delay at stop two; replan freezes stops one and two with historical timestamps, sets current live position as new origin, recalculates remaining four stops, and persists `route_version=2`, verifying **AC-8**.
- **Worker lease fencing and async polling**: Client posts optimization request, receives 202 Accepted with `job_id`, polls until completed, and fetches route; worker manages lease fence and atomic completion without stalling, verifying **AC-9**.
- **Auth and tenant isolation**: User B attempts to fetch or replan a route owned by User A; server rejects request with 403 Forbidden, verifying **AC-9**.

## Build plan

The build plan follows the Tracer Bullet approach, delivering an initial small offline vertical slice with fixed matrices and hard windows, followed by database persistence, multimodal continuity, external Valhalla integration, replanning, and serverless packaging acceptance.

1. **Slice 1: Offline core solver vertical slice and routing contracts**: Reconcile typed Pydantic v2 schemas (`RouteRequest`, `RouteStopInput`, `OptimizedRoute`, `ScheduledStopOutput`, `RouteLegOutput`, enums) in `backend/src/contracts/routing.py`, create a fixed 5x5 asymmetric matrix fixture with hard time windows and an unreachable pair, implement `OrToolsRoutingSolver` with time windows, dwell/wait accounting, drop penalties, and honest statuses (`OPTIMAL`, `FEASIBLE`, `TIME_LIMIT`, `INFEASIBLE`), and provide a backward compatible facade for `ItineraryScheduler`, satisfies **AC-1**, **AC-2**, **AC-5**.
2. **Slice 2: Database migration and PostGIS repository**: Create Alembic migration `0012_add_route_optimization.py` defining `routes`, `route_stops`, `route_legs`, and `route_restrictions` with UUID foreign keys to `canonical_places.id`, `claims.id`, `executions.id`, and `trips.id`, `Numeric(20, 6)` for fuel amounts, and PostGIS spatial indexes, and implement `RouteRepository` for atomic transaction persistence, versioning, and tenant isolation, satisfies **AC-6**.
3. **Slice 3: Multimodal vehicle continuity and parking hub service**: Implement `VehicleContinuityTracker` and `MultimodalTransitionService` searching `canonical_places` for verified parking places, injecting parking hubs, splitting driving and walking legs, ensuring travelers return to their parked vehicle before driving, and failing closed if no verified parking exists, satisfies **AC-4**.
4. **Slice 4: External Valhalla HTTP client and hard closure avoidance**: Implement `ValhallaClient` connecting to external Valhalla `/sources_to_targets` and `/route` endpoints with coordinate validation, asymmetric matrix parsing, snapping, timeouts, and bounded retries, implement hard closure avoidance via `exclude_polygons` and post routing geometry revalidation, and define an offline mock fixture and connected smoke acceptance gate, satisfies **AC-3**, **AC-7**.
5. **Slice 5: Mid trip dynamic replanning engine**: Implement `RouteReplanService` to freeze completed stops with historical arrival timestamps, inject current live GPS coordinates as the new departure origin, recalculate remaining stops, increment `route_version`, and enforce unique constraints against concurrent replans, satisfies **AC-8**.
6. **Slice 6: Worker fencing, execution integration, and REST API**: Implement `optimizer` worker handling `action="solve"` claimed under `WorkerFencingService` with parent execution lease checks, cancellation barriers, and atomic completion transactions, wire SQS task queues, and expose asynchronous REST endpoints (`POST /api/v1/optimization/routes`, `GET /api/v1/jobs/{job_id}`, `GET /api/v1/optimization/routes/{route_id}`, `POST /api/v1/optimization/routes/{route_id}/replans`), satisfies **AC-9**.
7. **Slice 7: Standalone Lambda ZIP packaging and connected provider acceptance**: Verify that the optimizer worker packages as a standalone Python ZIP beneath the AWS Lambda 250 MB uncompressed limit without bundling Valhalla or requiring ECS/ECR, and execute the connected provider acceptance test suite against a live Valhalla instance, satisfies **AC-7**, **AC-9**.

## Consequences

**Positive**:
- Replaces hallucinated large language model itineraries with mathematically verified, schedule compliant travel routes.
- Preserves independent serverless Lambda ZIP packaging beneath the 250 MB ceiling by treating Valhalla as an external HTTP dependency.
- Guarantees vehicle continuity by tracking parked vehicle locations and preventing impossible teleportation or invented street parking.
- Strictly prohibits traversing closed roads via topological polygon exclusions rather than weak cost penalties.
- Reconciles completely with existing database schema, UUID types, canonical places, claims, execution fencing, and money rules.
- Retains backward compatibility with `ItineraryScheduler` so existing orchestrator graphs continue working seamlessly.

**Negative / tradeoffs**:
- Requires operating an external Valhalla routing service instance for live route matrix generation.
- High stop counts (more than twenty stops) increase solver calculation latency, requiring strict calculation time limits.

**Neutral**:
- Adds four new database tables and PostGIS spatial dependencies to the PostgreSQL schema.
- Introduces new asynchronous polling endpoints to the platform REST API.

## Follow-up

- [ ] Benchmark Valhalla matrix extraction latency under concurrent worker load to determine memory sizing for routing containers.
- [ ] Investigate predictive traffic historical speed profiles in Valhalla to improve travel time accuracy during rush hours.
