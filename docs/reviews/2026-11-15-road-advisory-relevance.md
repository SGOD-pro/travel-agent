# Review, 0007 Slice 8 Road Route Advisory Relevance, 2026-11-15

**Reviewed by**: Staff Reviewer (subagent)
**Scope**: 3 files, commit `ed23868`
**Verdict**: Approve

## Summary

Commit `ed23868` implements physical road route geometry extraction and advisory date boundary reconciliation for the trip planner orchestrator under specification 0007 Slice 8. The change addresses a fundamental safety requirement: road hazard relevance must be evaluated against the actual road polyline traversed by the traveler, never against naive straight line segments connecting stops. In addition, the change provides robust date boundary reconciliation, marking expired notices against the current UTC date when trip start dates are omitted, preserving interval uncertainty when dates are missing, and enforcing strict fail closed quarantine when evidence is absent. All verifications have passed with zero type errors, zero linter issues, and all regression suites green.

## Scope of Review

The review covers the following three modified files:
* [backend/src/functions/orchestrator/graphs/trip_planner/graph.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py)
* [backend/src/functions/orchestrator/evidence/claim_ingestion.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/evidence/claim_ingestion.py)
* [backend/tests/integration/test_claim_ingestion.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py)

## Detailed Findings and Verification

### 1. Road Route Geometry Extraction and Corridor Verification

In [graph.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3193-L3280), the helper function `_extract_road_corridor_coordinates` extracts ordered physical road corridor coordinates from the selected route geometry.

Key architectural properties verified:
* **True road polyline inspection**: The implementation retrieves geometry from `state["route_geometry"]` or `state["optimized_schedule"]["route_geometry"]` or individual leg geometries in `state["optimized_schedule"]["legs"]`.
* **Zero straight line fallback**: Straight line segments between origin, destination, and stops are never constructed or substituted for road geometry. If route geometry is unavailable or invalid, the helper returns an empty list, forcing the orchestrator to fall back to textual matching or fail closed safety rather than inventing artificial geometric paths.
* **GeoJSON format support**: The coordinate parser cleanly handles GeoJSON `LineString`, `MultiLineString`, `Feature`, `FeatureCollection`, and coordinate dictionaries with `lat` and `lng` keys, validating each coordinate pair via `validate_coordinates`.

### 2. Spatial Relevance Evaluation Against Road Polyline vs Fallback

In [graph.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3608-L3641), spatial relevance logic in `_build_advisory_research` evaluates warnings against physical road corridors:
* When physical road corridor coordinates exist (`road_corridor_coords`), distance from the advisory coordinate to the road polyline is computed via `compute_corridor_distance_km`. If within 100.0 km, it evaluates to `WITHIN_CORRIDOR`; otherwise, it evaluates to `OUTSIDE_CORRIDOR`.
* Crucially, when road corridor coordinates are present, text matching is bypassed. This prevents hazards located far from a mountain detour from matching merely because the notice mentions the region name.
* When road corridor coordinates are absent, the system falls back to text matching against verified trip places (`origin_name`, destination names, scheduled stop names).
* When both road geometry and trip location evidence are absent (such as an empty brief), spatial relevance evaluates to `UNKNOWN` and is quarantined to `unverified_advisories` with `uncertainty = "UNKNOWN_RELEVANCE"`. It never defaults to `WITHIN_CORRIDOR`.

### 3. Date Boundary Reconciliation and Temporal Freshness

In [graph.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3569-L3583), temporal evaluation handles partial and missing trip dates with precision:
* `effective_start = trip_start_date if trip_start_date is not None else now_date`: When `trip_start_date` is omitted, ended warnings evaluate against the current UTC date. If the advisory ended before the current date, it is classified as `EXPIRED` (`status = "EXPIRED"`, `uncertainty = "EXPIRED"`) and placed in `expired_advisories`.
* When neither `trip_start_date` nor `trip_end_date` is provided, and the advisory has not expired and is not undated, `interval_uncertain` evaluates to true (`status = "UNKNOWN_INTERVAL"`, `uncertainty = "UNKNOWN_INTERVAL"`). The advisory is quarantined in `unverified_advisories` rather than asserted as active.
* Future warnings scheduled beyond `trip_end_date` evaluate to `FUTURE` (`uncertainty = "FUTURE_ADVISORY"`) and are excluded from active alerts.

### 4. Propagation of Route Geometry into Final Proposal

In [graph.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3834) and [graph.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3851), `route_geometry` is explicitly preserved in the `proposal` dictionary and returned in the node output dictionary of `synthesize_proposal_node`. This guarantees downstream consumers receive the exact road polyline used during advisory evaluation.

