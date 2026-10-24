# Review, main, 2026-10-24

**Reviewed by**: Gemini 3.8 Flash (author on Gemini 3.8 Flash / Claude Sonnet 4.6)
**Scope**: 14 files, uncommitted
**Verdict**: Approve

## Summary

Slice 4 adds an external Valhalla HTTP client (`ValhallaClient`), road closure avoidance via `exclude_polygons`, post routing spatial revalidation with Shapely, and an offline `MockValhallaClient`. All previous minor issues and nits (unreachable fallback raise, raw float coordinate validation safety, static integration test coordinate flakiness and teardown, docstring precision, module constants, and circular import comments) have been cleanly resolved and verified. Code quality is high throughout, type checking and linting pass with zero errors, and all 51 tests across slices 1 through 4 pass cleanly.

## Strengths

- **Two phase closure safety gate**: Pre routing topological exclusion via Valhalla `exclude_polygons` paired with post routing Shapely intersection revalidation provides rigorous defense in depth against closed roads.
- **Robust retry and timeout control**: Bounded exponential backoff on 5xx errors and transport exceptions, combined with fast failing 4xx responses and a clean `while True` loop that guarantees mypy exhaustive returns without dead code.
- **Honest matrix contracts**: Preserves `None` values for unreachable matrix cells rather than coercing them to sentinel values, ensuring exact constraints for OR Tools.
- **Safe and isolated test suite**: High fidelity mock client with haversine computations, clean offline gate skipping for connected tests, and resilient PostGIS test teardowns.

## Test coverage

Test signal: `configured` (pytest with asyncio). Comprehensive coverage with 51 passing tests and 1 offline integration test skipped:
- Coordinate boundary validation, SSRF checks, and missing longitude rejection.
- Transport mode costing mappings and polyline6 shape decoding.
- Asymmetric matrix parsing with null cell handling and route leg maneuver extraction.
- Retry backoff on 5xx, immediate 4xx fast fail, and timeout escalation.
- Road closure polygon and buffered LineString extraction, MultiPolygon support, and fail closed handling for unsupported geometry.
- Temporal applicability filtering and spatial intersection detection.
- Offline mock client simulation and live database multimodal continuity persistence.
