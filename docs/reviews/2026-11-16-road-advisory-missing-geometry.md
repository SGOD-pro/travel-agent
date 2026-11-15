# Review: Advisory Missing Geometry Correction (2026-11-16)

**Reviewed by**: Senior Staff Engineer Reviewer
**Scope**: 2 files, commit af08e62
**Verdict**: Approve

## Summary

This review covers the advisory missing geometry correction for specification 0007 Slice 8 in commit af08e62. The change addresses spatial validation in [graph.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3608-L3662) by enforcing strict road geometry requirements for travel advisories and hazard notices. When road route geometry is unavailable or when notices lack spatial coordinates, the system preserves notices under UNKNOWN relevance and places the overall proposal into the fail closed ADVISORY_UNKNOWN status. The implementation cleanly distinguishes verified corridor matches from unverified mentions, prevents false safety claims, preserves observation provenance, and includes comprehensive regression tests in [test_claim_ingestion.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3420-L3670).

## Evaluation of Review Criteria

### 1. Strict Road Geometry Requirement
When selected road route geometry is absent or empty, the logic in [graph.py:3647-3662](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3647-L3662) assigns spatial relevance as UNKNOWN rather than WITHIN_CORRIDOR. Even if an advisory explicitly names a verified trip place such as an origin, destination, or intermediate stop, the orchestrator notes that spatial corridor relevance cannot be confirmed without road route geometry. The advisory item is collected in `unverified_advisories` with `uncertainty` set to UNKNOWN_RELEVANCE as seen in [graph.py:3716-3725](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3716-L3725), preserving its canonical source URL, observation identifier, and detailed explanatory reason.

### 2. Missing Coordinates With Available Geometry
When physical road route geometry exists but the advisory lacks coordinates or provides invalid coordinates, the logic in [graph.py:3627-3646](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3627-L3646) treats the spatial relevance as UNKNOWN. If the affected area text matches a trip place, the explanatory reason clearly records that coordinates are missing to tie the notice to the selected road route geometry. The system refuses to claim a confirmed route match, routing the notice into unverified advisories and preventing false certainty.

### 3. Preserving Relevant Warnings and Safety Boundary
When valid road route geometry is present and the advisory provides valid coordinates within the 100 kilometer corridor, [graph.py:3616-3621](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3616-L3621) computes the corridor distance and assigns spatial relevance as WITHIN_CORRIDOR. Authoritative notices evaluate to ACTIVE status with CONFIRMED_AUTHORITATIVE uncertainty and are included in `relevant_advisories`. This sets the overall proposal advisory status to ADVISORIES_PRESENT as verified in [graph.py:3781-3784](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3781-L3784). Crucially, the system maintains the fail closed invariant: absence of confirmed advisories never results in a SAFE assertion, but yields ADVISORY_UNKNOWN with explicit safety warnings at [graph.py:3804-3807](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3804-L3807).

### 4. Verification of Proposal Path Regressions
The test suite in [test_claim_ingestion.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3420-L3670) contains three targeted regression tests:
* Regression 12 at [test_claim_ingestion.py:3420-3494](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3420-L3494): Verifies that a matching place name without road route geometry yields UNKNOWN relevance, UNKNOWN_RELEVANCE uncertainty, zero relevant advisories, one unverified advisory with full provenance, and ADVISORY_UNKNOWN proposal status.
* Regression 13 at [test_claim_ingestion.py:3495-3580](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3495-L3580): Verifies that available road route geometry with missing affected area coordinates yields UNKNOWN relevance, UNKNOWN_RELEVANCE uncertainty, and ADVISORY_UNKNOWN proposal status.
* Regression 14 at [test_claim_ingestion.py:3581-3670](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3581-L3670): Verifies that valid road route geometry with matching coordinates within the corridor yields WITHIN_CORRIDOR relevance, CONFIRMED_AUTHORITATIVE uncertainty, ACTIVE status, route diagnostics annotations, and ADVISORIES_PRESENT proposal status.

### 5. Test Suite Verification
All verification suites executed cleanly on isolated PostgreSQL:
* Ruff check: 0 issues detected across `src` and `tests` (`cd backend && uv run ruff check src tests`)
* Mypy static typing: 0 issues across 11 key files (`cd backend && uv run mypy src/contracts/llm.py src/functions/api/routes/trips.py src/functions/evidence_collector/scrape.py src/functions/evidence_collector/service.py src/functions/orchestrator/evidence/query_planner.py src/functions/orchestrator/evidence/place_extraction.py src/functions/orchestrator/evidence/claim_ingestion.py src/functions/orchestrator/evidence/eligibility.py src/functions/orchestrator/places/candidate_service.py src/functions/orchestrator/graphs/trip_planner/graph.py src/functions/orchestrator/evidence/policy.py`)
* Claim ingestion integration: 49 passed in 3.70s (`cd backend && uv run pytest tests/integration/test_claim_ingestion.py -v`)
* Evidence runner life cycle: 5 passed in 21.49s (`cd backend && uv run pytest tests/integration/test_trip_planner_evidence_runner_lifecycle.py -v`)
* Connected evidence acceptance: 1 passed in 16.42s (`cd backend && RUN_CONNECTED_EVIDENCE_TESTS=1 uv run pytest -s tests/integration/test_trip_planner_evidence_connected.py`)

## Strengths
* Fail closed safety posture: Prevents assuming route safety or confirmed spatial proximity when route geometry or notice coordinates are missing.
* Complete provenance retention: Preserves observation identifiers, canonical URLs, publication authorities, and specific evaluation reasons even for unverified or unknown notices.
* Clean separation of concerns: Distinguishes geographic coordinate corridor checks from textual place name heuristics, avoiding conflation of text mentions with geometric proximity.
* Comprehensive regression coverage: Directly exercises the three branches with realistic state fixtures and asserts status, relevance, uncertainty, diagnostics, and geometry immutability.

## Test Coverage
The regression suite in [test_claim_ingestion.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3420-L3670) provides full branch coverage for the modified spatial evaluation branches in [graph.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3608-L3662). All test suites pass without regression.
