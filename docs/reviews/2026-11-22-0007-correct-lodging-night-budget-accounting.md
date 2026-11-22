# Code Review: Correct Lodging Night Budget Accounting

* Review Date: 22 November 2026
* Scope: Uncommitted changes for slice 0007 correct lodging night budget accounting
* Target Files:
  * `backend/src/functions/orchestrator/graphs/trip_planner/graph.py`
  * `backend/src/functions/api/service.py`
  * `backend/tests/unit/test_trip_planner_proposal_slice5.py`
* Verdict: Approve

## Executive Summary

The uncommitted changes resolve a critical budget calculation defect where destination `stay_days` was multiplying hotel accommodation nights. Previously, each destination in the brief generated a separate accommodation line scaled by that destination `stay_days`. For a two day trip across two destinations, this fabricated two hotel nights even though the trip spanned only one calendar night.

The fix correctly calculates total lodging nights from local calendar dates using `(brief.end_date - brief.start_date).days`. When calendar nights equal zero, such as on same day trips, zero accommodation lines are created. When calendar nights are positive, exactly one accommodation line is generated with quantity equal to `brief.rooms * calendar_nights` at the benchmark rate of 3500 INR. For single destination trips, the destination name is preserved in the basis string. For multi destination trips, the code searches scheduled stops for an explicit accommodation stop; if none is found, it labels the location as unknown (`hotel_stay_unknown`) without fabricating a destination. The PDF export fallback in `service.py` is aligned with this calendar night logic. All existing and new tests pass cleanly, and remote LAYA remains disabled.

## Review Findings

### Blockers
None.

### Majors
None.

### Minors

1. Overnight location detection for multi night trips:
In multi destination trips spanning multiple nights, the code scans scheduled stops and takes the first stop matching type `ACCOMMODATION`, `HOTEL`, or `LODGING`. In Slice 5, where benchmark lodging is estimated as a single aggregate line item, this behavior is acceptable. Once full multi hotel selection is implemented in downstream slices, each distinct overnight stay will need its own itemized line item.

2. Basis string pluralization:
The basis string formatting `f"hotel_stay_{loc_str} ({brief.rooms} rooms, {calendar_nights} nights)"` uses plural nouns unconditionally, producing strings like `(1 rooms, 1 nights)`. This is purely cosmetic and matches current test expectations, but singular forms could be used in future cleanup.

3. PDF fallback row for same day trips:
In `service.py`, when the fallback branch is taken (when no orchestrator budget summary is provided), a table row for Accommodation is still appended with quantity "0". In normal execution, the orchestrator supplies `budget_summary["lines"]`, so this fallback is not hit. Omitting the row when quantity is zero in the fallback would be slightly cleaner.

### Strengths

* Strict adherence to calendar truth: Deriving lodging nights strictly from `(brief.end_date - brief.start_date).days` eliminates artificial budget inflation caused by intermediate waypoint `stay_days`.
* Correct zero nights handling: Same day itineraries cleanly omit accommodation lines rather than forcing dummy hotel charges or zero amount lines.
* Safe location attribution: Multi destination trips avoid guessing or fabricating an overnight location, correctly preserving `hotel_stay_unknown` when no scheduled accommodation stop exists.
* Non coercive unknown cost preservation: Highway tolls and unverified activity fees remain explicit unknown lines with documented reasons without being coerced to zero.
* High surgical precision: The change modifies only the necessary calculation block in `graph.py` and the fallback in `service.py`, avoiding unrelated refactors.
* Robust test suite: Four dedicated unit tests in `test_trip_planner_proposal_slice5.py` thoroughly cover two destination regression, same day trips, multi room scaling, and single destination location retention.

## Requirements Compliance Checklist

1. Lodging nights calculated from local calendar dates:
PASS. Calculated as `calendar_nights = max(0, (brief.end_date - brief.start_date).days)`.

2. Destination `stay_days` must not multiply hotel nights:
PASS. The destination loop in `graph.py` was replaced with a single calendar night computation.

3. 25 to 26 November trip with two destinations and one room yields one estimated night at 3500 INR:
PASS. Verified by `test_compute_budget_lodging_accounting_mysuru_regression`, producing exactly 1 accommodation line with quantity 1 and amount 3500.00 INR.

4. Same day trips yield zero lodging nights and zero accommodation lines:
PASS. Guarded by `if calendar_nights > 0:`. Verified by `test_compute_budget_lodging_accounting_same_day_trip`.

5. Multiple rooms scale quantity cleanly as `brief.rooms * calendar_nights`:
PASS. Quantity computed as `Decimal(str(brief.rooms * calendar_nights))`. Verified by `test_compute_budget_lodging_accounting_multiple_rooms` where 3 rooms for 2 nights produce quantity 6.

6. Unknown overnight location in multi destination trips remains unknown:
PASS. Multi destination trips without an explicit accommodation stop assign `loc_str = "unknown"`, creating `hotel_stay_unknown` without guessing.

7. Estimate labels and unknown cost handling preserved:
PASS. Accommodation lines retain `estimated=True` and `source_ref="benchmark_hotel_estimate"`. Unverified tolls remain explicit unknown lines with valid `unknown_reason` strings and uncoerced `None` amounts.

8. Slice strictly limited to lodging night accounting, LAYA kept off:
PASS. Only lodging accounting lines in `graph.py` and `service.py` were edited. LAYA endpoint remains blanked and disabled.

## Test Verification

All unit tests in `backend/tests/unit/test_trip_planner_proposal_slice5.py` were executed using Python 3.13 in `backend/.venv`:
* 32 passed in 1.60s.
* All 4 new regression tests passed.
* All 76 tests across the trip planner test suites passed cleanly.
