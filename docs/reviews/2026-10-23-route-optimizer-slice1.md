# Review, 0005 route optimizer slice 1, 2026-10-23

**Reviewed by**: Senior Reviewer (inline mode, author model Gemini 3.8 Flash)
**Scope**: 5 files (contracts/routing.py, functions/optimizer/solver.py, functions/route_optimizer/solver.py, functions/route_optimizer/fixtures.py, tests/unit/test_route_optimizer_slice1.py)
**Verdict**: Approve

## Summary

This review assesses Slice 1 of specification 0005 (route optimizer and multimodal itinerary planning). The slice implements the offline core constraint solver and domain routing contracts while preserving backward compatibility for existing scheduler callers and money calculations.

The implementation strictly satisfies all core constraints and spec corrections:
1. Unreachable arcs are treated as strictly forbidden transitions by removing destination indices directly from routing decision variable domains (`NextVar.RemoveValue`), never merely applying large numerical penalties.
2. Honest solver status reporting distinguishes mathematical optimality (`ROUTING_OPTIMAL`), feasibility (`ROUTING_SUCCESS`), timeouts (`ROUTING_FAIL_TIMEOUT` returning `TIME_LIMIT` with `UNAVAILABLE` route status), and proven infeasibility (`INFEASIBLE`).
3. Dwell and wait accounting enforces service completion before venue closing (`arrival + wait + dwell <= window_end`), preventing unrealistic itineraries where travelers arrive minutes before a venue shuts.
4. Start and end semantics support round trips, designated destination termini, and open routes using zero cost dummy end nodes.
5. Mandatory stops enforce hard constraints while optional stops allow graceful dropping with concrete integer penalties (10,000,000) and explicit skip reasons.
6. Existing money, fuel profile, road segment, and budget calculator contracts in `contracts/routing.py` are preserved verbatim with full test coverage.

## Verification evidence

All verification commands executed cleanly with zero warnings or errors:
- `uv run pytest tests/unit/test_route_optimizer_slice1.py -v`: 15 passed in 11.16s.
- `uv run pytest tests/unit -k "route or optimizer or scheduler"`: 56 passed in 12.31s.
- `uv run pytest tests/unit`: 446 passed in 54.33s.
- `uv run ruff check src/contracts/routing.py src/functions/route_optimizer src/functions/optimizer/solver.py tests/unit/test_route_optimizer_slice1.py`: All checks passed.
- `uv run mypy src/contracts/routing.py src/functions/route_optimizer src/functions/optimizer/solver.py`: Success, zero issues found across 5 source files.

## Blockers

None.

## Major findings

None.

## Minor findings and notes

1. End node index resolution in OR-Tools:
   In Google OR-Tools `RoutingIndexManager`, when custom starts and ends are passed, `manager.NodeToIndex(end_node)` evaluates to negative one for end nodes. The implementation handles this cleanly by querying `routing.End(ends.index(node))` and skipping `NextVar` removal on end nodes. This prevents C++ level segmentation faults during arc removal and window constraints.

2. Pydantic default validation on `RoadSegment`:
   The pre-existing `RoadSegment` model requires explicit reasons when `toll_unknown` is True. The new `RouteLegOutput` contract separates routing leg geometry from financial line items, ensuring full compatibility without triggering schema validation surprises.

## Strengths

- Mathematical integrity: Distinguishes true optimality from local search feasibility, avoiding misleading claims in travel schedules.
- True topological exclusion: Unreachable arcs cannot be traversed under any objective weighting because they are deleted from variable domains.
- Dwell and closing consistency: Venue visits are bounded by closing times minus required dwell durations, rejecting plans that arrive without sufficient time for the planned visit.
- Clean backward compatibility: The legacy `ItineraryScheduler` delegates seamlessly to `OrToolsRoutingSolver` using local haversine matrices without network requests or breaking caller contracts.
- Strong test coverage: 15 dedicated unit tests exercise asymmetric directions, forbidden arcs, timeout semantics, mandatory conflicts, optional dropping, and round trip circuits.

For an independent second opinion from a different provider, switch your model with `/model` (or paste the diff into another assistant) and re-run /check review, no API keys needed.
