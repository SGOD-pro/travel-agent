# Verify: Route Optimizer and Multimodal Itinerary Planning, spec 0005, updated 2026-10-23
_Steps derived from spec 0005 acceptance criteria. /check verify runs these; /test locks the durable ones._

## Acceptance criteria (Slice 1)

- [x] AC-1: SWENA defines internal typed domain contracts (`RouteRequest`, `RouteStopInput`, `OptimizedRoute`, `ScheduledStopOutput`, `RouteLegOutput`, `MatrixRequest`, `MatrixResponse`, `RouteObjective`, `RouteStatus`, `SolverStatus`, `StopType`, `TrafficBasis`) in `contracts/routing.py` that encapsulate multi stop itinerary inputs, provider matrices, and scheduled outputs while preserving existing fuel profile, road segment, and budget calculator types. (Slice 1 complete)
- [x] AC-2: The route optimizer executes a hybrid constraint solver combining Google OR-Tools constraint programming with asymmetric duration and distance matrices, enforcing hard time windows, dwell durations, wait time accounting, optional stop drop penalties, and user chosen objectives (`FASTEST`, `SHORTEST`, `BALANCED`). (Slice 1 complete)
- [ ] AC-3: The optimization engine ingests verified spatial claims from specification 0002, applying strict road prohibition via Valhalla `exclude_polygons` and revalidating final leg geometries against active restriction intervals, treating unsupported restrictions as explicit and failing closed safely. (Slice 4)
- [x] AC-4: When a destination is flagged as pedestrian only or vehicle restricted, the solver automatically identifies a verified parking hub from `canonical_places`, injecting a `PARKING_HUB` stop and maintaining strict vehicle continuity where driving legs only depart from the stop where the vehicle is currently parked, never inventing parking permissions from raw road nodes. (Slice 3 complete)
- [x] AC-5: The solver enforces strict priority partitioning: must visit stops are hard constraints that must be fulfilled, while optional stops have penalty costs allowing graceful omission with explicit `skip_reason` codes; if must visit stops are temporally unreachable or forbidden, the solver returns status `INFEASIBLE` with actionable diagnostics. (Slice 1 complete)
- [x] AC-6: Route records, scheduled stops, legs with GeoJSON LineString geometries, and applied restrictions persist in PostgreSQL tables (`routes`, `route_stops`, `route_legs`, `route_restrictions`) with UUID foreign keys to `canonical_places.id`, `claims.id`, `executions.id`, and `trips.id`, storing fuel costs as `Decimal` values with `Numeric(20, 6)` precision. (Slice 2 complete)
- [ ] AC-7: The routing adapter connects to an external Valhalla HTTP service for road and pedestrian matrices and turn by turn path geometries, specifying explicit coordinate order, distance in meters, duration in seconds, snapping behavior, socket timeouts, bounded retries, and offline mock fixtures for testing without cloud deployment. (Slice 4)
- [ ] AC-8: Mid trip replanning accepts current traveler coordinates, freezes completed stops with historical arrival timestamps, treats the current position as the new departure origin, optimizes remaining stops, and writes an immutable incremented `route_version` referencing `previous_route_version` under optimistic concurrency controls. (Slice 5)
- [ ] AC-9: Route optimization tasks execute asynchronously via an `optimizer` worker with action `solve` claimed through `WorkerFencingService` on SQS, respecting parent execution lease fences, deadlines, cooperative cancellation, and atomic route completion while reusing the existing `/api/v1/jobs/{job_id}` polling and execution identity. (Slice 6)

## Specced surfaces (Slice 1)

- [x] Contract enums in `backend/src/contracts/routing.py`: `RouteObjective`, `RouteStatus`, `SolverStatus`, `StopType`, `TrafficBasis`.
- [x] Coordinate contract `Point` in `backend/src/contracts/routing.py`: geographic coordinate validating latitude between -90 and 90, and longitude between -180 and 180.
- [x] Input contracts in `backend/src/contracts/routing.py`: `RouteStopInput` and `RouteRequest` with departure time, return deadline, objective, and stop definitions.
- [x] Matrix contracts in `backend/src/contracts/routing.py`: `MatrixElement`, `MatrixRequest`, and `MatrixResponse` supporting asymmetric durations and distances.
- [x] Scheduled output contracts in `backend/src/contracts/routing.py`: `ScheduledStopOutput`, `RouteLegOutput`, and `OptimizedRoute` with complete elapsed time and distance breakdowns.
- [x] Backward compatibility preservation in `backend/src/contracts/routing.py`: `VehicleFuelProfile`, `RoadSegment`, `RoadBudgetCalculator`, and legacy aliases `RoutePoint`, `RouteLeg`, `RouteResponse`, `RouteAlternative`.
- [x] Fixed 5x5 asymmetric matrix fixture in `backend/src/functions/route_optimizer/fixtures.py`: durations and distances matrix with directional asymmetry and an unreachable pair (1, 3).
- [x] Constraint solver `OrToolsRoutingSolver` in `backend/src/functions/route_optimizer/solver.py`:
  - Enforces start and end semantics (round trip, fixed terminus, and open route).
  - Enforces asymmetric durations and distances without matrix corruption.
  - Forbids unreachable transitions via arc removal on routing next variables.
  - Enforces hard service windows, wait accounting, and dwell completion before venue closing.
  - Enforces return deadline compliance at destination terminus.
  - Solves multi criteria integer objectives (FASTEST, SHORTEST, BALANCED) using integer weights.
  - Disjunctions with concrete integer drop penalty (10,000,000) for optional stops with explicit skip reasons.
  - Honest status mapping: OPTIMAL requires mathematical proof (ROUTING_OPTIMAL), finding a solution yields FEASIBLE, timeout yields TIME_LIMIT with UNAVAILABLE route status, and impossible mandatory stops yield INFEASIBLE.
