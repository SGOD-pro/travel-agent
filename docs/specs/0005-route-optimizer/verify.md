# Verify: Route Optimizer and Multimodal Itinerary Planning, spec 0005, updated 2026-10-23
_Steps derived from spec 0005 acceptance criteria. /check verify runs these; /test locks the durable ones._

## Acceptance criteria (Slice 1)

- [x] AC-1: SWENA defines internal typed domain contracts (`RouteRequest`, `RouteStopInput`, `OptimizedRoute`, `ScheduledStopOutput`, `RouteLegOutput`, `MatrixRequest`, `MatrixResponse`, `RouteObjective`, `RouteStatus`, `SolverStatus`, `StopType`, `TrafficBasis`) in `contracts/routing.py` that encapsulate multi stop itinerary inputs, provider matrices, and scheduled outputs while preserving existing fuel profile, road segment, and budget calculator types. (Slice 1 complete)
- [x] AC-2: The route optimizer executes a hybrid constraint solver combining Google OR-Tools constraint programming with asymmetric duration and distance matrices, enforcing hard time windows, dwell durations, wait time accounting, optional stop drop penalties, and user chosen objectives (`FASTEST`, `SHORTEST`, `BALANCED`). (Slice 1 complete)
- [x] AC-3: The optimization engine ingests verified spatial claims from specification 0002, applying strict road prohibition via Valhalla `exclude_polygons` and revalidating final leg geometries against active restriction intervals, treating unsupported restrictions as explicit and failing closed safely. (Slice 4 complete)
- [x] AC-4: When a destination is flagged as pedestrian only or vehicle restricted, the solver automatically identifies a verified parking hub from `canonical_places`, injecting a `PARKING_HUB` stop and maintaining strict vehicle continuity where driving legs only depart from the stop where the vehicle is currently parked, never inventing parking permissions from raw road nodes. (Slice 3 complete)
- [x] AC-5: The solver enforces strict priority partitioning: must visit stops are hard constraints that must be fulfilled, while optional stops have penalty costs allowing graceful omission with explicit `skip_reason` codes; if must visit stops are temporally unreachable or forbidden, the solver returns status `INFEASIBLE` with actionable diagnostics. (Slice 1 complete)
- [x] AC-6: Route records, scheduled stops, legs with GeoJSON LineString geometries, and applied restrictions persist in PostgreSQL tables (`routes`, `route_stops`, `route_legs`, `route_restrictions`) with UUID foreign keys to `canonical_places.id`, `claims.id`, `executions.id`, and `trips.id`, storing fuel costs as `Decimal` values with `Numeric(20, 6)` precision. (Slice 2 complete)
- [x] AC-7: The routing adapter connects to an external Valhalla HTTP service for road and pedestrian matrices and turn by turn path geometries, specifying explicit coordinate order, distance in meters, duration in seconds, snapping behavior, socket timeouts, bounded retries, and offline mock fixtures for testing without cloud deployment. (Slice 4 complete)
- [x] AC-8: Mid trip replanning accepts current traveler coordinates, freezes completed stops with historical arrival timestamps, treats the current position as the new departure origin, optimizes remaining stops, and writes an immutable incremented `route_version` referencing `previous_route_version` under optimistic concurrency controls. (Slice 5 complete)
- [x] AC-9: Route optimization tasks execute asynchronously via an `optimizer` worker with action `solve` claimed through `WorkerFencingService` on SQS, respecting parent execution lease fences, deadlines, cooperative cancellation, and atomic route completion while reusing the existing `/api/v1/jobs/{job_id}` polling and execution identity. (Slice 6 complete)

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

## Specced surfaces (Slice 4)

- [x] Configuration parameters in `backend/src/config/config.py` and environment template `backend/.env.example`:
  - `VALHALLA_ENDPOINT_URL` with `ROUTING_BASE_URL` alias pointing to external Valhalla instance.
  - `ORTOOLS_SOLVER_TIME_LIMIT_SECONDS` (default 10 seconds).
  - `VALHALLA_CONNECT_TIMEOUT_SECONDS` (default 2.0 seconds).
  - `VALHALLA_READ_TIMEOUT_SECONDS` (default 5.0 seconds).
  - `VALHALLA_MAX_RETRIES` (default 2 retries).
- [x] Valhalla HTTP client in `backend/src/functions/route_optimizer/valhalla.py`:
  - `ValhallaClient` with connection pooling, coordinate boundary validation, and SSRF rejection on unsafe hosts or schemes.
  - Translates `TransportMode` to Valhalla costing models (`auto`, `motorcycle`, `pedestrian`, `bicycle`).
  - Calls `/sources_to_targets` matrix endpoint, converting kilometers to integer meters and durations to integer seconds, handling null or unreachable cells honestly.
  - Calls `/route` endpoint, extracting turn maneuvers and decoding polyline6 precision shapes into GeoJSON LineString coordinates.
  - Handles server errors (5xx) with bounded exponential backoff retries and fast fails on client errors (4xx).
  - Custom typed exceptions: `ValhallaError`, `ValhallaConnectionError`, `ValhallaTimeoutError`, `ValhallaRoutingError`, `ValhallaSSRFError`.
