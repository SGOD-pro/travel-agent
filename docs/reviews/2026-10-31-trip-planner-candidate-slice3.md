# Review, main, 2026-10-31

**Reviewed by**: Senior Code Reviewer
**Scope**: 8 files, uncommitted changes (Slice 3 of Feature 7)
**Verdict**: Approve

## Summary

This review assesses the implementation of Slice 3 of Feature 7 (Place candidate registration, spatial corridor validation, and deterministic claim eligibility gate). The implementation strictly satisfies acceptance criteria AC-1 and AC-3 with zero collateral churn and robust unit and live PostgreSQL integration test coverage. All 19 candidate unit tests pass in 1.38s, all 6 live PostgreSQL integration tests pass in 19.57s, 41 weather unit tests and 25 language model tests pass cleanly, mypy reports zero type issues across 16 source files, and ruff formatting and linting pass with zero warnings.

## Architectural compliance

1. **Editorial candidate boundary**: Generative language model proposals are strictly modeled as editorial recommendations labeled with uncertainty `EDITORIAL_DISCOVERY` and `is_candidate=True`. They are never treated as verified facts.
2. **Canonical registration and replay idempotency**: Eligible proposals within the 100km corridor are registered or resolved via [`CanonicalPlacesRepository`](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/places/repository.py) and [`PlaceResolutionService`](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/places/service.py). The service matches existing places via provider identifiers or spatial proximity within 100 meters combined with normalized name similarity (threshold >= 0.8), resolving to existing records and preventing duplicate rows on workflow replay.
3. **Corridor boundary and coordinate safety**: Candidate proposals are screened against geodesic travel corridor lines connecting the origin to destination stops. Coordinates are verified for finite numerical bounds, rejecting out of range values or non numeric inputs. Candidates farther than 100 kilometers from the corridor are discarded. Crucially, passing the spatial corridor check establishes only spatial eligibility; existence remains unverified until corroborating evidence is confirmed.
4. **Solver schedule isolation**: Candidates with unverified existence are filtered out of the route solver and preserved only as editorial recommendation cards in the trip state and synthesized proposal. Contradicted places (those refuted by negative observations or conflicting claims) are excluded entirely from both the route solver and recommendation cards.
5. **Assumed visiting windows**: Places with verified existence but unknown or missing operating hours enter the solver with an assumed daytime window (09:00 to 18:00) that is explicitly labeled in state and stop metadata as `HOURS_ASSUMED`. They are never presented as verified hours.
6. **Honest evidence preservation**: Evidence evaluation preserves stale, conflicting, unavailable, and missing states without loss. Stale hours claims do not qualify as verified hours and honestly fall back to `HOURS_ASSUMED` with diagnostic records.
7. **Tenant and execution isolation**: Evidence queries against PostgreSQL source observations and claims strictly filter by `execution_id` and tenant ownership, ensuring observations belonging to one tenant cannot verify candidates belonging to another.
8. **Hermetic testing**: Initial verification utilizes deterministic offline fixtures and isolated PostgreSQL sessions without making premature claims of live external model or cloud provider verification.

## Test coverage

* [`backend/tests/unit/test_candidate_corridor_and_eligibility.py`](file:///home/swyra/projects/travel-agent/backend/tests/unit/test_candidate_corridor_and_eligibility.py): 19 unit tests passed in 1.38s, covering coordinate bounds checking, Haversine distance math, cross track segment distance, 100km boundary filtering, registration idempotency, verified existence with verified hours, verified existence with assumed daytime hours (HOURS_ASSUMED), unverified candidate retention as editorial recommendations, contradicted place exclusion, stale claim preservation, cross tenant evidence isolation, and graph solver schedule boundary isolation.
* [`backend/tests/integration/test_candidate_registration_persistence.py`](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_candidate_registration_persistence.py): 6 integration tests passed in 19.57s on live PostgreSQL, validating canonical place creation with PostGIS geometry, replay deduplication, evidence eligibility with persisted observations, verified fresh hours claims, refuting evidence contradiction handling, and execution isolation.
* [`backend/tests/unit/test_weather_client.py`](file:///home/swyra/projects/travel-agent/backend/tests/unit/test_weather_client.py): 41 unit tests passed in 2.44s.
* [`backend/tests/unit/test_llm_client.py`](file:///home/swyra/projects/travel-agent/backend/tests/unit/test_llm_client.py): 25 unit tests passed in 1.75s.
* Strict mypy type checking verified with zero issues across 16 source files.
* Code style and formatting verified clean with ruff.

