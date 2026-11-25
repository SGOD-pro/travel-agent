# Review, main, 2026-11-24

**Reviewed by**: Staff Software Engineer (author on claude 3.5 sonnet)
**Scope**: 7 files, uncommitted
**Verdict**: Approve

## Summary
The updated working tree changes resolve all previously identified blockers and major findings. The point to point probe cache is now cleanly scoped and reset per execution in `execute_task`, eliminating cross execution leaks and call cap exhaustion across tasks and tenants. Active road closure polygon exclusions are consistently generated and applied to fallback probes and verified during route hydration, multi stop destination cluster recovery correctly computes non zero return leg durations back to origin, the core trip planner graph contains no hardcoded test scenario cities, and strict mypy verification passes with zero errors across all affected files.

## Nits
- Resolved: `probe_cache` is passed explicitly through `_hydrate_leg_geometries_and_revalidate_schedule` and down into recursive `_solve_route_with_provider` calls when re solving after dropping window breached stops.

## Strengths
- Clean per execution probe cache scoping with explicit barrier checks against parent deadlines and active execution budget tracking.
- Precise integration of road closure polygon exclusions across both matrix probing and route hydration, preventing closed corridor traversal.
- Accurate composite duration and distance calculations for return legs in multi stop cluster recovery, ensuring realistic schedule calculations without teleportation.
- Complete removal of hardcoded scenario city names from the orchestrator graph node in favor of generic traveler preference and constraint inspection.
- Flawless strict typing adherence under mypy across service, contracts, graph, and unit test suites.
- Robust unit test coverage including regression tests for HTTP 400 error 154, matrix null cell handling, execution deadline boundaries, and multi stop cluster recovery.

## Test coverage
- All 11 unit tests in `tests/unit/test_valhalla_bounded_fallback.py` and `tests/unit/test_valhalla_bounded_fallback_regressions.py` execute and pass cleanly.
- Dedicated unit test `test_valhalla_bounded_fallback_multistop_cluster_recovery` provides full coverage for multi stop destination cluster matrix recovery and verifies non zero travel durations and distances for the return leg.
- Type checking passes with 0 errors across all 6 target source and test files under strict mypy configuration.