- [x] Closure avoidance service in `backend/src/functions/route_optimizer/closures.py`:
  - `ClosureAvoidanceService` and `RestrictionClaim` domain model.
  - Filters restrictions by temporal applicability using leg traversal intervals (`check_temporal_overlap`).
  - Transforms GeoJSON Polygon outer rings and buffered LineString corridors into Valhalla `exclude_polygons` query parameters.
  - Post routing geometric revalidation: checks Shapely intersection between leg LineStrings and active restriction polygons.
  - Strict fail closed safety: returns `RouteStatus.INFEASIBLE` with diagnostic `UNSUPPORTED_RESTRICTION` when restrictions cannot be mapped or when post routing intersection occurs.
- [x] Offline test fixture in `backend/src/functions/route_optimizer/fixtures.py`:
  - `MockValhallaClient` simulating matrix responses and route geometries without network dependencies.
- [x] Unit test suite in `backend/tests/unit/test_route_optimizer_slice4.py`:
  - 20 unit tests verifying coordinate validation, SSRF checks, missing longitude ValueError, polyline6 decoding, costing mappings, matrix parsing, route parsing, retry backoff on 5xx, fast failure on 4xx, timeouts, exclusion polygons, MultiPolygon extraction, line buffering, unsupported restriction rejection, temporal filtering, spatial intersection detection, and mock client offline simulation.
- [x] Connected integration test gate in `backend/tests/integration/test_valhalla_connected_slice4.py`:
  - Connected test gate verifying live `/status`, `/sources_to_targets`, and `/route` queries when `VALHALLA_ENDPOINT_URL` is configured, skipping cleanly when offline.

## Specced surfaces (Slice 5)

- [x] Mid trip replan request contract in `backend/src/contracts/routing.py`:
  - `RouteReplanRequest` model validating current traveler coordinates, replan departure timestamp, completed stop UUIDs, expected previous version for optimistic concurrency checks, and optional overrides for objective, return deadline, transport mode, and vehicle fuel profile.
- [x] Mid trip dynamic replanning service `RouteReplanService` in `backend/src/functions/route_optimizer/replan.py`:
  - Enforces geographic boundary validation with SSRF prevention.
  - Enforces optimistic concurrency checks raising `RouteVersionConflictError` on stale previous version tokens.
  - Freezes completed stops preserving historical arrival and departure timestamps from prior version.
  - Discards previous departure origin unless completed, and injects traveler's live coordinates as new origin stop at `stop_order = 0`.
  - Re-solves remaining stops via OR-Tools constraint programming with hard time windows, dwell durations, and optional drop penalties.
  - Reconstructs continuous stop and leg sequences across frozen completed segments and newly planned remaining segments.
  - Recomputes aggregate travel time, wait duration, dwell seconds, and canonical driving-only fuel costs with `Numeric(20, 6)` Decimal precision.
  - Generates immutable incremented `route_version` referencing `previous_route_version`.
  - Provides `replan_and_persist` performing tenant-isolated route retrieval and atomic persistence in PostgreSQL via `RouteRepository.save_route`.
  - Clean edge case handling for zero completed stops, all stops completed, and dropped optional stops on tight deadlines.
- [x] Unit test suite in `backend/tests/unit/test_route_optimizer_slice5.py`:
  - 9 unit tests verifying stop freezing, version incrementing, optimistic version conflicts, SSRF coordinate validation, zero completed stops, all stops completed, dropped optional stops on tight deadlines, safe empty stop list handling during infeasible replans, retention of previously dropped stops in audit records, and decimal fuel calculations.
- [x] Integration test suite in `backend/tests/integration/test_route_optimizer_replan_slice5.py`:
  - 2 live PostgreSQL/PostGIS database tests verifying end-to-end replan persistence, immutable version history, and cross-tenant replan fencing.

## Specced surfaces (Slice 6)

- [x] Worker output and response contracts in `backend/src/contracts/routing.py`:
  - `RouteOptimizerWorkerOutput` and `AcceptedRouteResponse`.
- [x] Route optimizer worker service in `backend/src/functions/route_optimizer/service.py`:
  - `RouteOptimizerWorkerService` claiming tasks under `WorkerFencingService`.
  - Supports actions `solve` and `replan`.
  - Enforces parent execution cancellation check and deadline barriers.
  - Persists routes via `RouteRepository.save_route` and completes tasks atomically under lease fencing.
- [x] Dedicated SQS Lambda consumer in `backend/src/functions/route_optimizer/consumer.py`:
  - `process_record` and `process_batch` with partial batch failure reporting (`batchItemFailures`).
  - Remaining Lambda execution time deadline guards (failing messages when remaining time drops under 5000ms for safe redrive).
- [x] Standalone packaging and SAM infrastructure:
  - `backend/src/functions/route_optimizer/packaging.py`: builds standalone zip artifact verified at 0.26 MB (under 250 MB ceiling).
  - `backend/src/functions/route_optimizer/template.yaml`: SAM template configuring `RouteOptimizerFunction`, SQS queue, and dead letter queue with redrive policy.
  - `backend/src/functions/route_optimizer/requirements.txt`: runtime dependencies without orchestrator or LangGraph bloat.
