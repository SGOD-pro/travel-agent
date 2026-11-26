# Code Review: 0007 Must-Visit Bounded Place Lookup (`210e98278a1d2ccf58d974e8a9d782a22d124cd4`)

- **Date:** 2026-11-26
- **Spec:** `0007-trip-planner-e2e`
- **Base SHA:** `da09db8406940acf6c824f10fcba630798e2c48b`
- **Reviewed SHA:** `210e98278a1d2ccf58d974e8a9d782a22d124cd4`
- **Reviewer:** Fresh `self` subagent (`Model: pro`, `Role: Senior Code Reviewer`)
- **Verdict:** **APPROVE**

## Summary
Commit `210e98278a1d2ccf58d974e8a9d782a22d124cd4` resolves explicit traveler must-visits through bounded `google_maps` `place_results` lookup + `google` hours queries without any Ooty/Mysuru special-casing. All prioritization, ceiling enforcement, `skipped_research_areas` preservation, wrong-city/centroid/lookup-hours guards, and prior Blocker/Major review findings (spatial ambiguity preservation in `_is_same_place`, `rank_candidates_node`, and `dispatch_route_optimizer_node`; `discovered_places` not shadowing `evidence_proposals`; `d0_hours`/`destinations[1:]` priority vs optional `food`) are verified in code and passing tests.

## Findings
- **Blocker:** None
- **Major:** None
- **Minor:**
  1. `PlaceExtractionService.extract_candidates_from_observations` (`place_extraction.py:262-275`, `graph.py:577-579`): Step 2 text-mention URL merging runs in a single pass; if a `SCRAPLING_HTTP` page precedes `google_maps`, `PlaceProposal.source_urls` may omit that URL at extraction time (downstream `ingest_claims_from_observations` and `DeterministicEvidenceEligibilityGate` recover it order-independently).
  2. `dispatch_route_optimizer_node` (`graph.py:1624-1635`): `mv_locations` buckets coordinates by `round(float(...), 3)` (~111m grid), which could treat two records ~20m apart across a `.0005` boundary as distinct if not already merged by `CandidateRegistrationService` (`<= 100m`).

## Verification Run
- `ruff check` on all 5 modified files: passed (0 issues).
- `mypy --strict` on all 4 modified production files: passed (0 errors).
- `pytest` across 12 unit & PostgreSQL integration suites (`147 passed` in parent verification; `40 + 58 + 126 = 224` unit tests in reviewer verification).
