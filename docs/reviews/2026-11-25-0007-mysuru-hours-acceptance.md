# Code Review — Feature 0007: Mysuru Opening-Hours Acceptance Run

**Date:** 2026-11-25  
**Verdict: APPROVE** (1 non-blocking note, 0 must-fix items)

---

## 1. `repository.py` — Skipped-Stop Unique-Order Assignment (lines 157–214)

**Correctness**

- The core logic is sound: active stops keep their scheduler-assigned `stop_order`, then skipped stops (from both `route.stops` and `route.dropped_stops`) are compacted into trailing unique integers, eliminating the previous `uq_route_stops_route_order` unique-constraint violation when multiple skipped stops all carried `stop_order=-1`.
- The `while next_skipped_order in used_orders` loop correctly handles the unlikely but possible case where active stops have non-contiguous or out-of-sequence orders (e.g., gaps introduced by earlier skips being discarded before this code ran).
- De-duplication via `seen_skipped_ids` correctly prevents the case where a stop appears in both `route.stops` (as `is_skipped=True`) and `route.dropped_stops`. Dedup key `s.stop_id or s.id` is consistent with the downstream `stop_id_map` population (`s.stop_id` checked first, `s.id` as fallback).

**Edge cases / failure modes**

- **`sid is None` path is not deduplicated.** When both `stop_id` and `id` are `None` on a skipped stop, the guard `if sid is not None` falls through without checking `seen_skipped_ids`, so a stop without any identifier will be inserted twice if it appears in both lists. In practice `ScheduledStopOutput` validators require at least one of these, so this is low risk, but defensively a `continue`-less path means duplicates if the schema ever relaxes.
- **`skip_reason[:64]` truncation is silent.** No log or warning is emitted when a reason is truncated. Not a defect, but worth noting for debugging.

**Minimal scope**

- Change is surgical: only the stop-order assignment block is new. The rest of the method body is unmodified. Good.

**Type safety / mypy**

- `ScheduledStopOutput` is imported at line 21 and used for the type annotation `ordered_stops: list[tuple[ScheduledStopOutput, int]]`. Import is consistent with its use; no phantom import, no missing usage.
- `getattr(s, "is_skipped", False)` is used twice in the new active/skipped partitioning — appropriate given `is_skipped` is optional on the contract type.

---

## 2. `graph.py` — `consume_route_result_node` `is_skipped` Guards

**Correctness**

- **Timestamp validation loop (line 2147–2170):** `if getattr(s, "is_skipped", False): continue` correctly skips skipped stops before the `planned_arrival_at / planned_departure_at is None` check. Without this guard, any PARTIAL route with skipped stops (which correctly have `None` timestamps) would have caused a spurious `ROUTING_UNAVAILABLE` failure — exactly the Mysuru acceptance regression.
- **Schedule construction loop (line 2192–2247):** The same guard prevents skipped stops from being included in `sched_stops`, preserving the existing contract that the schedule only contains active stops.
- Both guard sites use `getattr(s, "is_skipped", False)` matching the repository's pattern — consistent and safe.

**Minimal scope**

- Only two one-liner guards were added. No other lines changed. Scope is minimal.

**Edge cases**

- Skipped stops with `is_skipped=True` but non-None timestamps (edge case if the solver ever sets one) are still silently excluded from the schedule. This is correct behavior — the guard is on `is_skipped`, not on timestamp presence — so no regression risk.

---

## 3. `tests/integration/test_route_stops_skipped_persistence.py` — Full New File

**Regression coverage**

- The test directly reproduces the failure mode: three dropped stops all with `stop_order=-1`, which would have collided on the unique constraint. The test drives the full `save_route → commit → get_route_by_id → to_optimized_route()` round-trip on a real (or test) PostgreSQL session, so it is a genuine integration regression test.
- The final assertion `assert len({s.stop_order for s in loaded.stops}) == 5` is the exact uniqueness invariant that the fix enforces — this would have failed before the patch.
- Order and content of `dropped_stops` after readback is verified, confirming the round-trip preserves identity and skip reasons.

