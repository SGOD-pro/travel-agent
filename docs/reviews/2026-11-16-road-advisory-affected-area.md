# Review: Affected Area Unknown Relevance Preservation (2026-11-16)

**Reviewed by**: Senior Staff Engineer Reviewer
**Scope**: 2 files, commit c06db10
**Verdict**: Approve

## Summary

This review assesses commit c06db10 on specification 0007 Slice 8, titled "feat(orchestrator): preserve unknown relevance for affected area without coordinates". The change refines spatial classification in [graph.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3634-L3664) so that an advisory with only an affected area name and no coordinates is never prematurely categorized as OUTSIDE_CORRIDOR merely because its place name differs from the origin, destination, or scheduled stops. Because intermediate towns or bypasses frequently lie along the driving route, notices without coordinates are safely preserved under UNKNOWN relevance and UNKNOWN_RELEVANCE uncertainty, keeping the proposal in the fail closed ADVISORY_UNKNOWN status. The OUTSIDE_CORRIDOR classification is strictly reserved for notices where physical route geometry is available and valid coordinates evaluate to exceeding the 100 kilometer corridor threshold. The implementation is verified through four targeted proposal path regression tests in [test_claim_ingestion.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3695-L4035).

## Evaluation of Review Criteria

### 1. Preservation of Unknown Relevance for Affected Area Without Coordinates
In [graph.py:3634-3647](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3634-L3647), when road route geometry is present but an advisory provides only an affected area text without coordinates, the system assigns spatial relevance as UNKNOWN. The prior heuristic that labeled an affected area as OUTSIDE_CORRIDOR if it failed to match the named trip places has been eliminated. The explanation explicitly records that intermediate corridor locations cannot be ruled out without coordinates. Similarly, when road route geometry is unavailable in [graph.py:3649-3664](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3649-L3664), spatial relevance remains UNKNOWN with an explanation that geometry is missing and coordinates are absent.

### 2. Complete Provenance and Fail Closed Safety
For every advisory classified as UNKNOWN relevance, the orchestrator preserves the canonical source URL, observation identifier, retrieval timestamp, affected area name, and explicit relevance reason in `unverified_advisories` with `uncertainty` set to UNKNOWN_RELEVANCE. The overall proposal advisory status evaluates to ADVISORY_UNKNOWN. It never defaults to safe, ensuring unverified road segments trigger traveler caution.

### 3. Strict Boundary for Outside Corridor Classification
In [graph.py:3610-3626](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3610-L3626), the OUTSIDE_CORRIDOR classification is applied exclusively when road corridor coordinates exist, the advisory provides valid coordinates, and the computed distance exceeds 100.0 kilometers (`dist_km > 100.0`). Textual mismatch alone is never permitted to dismiss an advisory.

### 4. Verification of Proposal Path Regressions
Four comprehensive integration tests in [test_claim_ingestion.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3695-L4035) verify all operational paths:
* Regression 15 at [test_claim_ingestion.py:3695-3781](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3695-L3781): Unnamed intermediate road location without coordinates (Kolaghat Bypass on Kolkata to Digha route). Confirms zero unrelated advisories, zero relevant advisories, one unverified advisory with UNKNOWN relevance, UNKNOWN_RELEVANCE uncertainty, complete provenance, and ADVISORY_UNKNOWN proposal status.
* Regression 16 at [test_claim_ingestion.py:3782-3857](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3782-L3857): Missing geometry with unmatched affected area name (Contai Coastal Sector). Confirms zero unrelated advisories, one unverified advisory with UNKNOWN relevance, UNKNOWN_RELEVANCE uncertainty, complete provenance, and ADVISORY_UNKNOWN proposal status.
* Regression 17 at [test_claim_ingestion.py:3858-3944](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3858-L3944): Genuinely outside location supported by coordinates (Rangpo, Sikkim > 500 km away). Confirms one unrelated advisory with OUTSIDE_CORRIDOR relevance, zero unverified advisories, and ADVISORY_UNKNOWN proposal status.
* Regression 18 at [test_claim_ingestion.py:3945-4035](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3945-L4035): Valid relevant location supported by route geometry and coordinates (NH-116B Nandakumar Junction on corridor). Confirms one relevant advisory with WITHIN_CORRIDOR relevance, CONFIRMED_AUTHORITATIVE uncertainty, ACTIVE status, and ADVISORIES_PRESENT proposal status.

### 5. Test Suite Verification
All verification suites executed cleanly on isolated PostgreSQL:
* Ruff check: 0 issues detected across `src` and `tests` (`cd backend && uv run ruff check src tests`)
* Mypy static typing: 0 issues across 11 key files (`cd backend && uv run mypy src/contracts/llm.py src/functions/api/routes/trips.py src/functions/evidence_collector/scrape.py src/functions/evidence_collector/service.py src/functions/orchestrator/evidence/query_planner.py src/functions/orchestrator/evidence/place_extraction.py src/functions/orchestrator/evidence/claim_ingestion.py src/functions/orchestrator/evidence/eligibility.py src/functions/orchestrator/places/candidate_service.py src/functions/orchestrator/graphs/trip_planner/graph.py src/functions/orchestrator/evidence/policy.py`)
* Claim ingestion integration: 53 passed in 3.53s (`cd backend && uv run pytest tests/integration/test_claim_ingestion.py -v`)
* Evidence runner life cycle: 5 passed in 20.65s (`cd backend && uv run pytest tests/integration/test_trip_planner_evidence_runner_lifecycle.py -v`)
* Connected evidence acceptance: 1 passed in 16.53s (`cd backend && RUN_CONNECTED_EVIDENCE_TESTS=1 uv run pytest -s tests/integration/test_trip_planner_evidence_connected.py`)

## Strengths
* Eliminates dangerous false negative filtering: Stops silently ignoring real road closures on intermediate highway segments whose place names do not appear in the brief.
* Fail closed safety posture: Sustains ADVISORY_UNKNOWN whenever spatial certainty cannot be established, adhering to traveler safety principles.
* Strict mathematical boundary: Grounds OUTSIDE_CORRIDOR determinations entirely in verified coordinate geometry distances rather than lexical matching assumptions.
* Robust test harness: Provides explicit assertions on observation identifiers, retrieval timestamps, canonical URLs, and advisory research buckets.

## Test Coverage
The test additions in [test_claim_ingestion.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_claim_ingestion.py#L3695-L4035) deliver complete branch coverage for all permutations of coordinate availability, geometry availability, and corridor distance in [graph.py](file:///home/swyra/projects/travel-agent/backend/src/functions/orchestrator/graphs/trip_planner/graph.py#L3610-L3664). All integration and static checks pass without regression.
