# Code Review: Lodging Budget Review Follow Ups

* Review Date: 23 November 2026
* Scope: Slice 0007 lodging budget review follow ups
* Reviewer: Senior Staff Engineer (Independent Review)
* Target Files:
  - backend/src/functions/api/service.py
  - backend/src/functions/orchestrator/graphs/trip_planner/graph.py
  - backend/tests/unit/test_trip_planner_proposal_slice5.py
  - docs/reviews/2026-11-22-0007-live-routed-itinerary-review.md
  - docs/specs/0007-trip-planner-e2e/index.md
* Preserved Historical Artifacts:
  - backend/scripts/live_journey_result_bengaluru_mysuru_madikeri.json
  - backend/scripts/live_journey_result_mysuru_srirangapatna_mandya.json
* Verdict: Approve

## Executive Summary

This formal senior code review evaluates the uncommitted changes for slice 0007 lodging budget review follow ups. The changes resolve remaining minor items and documentation discrepancies identified during previous reviews and live itinerary evaluations.

Specifically, the PDF export fallback in service.py now cleanly omits the accommodation table row when calendar nights equal zero, and scales quantities accurately as rooms multiplied by calendar nights. The orchestrator graph and the PDF service now format basis labels with grammatically correct singular and plural wording, producing 1 room versus N rooms and 1 night versus N nights instead of unconditional plural nouns. The 0007 specification design table is reconciled with calendar night accounting, and explicitly notes that per hotel itemization is deferred until hotels are assigned to specific nights. The historical live review document is corrected to reflect the true GeoJSON structure of 20 features, consisting of 10 Point stops, 9 LineString legs, and 1 Polygon road closure restriction feature, with a top level bounding box property. Both historical live execution artifacts remain preserved without modification. Remote LAYA remains disabled with zero external calls, and no out of scope modifications were introduced.

All unit tests pass with zero failures. The changes are surgical, robust, well verified, and approved for integration.

## Target Files and Scope Analysis

The uncommitted changes are strictly confined to five files:

1. backend/src/functions/api/service.py:
Updates the PDF export fallback budget table construction. When calendar nights equal zero, the accommodation row is omitted entirely. When calendar nights exceed zero, the quantity reflects rooms multiplied by calendar nights, and the basis description formats room count using singular or plural grammar (1 room across destinations or N rooms across destinations).

2. backend/src/functions/orchestrator/graphs/trip_planner/graph.py:
Updates compute_budget_node in the trip planning graph. Pluralization logic is introduced for rooms and nights labels so that singular values display 1 room and 1 night, while plural values display N rooms and N nights.

3. backend/tests/unit/test_trip_planner_proposal_slice5.py:
Updates existing regression assertions to expect singular grammar for 1 room and 1 night. Implements helper _extract_pdf_decompressed_stream and adds three new unit tests validating PDF fallback generation for same day trips, single room single night trips, and multiple room trips.

4. docs/reviews/2026-11-22-0007-live-routed-itinerary-review.md:
Corrects the description of the map delivery payload in both the technical narrative and verification matrix table. Accurately identifies the 20 features as 10 Point stops, 9 LineString road legs, and 1 Polygon road closure restriction, alongside the top level bounding box.

5. docs/specs/0007-trip-planner-e2e/index.md:
Reconciles the Stage 8 budget row in the execution stages table to state that hotel stay calculation uses trip calendar nights rather than destination stay days, and notes that per hotel itemization is deferred until hotels are assigned to specific nights.

Historical run artifacts in backend/scripts/ were verified using git status and file inspection. Both live_journey_result_bengaluru_mysuru_madikeri.json and live_journey_result_mysuru_srirangapatna_mandya.json remain intact and unmodified.

## Requirements Compliance Evaluation

### 1. Omit Accommodation Row from PDF Fallback When Calendar Nights Are Zero
PASS. In service.py, calendar nights are computed from the difference between brief.end_date and brief.start_date. The accommodation table row is appended only within an if calendar_nights > 0 block. When calendar nights equal zero, such as on same day trips, the accommodation row is completely omitted.

### 2. Positive Quantities Equal to Rooms Multiplied by Calendar Nights in PDF Fallback
PASS. When calendar nights exceed zero, the accommodation line sets quantity to str(brief.rooms * calendar_nights). Because both rooms and calendar nights are positive integers, the quantity is guaranteed positive and mathematically sound.

### 3. Correct Singular and Plural Wording in Lodging Budget Basis Label
PASS. In graph.py, the basis string construction uses:
- rooms_label: 1 room if brief.rooms == 1 else f"{brief.rooms} rooms"
- nights_label: 1 night if calendar_nights == 1 else f"{calendar_nights} nights"
This produces hotel_stay_{loc_str} (1 room, 1 night) for single room single night stays, and scales plurals appropriately. In service.py, rooms_str similarly distinguishes 1 room from N rooms.