### 5. Ingestion of Past Ended Notices in Claim Ingestion

In [claim_ingestion.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/evidence/claim_ingestion.py#L917-L926), `ingest_claims_from_observations` reconciles advisory dates during evidence ingestion:
* Any observation with a parsed end timestamp strictly prior to the current UTC date (`parsed_end.date() < now.date()`) is marked with `status = "EXPIRED"` and `uncertainty = "EXPIRED"`.
* This ensures that claims stored in PostgreSQL reflect the expired status immediately, preventing stale warnings from surfacing as active in downstream pipelines.

### 6. Verification of the Five Proposal Path Regressions

In [test_claim_ingestion.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3008-L3381), five regression tests prove the correctness of the proposal path:

1. **Road route bending away from straight line between stops** ([test_claim_ingestion.py:3008](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3008)):
   A road route bends north through latitude 22.0 between stops at latitude 20.0. A landslide warning located at (22.0, 82.0) is over 220 km away from the straight line between stops, but 0 km from the road polyline. The test verifies that the warning is correctly classified as `WITHIN_CORRIDOR` and `ACTIVE`.
2. **Warning near straight line but outside selected road route** ([test_claim_ingestion.py:3092](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3092)):
   A flood warning at (20.0, 82.0) sits directly on the straight line between stops, but is over 180 km away from the northern road detour. The test confirms that the warning is excluded from active advisories, categorized as `OUTSIDE_CORRIDOR` in `unrelated_advisories`, and yields `ADVISORY_UNKNOWN`.
3. **Missing route geometry and empty brief** ([test_claim_ingestion.py:3172](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3172)):
   With route geometry set to `None` and an empty brief, the test confirms that the warning is quarantined in `unverified_advisories` with `relevance = "UNKNOWN"` and `uncertainty = "UNKNOWN_RELEVANCE"`.
4. **Expired warning when trip start is missing** ([test_claim_ingestion.py:3229](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3229)):
   With no start or end date in the brief, an advisory that ended five days ago evaluates against the current UTC date. The test verifies it is placed in `expired_advisories` with `status = "EXPIRED"` and `uncertainty = "EXPIRED"`.
5. **Relevant current authoritative warning on selected route** ([test_claim_ingestion.py:3293](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3293)):
   A valid warning from West Bengal Traffic Police on NH-116B submerged at Nandakumar matches the selected route geometry coordinates. The test verifies it evaluates to `WITHIN_CORRIDOR`, `ACTIVE`, `CONFIRMED_AUTHORITATIVE`, with full provenance and route diagnostic notices attached.

## Architectural and Operational Strengths

* **Defense in depth**: Spatial verification requires verified coordinates and actual road geometry, eliminating false positive alerts caused by coarse bounding boxes or straight lines.
* **Fail closed integrity**: An absence of evidence never results in an unverified assertion of safety. Missing geometry, empty briefs, and indeterminate time windows cleanly quarantine records to unverified pools.
* **Surgical implementation**: Edits are contained strictly to the orchestrator graph node and claim ingestion service, cleanly fulfilling specification requirements without unnecessary refactoring.
* **Immutability and replay safety**: Route geometry and advisories are propagated downstream without mutating solver schedules or database entities unexpectedly.

## Verification Test Execution Results

All automated test and verification gates passed with zero errors:
* `uv run ruff check src tests`: Passed with 0 issues.
* `uv run mypy src/contracts/llm.py src/functions/api/routes/trips.py src/functions/evidence_collector/scrape.py src/functions/evidence_collector/service.py src/functions/orchestrator/evidence/query_planner.py src/functions/orchestrator/evidence/place_extraction.py src/functions/orchestrator/evidence/claim_ingestion.py src/functions/orchestrator/evidence/eligibility.py src/functions/orchestrator/places/candidate_service.py src/functions/orchestrator/graphs/trip_planner/graph.py src/functions/orchestrator/evidence/policy.py`: Passed with 0 type errors.
* `uv run pytest tests/integration/test_claim_ingestion.py -k "test_advisory"`: 16 of 16 tests passed in 0.64s.
* `uv run pytest tests/integration/test_claim_ingestion.py`: 46 of 46 tests passed in 3.79s.
* `uv run pytest tests/integration/test_trip_planner_evidence_runner_lifecycle.py`: 5 of 5 tests passed in 18.96s.
* `RUN_CONNECTED_EVIDENCE_TESTS=1 uv run pytest -s tests/integration/test_trip_planner_evidence_connected.py`: 1 of 1 passed in 23.85s.

## Conclusion and Verdict

Verdict: **Approve**.

The implementation is verified, thoroughly tested, and conforms fully to specification 0007 Slice 8. It is ready for production merge.