- [x] Asynchronous REST API endpoints in `backend/src/functions/api/routes/optimization.py`:
  - `POST /api/v1/optimization/routes`: validates input, reserves idempotency, stages execution and task rows with `PENDING` status, dispatches outbox event, and returns 202 Accepted.
  - `GET /api/v1/optimization/routes/{route_id}`: fetches full route itinerary with owner tenant isolation, returning 403 Forbidden on tenant mismatch.
  - `POST /api/v1/optimization/routes/{route_id}/replans`: validates replan body, enforces caller ownership, stages replan execution and task rows, and returns 202 Accepted.
  - Registered in `backend/src/functions/api/app.py` under router prefix `/api/v1`.
- [x] Unit test suite in `backend/tests/unit/test_route_optimizer_slice6.py`:
  - 12 unit tests verifying envelope parsing, worker and action validation, cooperative cancellation, deadline checks, SQS discard on fencing conflicts, partial batch failures, packaging limits, API validation, and tenant isolation 403 checks.
- [x] Integration test suite in `backend/tests/integration/test_route_optimizer_worker_slice6.py`:
  - Live PostgreSQL and PostGIS test executing end to end flow: API submission, SQS consumption, worker solve under lease fencing, route persistence, job polling, route retrieval, tenant isolation checks, and version 2 mid trip replanning.

## Specced surfaces (Slice 7)

- [x] Packaging verification and package auditing in `backend/src/functions/route_optimizer/packaging.py`:
  - `audit_package_contents`: audits package contents preventing bundling of Valhalla C++ binaries or OSM graph tiles.
  - Confirms uncompressed package size <= 250 MB (verified at 0.27 MB) and compressed size <= 50 MB (verified at 0.07 MB).
  - Proves handler import in clean isolated environment without fallback to repo root.
  - CLI runner supporting `--output-dir`, `--backend-root`, `--install-dependencies`, and `--verify-only`.
- [x] Unit test suite in `backend/tests/unit/test_route_optimizer_slice7.py`:
  - 6 unit tests verifying SAM template alignment, requirements lean boundaries and exclusions, standalone packaging size limits, audit detection of forbidden assets/tiles, isolated import proof, and CLI entrypoint.
- [x] Connected provider acceptance test suite in `backend/tests/integration/test_valhalla_connected_acceptance_slice7.py`:
  - 6 integration tests verifying connected gate skip logic in offline mode, actionable diagnostics on health probe failure when enabled, live asymmetric matrix `/sources_to_targets`, turn-by-turn maneuvers and geometries `/route`, multimodal network costing segregation (driving vs walking), hard road closure avoidance via `exclude_polygons`, and end-to-end multi-stop itinerary optimization connecting live Valhalla matrices with `OrToolsRoutingSolver`.

## Commands

- [x] `uv run pytest tests/unit/test_route_optimizer_slice1.py -v`: 15 passed in ~11s: AC-1, AC-2, AC-5
- [x] `uv run pytest tests/integration/test_route_optimizer_slice2.py -v`: 4 passed in ~17s: AC-6
- [x] `uv run pytest tests/unit/test_route_optimizer_slice3.py -v`: 8 passed in ~2s: AC-4, AC-5
- [x] `uv run pytest tests/integration/test_route_optimizer_multimodal_slice3.py -v`: 4 passed in ~12s: AC-4, AC-6
- [x] `uv run pytest tests/unit/test_route_optimizer_slice4.py -v`: 20 passed in ~3s: AC-3, AC-7
- [x] `uv run pytest tests/integration/test_valhalla_connected_slice4.py -v`: 1 skipped (offline gate) in ~0.05s: AC-7
- [x] `uv run pytest tests/unit/test_route_optimizer_slice5.py -v`: 9 passed in ~51s: AC-8
- [x] `uv run pytest tests/integration/test_route_optimizer_replan_slice5.py -v`: 2 passed in ~21s: AC-8
- [x] `uv run pytest tests/unit/test_route_optimizer_slice6.py -v`: 12 passed in ~2.3s: AC-9
- [x] `uv run pytest tests/integration/test_route_optimizer_worker_slice6.py -v`: 1 passed in ~45s: AC-9
- [x] `uv run pytest tests/unit/test_route_optimizer_slice7.py -v`: 6 passed in ~3.6s: AC-7, AC-9
- [x] `uv run pytest tests/integration/test_valhalla_connected_acceptance_slice7.py -v`: 6 skipped (offline gate) in ~0.1s: AC-3, AC-7, AC-9
- [x] `uv run ruff check src/functions/route_optimizer/packaging.py tests/unit/test_route_optimizer_slice7.py tests/integration/test_valhalla_connected_acceptance_slice7.py`: All checks passed
- [x] `uv run mypy src/functions/route_optimizer/packaging.py tests/unit/test_route_optimizer_slice7.py tests/integration/test_valhalla_connected_acceptance_slice7.py`: Success, zero issues found in 3 source files