### 4. Comprehensive Test Verification for PDF Fallback Scenarios
PASS. Three dedicated unit tests were added to test_trip_planner_proposal_slice5.py:
- test_pdf_export_fallback_same_day_trip_omits_accommodation: Proves that Accommodation does not appear in the decompressed PDF stream for same day itineraries.
- test_pdf_export_fallback_one_room_one_night: Proves singular label 1 room across destinations and quantity 1 in the stream.
- test_pdf_export_fallback_multiple_rooms: Proves plural label 3 rooms across destinations and quantity 6 for 3 rooms spanning 2 nights.
All three tests pass cleanly.

### 5. Per Hotel Itemization Recorded as Deferred Until Specific Night Assignment
PASS. The specification table in docs/specs/0007-trip-planner-e2e/index.md explicitly records that per hotel itemization is deferred until hotels are assigned to specific nights.

### 6. Reconcile 0007 Design Table with Calendar Night Accounting
PASS. The design table in docs/specs/0007-trip-planner-e2e/index.md now explicitly defines lodging calculation as Trip calendar nights (end date minus start date) multiplied by room count benchmark (labeled ESTIMATED), clarifying that destination stay days do not multiply hotel nights.

### 7. Correct GeoJSON Breakdown in Historical Review Document
PASS. docs/reviews/2026-11-22-0007-live-routed-itinerary-review.md was updated to correctly identify the 20 features: 10 Point stops, 9 LineString legs, and 1 Polygon road closure restriction feature, with a top level bounding box property. This matches the raw json data in backend/scripts/live_journey_result_mysuru_srirangapatna_mandya.json.

### 8. Verify Remote LAYA Status
PASS. Remote LAYA remains completely disabled. LAYA_ENDPOINT_URL remains unconfigured, resulting in safe local singleton bypasses with zero remote HTTP calls.

### 9. Verify Scope Boundaries
PASS. Git diff inspection confirms no changes to overnight scheduling, event opening hours, or meal arrangements. All modifications relate strictly to the lodging budget review follow ups.

## Detailed Findings

### Blockers
None.

### Majors
None.

### Minors
None. All minor follow ups identified in previous reviews have been resolved cleanly.

### Strengths
- High surgical precision: Exactly 5 files modified with no extraneous formatting or scope creep.
- Clear singular and plural formatting: Human readable labels now cleanly distinguish singular and plural counts for both rooms and nights.
- Defensive fallback handling: Same day itineraries avoid generating misleading zero quantity accommodation rows in PDF exports.
- Uncoerced unknowns preserved: Tolls remain preserved as explicit unknown lines with documented reasons.
- Preservation of historical evidence: Historical execution logs and live run artifacts in backend/scripts/ remain pristine.
- Comprehensive test coverage: Added robust unit tests verifying PDF stream contents across edge cases including same day, single room single night, and multi room multi night scenarios.

## Verification and Test Execution Evidence

All tests were executed against the modified files using the local environment:

1. Slice 5 unit test suite:
Command: pytest backend/tests/unit/test_trip_planner_proposal_slice5.py
Result: 35 passed in 1.61s.

2. PDF export fallback tests:
Command: pytest backend/tests/unit/test_trip_planner_proposal_slice5.py -k "pdf_export" -v
Result: 3 passed in 1.48s.

3. Complete trip planner test suite:
Command: pytest backend/tests/unit/test_trip_itinerary_api_slice6.py backend/tests/unit/test_trip_planner_llm_wiring_slice1.py backend/tests/unit/test_trip_planner_proposal_slice5.py backend/tests/unit/test_trip_planner_route_optimizer_slice4.py backend/tests/unit/test_trip_planner_weather_wiring_slice2.py
Result: 91 passed in 1.96s.

4. Historical live artifact verification:
Command: python inspection of backend/scripts/live_journey_result_mysuru_srirangapatna_mandya.json
Result: Confirmed 20 features consisting of 10 Point stops, 9 LineString legs, 1 Polygon road closure restriction, and top level bbox property [76.639391, 12.295324, 76.898968, 12.529111].

5. Repository status verification:
Command: git status -s
Result: Exactly 5 modified files, clean index, and no untracked file pollution.

## Final Review Verdict

Verdict: Approve

The follow ups for slice 0007 lodging budget accounting are complete, mathematically sound, clearly documented, and backed by comprehensive unit tests. The implementation adheres strictly to repository standards of simplicity and surgical precision.