- [x] Scheduler facade adapter `ItineraryScheduler` in `backend/src/functions/optimizer/solver.py`: delegates to `OrToolsRoutingSolver` without network requests or presenting haversine as road routing.
- [x] Comprehensive test suite in `backend/tests/unit/test_route_optimizer_slice1.py`: 15 passed unit tests verifying all 7 Slice 1 core criteria.

## Specced surfaces (Slice 2)

- [x] Database migration in `backend/infrastructure/migrations/versions/0012_add_route_optimization.py`:
  - Creates tables `routes`, `route_stops`, `route_legs`, `route_restrictions`.
  - Enforces foreign keys to `trips.id`, `executions.id`, `canonical_places.id`, and `claims.id`.
  - Defines `Numeric(20, 6)` fuel cost amounts, GeoJSON geometries, and PostGIS GiST spatial indexes.
- [x] Restriction contracts in `backend/src/contracts/routing.py`:
  - `RestrictionType` enum covering road closures, vehicle zones, time windows, and delays.
  - `RouteRestrictionOutput` frozen contract model linked to claims and containing geometry representations.
  - `restrictions` field added to `OptimizedRoute`.
- [x] SQLAlchemy 2.0 ORM models in `backend/src/functions/orchestrator/routing/models.py`:
  - `RouteModel`, `RouteStopModel`, `RouteLegModel`, and `RouteRestrictionModel`.
  - Cascade delete relationships and bidirectionally linked entity graphs.
  - Domain converters transforming ORM records to canonical routing contracts.
- [x] PostGIS persistence repository in `backend/src/functions/orchestrator/routing/repository.py`:
  - Atomic persistence in single transactions (`save_route`).
  - Tenant isolation enforcement rejecting unauthorized owner queries (`TenantMismatchError`).
  - Optimistic versioning conflict prevention rejecting duplicate trip versions (`RouteVersionConflictError`).
  - Safe PostGIS Shapely conversion for coordinates and LineString geometries.
- [x] Integration test suite in `backend/tests/integration/test_route_optimizer_slice2.py`:
  - 4 live database tests verifying schema, indexes, atomic save and load, tenant fencing, and route version conflicts.

## Specced surfaces (Slice 3)

- [x] Configuration parameter `DEFAULT_PARKING_SEARCH_RADIUS_METERS` in `backend/src/config/config.py`:
  - Configures search radius for candidate parking facilities (default 1000m).
- [x] Multimodal routing domain extensions in `backend/src/contracts/routing.py`:
  - Added `is_pedestrian_only`, `vehicle_restricted`, and `parking_hub_id` to `RouteStopInput`.
  - Added `transport_mode` to `RouteRequest`.
- [x] Spatial parking discovery query in `backend/src/functions/orchestrator/places/repository.py`:
  - `find_verified_parking_near` method using PostGIS `ST_DWithin` on geography, checking `is_candidate == False`, public accessibility, and parking amenities.
- [x] Spatial continuity validator in `backend/src/functions/route_optimizer/continuity.py`:
  - `VehicleContinuityTracker` enforcing that driving legs only depart from where the vehicle is parked, detecting vehicle teleportation or abandonment.
  - Exceptions `VehicleContinuityViolationError` and `NoVerifiedParkingHubError`.
- [x] Multimodal transition coordinator in `backend/src/functions/route_optimizer/multimodal.py`:
  - `MultimodalTransitionService` injecting `PARKING_HUB` stops, splitting driving and walking legs, and supporting walk chaining between adjacent pedestrian stops.
  - Safe closed failure returning status `INFEASIBLE` with diagnostic `NO_VERIFIED_PARKING_HUB` when no verified parking exists.
  - Strict distance segregation and zero fuel cost for walking legs.
- [x] Unit test suite in `backend/tests/unit/test_route_optimizer_slice3.py`:
  - 8 unit tests verifying vehicle continuity tracking, parking hub injection, walk chaining, walking round trips, and safe failure.
- [x] Integration test suite in `backend/tests/integration/test_route_optimizer_multimodal_slice3.py`:
  - 4 live database tests verifying PostGIS spatial parking queries with bicycle exclusion, multimodal route planning with real places, and route persistence.

## Commands

- [x] `uv run pytest tests/unit/test_route_optimizer_slice1.py -v`: 15 passed in ~11s: AC-1, AC-2, AC-5
- [x] `uv run pytest tests/integration/test_route_optimizer_slice2.py -v`: 4 passed in ~17s: AC-6
- [x] `uv run pytest tests/unit/test_route_optimizer_slice3.py -v`: 8 passed in ~2s: AC-4, AC-5
- [x] `uv run pytest tests/integration/test_route_optimizer_multimodal_slice3.py -v`: 4 passed in ~12s: AC-4, AC-6
- [x] `uv run pytest tests/unit/test_route_optimizer_slice1.py tests/integration/test_route_optimizer_slice2.py tests/unit/test_route_optimizer_slice3.py tests/integration/test_route_optimizer_multimodal_slice3.py`: 31 passed in ~40s: AC-1, AC-2, AC-4, AC-5, AC-6
- [x] `uv run ruff check src/config/config.py src/contracts/routing.py src/functions/orchestrator/places/repository.py src/functions/route_optimizer src/functions/orchestrator/routing tests/unit/test_route_optimizer_slice3.py tests/integration/test_route_optimizer_multimodal_slice3.py`: All checks passed
- [x] `uv run mypy src/contracts/routing.py src/functions/orchestrator/places/repository.py src/functions/route_optimizer src/functions/orchestrator/routing`: Success, zero issues found in 10 source files