**Test quality**

- Fixture setup (`_seed_trip_and_execution`) is minimal and focused; no unrelated seeding.
- Two separate sessions (write + read) correctly test persistence boundary.
- No assertions on internal implementation details (no stop_order value assertions for the skipped stops beyond their count being unique) — good test hygiene.

**Minor observation**

- The test does not cover the **`sid is None` dedup gap** noted in §1. If a future schema change allows both `stop_id=None` and `id=None`, the existing test would still pass because all three skipped stops carry explicit UUIDs. Not a must-fix, but worth documenting.
- No `@pytest.mark.integration` marker or similar tagging — if the test suite separates integration from unit tests by marker, this file may accidentally run in the wrong context. Check whether the existing integration test runner picks it up by directory path alone (which appears to be the convention given placement).

---

## 4. `test_source_backed_attractions_and_hours.py` — `test_solver_enforces_last_entry_closing_ticketing_and_unmatched_must_visit`

**What the test covers**

- `dispatch_route_optimizer_node`: ticketing window (`ticketing_opens=10:00`) and last-entry cutoff (`last_entry=16:15 + 30m dwell = 16:45`) are correctly translated into UTC window constraints on the `RouteRequest` stop.
- Unmatched must-visit place (`"Unmatched Monument"` not in `eligible_stops`) is surfaced in `must_visit_conflicts`, not silently dropped.
- `is_must_visit` promotion guard: "Palace Cutoffs" (matched) stays `True`; "Late Entry Optional" stays `False`.
- `OrToolsRoutingSolver.solve()`: with synthetic travel times that make "Late Entry Optional" arrive at 16:40 IST (past the 16:15 last-entry cutoff), it is dropped as `TIME_WINDOW_EXCEEDED` while "Palace Cutoffs" (2h travel → 11:00 IST, within 10:00–16:45 window) succeeds.

**Correctness**

- UTC conversion arithmetic in the comment is correct: IST = UTC+5:30, so `10:00 IST → 04:30 UTC` and `16:45 IST → 11:15 UTC`. The assertions on `window_start` and `window_end` match.
- Travel time for "Late Entry Optional" is set to 27600 s (7h40m from 09:00 → arrives 16:40 IST). The last-entry window ends at 16:15 IST + 30m = 16:45 IST (11:15 UTC). 16:40 arrival would still fall inside 16:45 close — **the test comment says "after 16:15 last_entry" which is correct (last entry is a constraint on when you can START your visit), but should be explicit that 16:40 arrival > 16:15 last_entry deadline, hence the solver drops it.** The assertion matches intended behavior; the comment is technically accurate but slightly ambiguous about why 16:40 fails when 16:45 is the UTC window end.

**Test quality**

- The test is a combined integration of `dispatch_route_optimizer_node` + `OrToolsRoutingSolver.solve()` in a single unit test, which is heavier than ideal but pragmatic given the scenario. It correctly exercises the full contract.
- `idx_origin = 0` hardcodes the assumption that the origin is first in `req.stops`. If `dispatch_route_optimizer_node` ever reorders stops, this index assumption would silently misbehave. Using `next(i for i, s in enumerate(req.stops) if s.stop_type == StopType.ORIGIN)` would be more robust, but this is a minor style note.

---

## Must-Fix Items

*None.*

---

## Notes (non-blocking)

1. **`sid is None` dedup gap in `repository.py`:** When both `stop_id` and `id` are `None` on a skipped stop, the stop bypasses `seen_skipped_ids` and can be inserted twice if it appears in both `route.stops` and `dropped_stops`. Defend with a `None`-keyed set entry or an explicit continue if `sid is None` after dedup, depending on whether anonymous skipped stops are a valid case.
