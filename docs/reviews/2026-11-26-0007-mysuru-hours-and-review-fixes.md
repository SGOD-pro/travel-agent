# Code Review: Spec 0007 Mysuru Palace Hours Scoping and Backend Acceptance Follow-Up

**Date:** 2026-11-26  
**Commit Reviewed:** `118ed1b843b6303f9c0936d80fc4c2a76d3859e2` (`fix(0007): resolve Mysuru Palace hours scoping and backend acceptance review findings`)  
**Parent Revision:** `eca25d9813a580568599544e9a6bddbe1642467b`  
**Files Reviewed:**
- `backend/src/functions/evidence_collector/service.py`
- `backend/src/functions/orchestrator/evidence/claim_ingestion.py`
- `backend/src/functions/orchestrator/evidence/eligibility.py`
- `backend/src/functions/orchestrator/evidence/hours_extraction.py`
- `backend/src/functions/orchestrator/evidence/place_extraction.py`
- `backend/tests/unit/test_source_backed_attractions_and_hours.py`
- `backend/tests/unit/test_source_backed_hidden_places.py`

## Verdict: Approve with nits

### Summary
All core requirements are cleanly implemented and verified:
1. **Mysuru Palace activity-scoped hours:** `_segment_text_by_activity` and `_extract_schedule_fields` keep general admission (`10:00` to `17:30`, `ticketing_opens="10:00"`, `ticketing_closes="17:30"`, `closed_days=[]`) isolated from Sunday illumination and the Sound and Light show (`closed_days=["Sunday"]`).
2. **Uncovered dates & partial weekends:** `resolve_hours_for_dates` tracks `unknown_visit_dates` separately so uncovered dates resolve to `CandidateHoursStatus.UNKNOWN` rather than closed or verified, while a weekend with one verified open day remains eligible on that open day with ticketing cutoffs preserved.
3. **Prior 5 review findings resolved:**
   - Shared `dining` + `hidden_places` URLs retain exact `purpose_destinations` associations and valid `active` claim support.
   - `NEEDS_BROWSER` escalations no longer consume static extraction capacity (`static_extractions_successful`) or suppress skip diagnostics.
   - Search failures, search budget exhaustion, and execution halts are distinguished from successful searches with no eligible URL.
   - Content-bearing URL query parameters survive deduplication via `normalize_url`.
   - Negated permit clauses (`no permit required`) qualify for public access while actual permit requirements remain excluded.

### Minor Nits (Non-Blocking)
1. Dead no-op `elif "click to book"` branch in `_segment_text_by_activity` (`hours_extraction.py:265-266`).
2. Activity split lookahead covers a subset of heading keywords, relying on sentence punctuation for others (`hours_extraction.py:238`).
3. Legacy `extract` fallback path sets `query_purpose`/`query_purposes` without `purpose`/`purposes` (`service.py:1074-1077`).
4. Direct `EligibilityGate` test assertion for `CandidateHoursStatus.UNKNOWN` on uncovered dates can be added alongside the `resolve_hours_for_dates` test.

### Verification Evidence
- `ruff check` across `src/` and both modified test files: Passed.
- `mypy` strict check across all 5 modified production modules and `test_source_backed_hidden_places.py`: Passed (0 errors).
- Targeted unit suites (6 files): 114 passed in 21.94s.
- Targeted PostgreSQL integration suites (`test_source_backed_hours_postgres.py`, `test_source_backed_hidden_places_postgres.py`, `test_evidence_collector_slice1.py`): 17 passed in 27.83s.
