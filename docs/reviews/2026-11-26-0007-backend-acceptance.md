# Code Review: Spec 0007 Backend Acceptance and Hidden-Place Route Eligibility

**Date:** 2026-11-26  
**Commit Reviewed:** `eca25d9813a580568599544e9a6bddbe1642467b` (`feat(0007): wire hidden-place evidence through collection and route eligibility`)  
**Diff Base:** Parent `a4945c56124e6859c9ec0a0d966311b21f2e5601`  
**Inspected Files:**
- `backend/src/functions/evidence_collector/service.py`
- `backend/src/functions/orchestrator/evidence/claim_ingestion.py`
- `backend/src/functions/orchestrator/evidence/place_extraction.py`
- `backend/src/functions/orchestrator/graphs/trip_planner/graph.py`
- `backend/tests/unit/test_source_backed_hidden_places.py`
- `backend/tests/integration/test_source_backed_hidden_places_postgres.py`

## Verdict: Request changes

### Summary
The architecture connects collector `skipped_purposes`, multi-purpose deduplication (`purpose_destinations`), pre-registration hidden-place verification (`evaluate_candidates_node`), and claim-backed gating (`is_must_visit=False` optional stops versus `REFUTES` or unverified exclusion). All 17 hidden-place unit tests pass and the PostgreSQL workflow test passes cleanly with strict `mypy` and `ruff`. However, the review identifies two High-severity defects and several Medium-severity gaps that should be addressed before final acceptance.

### Blockers and High-Severity Findings

1. **Shared URLs with a non-hidden primary purpose drop valid hidden-place candidates**
   - **Locations:** `service.py:370-387`, `service.py:956-967`, `place_extraction.py:31-53`, `place_extraction.py:1038-1040`, `claim_ingestion.py:808-830`
   - **Problem:** When a URL is matched by multiple queries, `select_eligible_search_urls` keeps the first-seen purpose in `purpose` / `query_purpose` and aggregates all purposes into `purposes` / `purpose_destinations`. However, `PlaceExtractionService.evaluate_hidden_place_candidates` still inspects single-valued `extract_observation_purpose(art_dict)` and skips `if purpose in ("advisory", "dining", "lodging")`. Meanwhile, `claim_ingestion.py` checks multi-valued `extract_observation_purposes(...)`.
   - **Impact:** If a venue URL is discovered first under `dining` and second under `hidden_places`, `evaluate_hidden_place_candidates` skips it, preventing it from entering `verified_hidden_names`. In `claim_ingestion.py`, `hidden_places in extract_observation_purposes(...)` is true while `cand_norm_key not in verified_hidden_names`, which unconditionally wipes `supporting_obs = []`, discarding valid evidence and blocking optional stop eligibility.

2. **NEEDS_BROWSER pages misclassified as completed static fetches distort capacity and skip diagnostics**
   - **Locations:** `scrape.py:519-549`, `service.py:970-977`, `service.py:1049-1082`
   - **Problem:** `ScraplingExtractionAdapter.extract_page` returns placeholder observations with `extraction_method="NEEDS_BROWSER"` when JavaScript rendering is required. In `execute_collection_task`, `if obs_list or b_reqs:` treats this as `call_success = True` and increments `static_extractions_successful`.
   - **Impact:** A JavaScript-gated page prematurely consumes one of the two permitted static extraction slots, skipping subsequent purposes (such as `hidden_places`) with `fetch capacity exhausted (2-page extraction cap reached)` before two real pages have been parsed. Furthermore, `purpose_success_counts` increments, suppressing skip diagnostics even though no static text was obtained.

3. **Search failures, search budget exhaustion, and loop interruptions mislabeled in skipped_purposes**
   - **Locations:** `service.py:724-764`, `service.py:811-835`, `service.py:867-874`, `service.py:912-939`
   - **Problem:** When search queries fail due to missing keys, timeouts, provider errors, or loop breaks on budget exhaustion or cancellation, no observations are produced. The subsequent loop unconditionally records `Skipped {req_p} fetch: no eligible URL found in search results.`, masking upstream provider and budget failures.

### Medium-Severity Findings

1. **Over-aggressive query-string stripping merges distinct query-parameterized URLs**
   - `service.py:375` strips query parameters with `url.split("?")[0]` rather than calling `normalize_url(url)`, collapsing distinct pages such as `portal.example/place?id=1` and `portal.example/place?id=2`.
2. **Incomplete error and unavailable filtering in evaluate_hidden_place_candidates**
   - `place_extraction.py:1042-1044` only skips `status_val in ("UNAVAILABLE", "BLOCKED", "ERROR", "FAILED")`, omitting `TIMED_OUT`, `NOT_FOUND`, `unavailable_reason`, `browser_reason`, and `[DYNAMIC_PAGE: JAVASCRIPT_REQUIRED]`.
3. **Unanchored substring checks on restricted and disqualifying markers create false REFUTES claims**
   - In `place_extraction.py:1120-1129`, raw substring matching triggers on negated phrases like `no permit required`, while `disqualifying_access_words` checks for `not ` and `no ` as substrings, matching words like `cannot` or `piano`.
4. **Exact normalized name lookup and missing excluded_names in claim_ingestion**
   - `claim_ingestion.py:724` omits `excluded_names` passed by `graph.py`, and checks `cand_norm_key not in verified_hidden_names` by exact string match, missing candidates extracted via spatial merging or alias resolution.

### Verification Summary
- `ruff check`: Passed on all target files.
- `mypy --strict`: Passed on all target files.
- `test_source_backed_hidden_places.py`: 17 passed.
- `test_source_backed_hidden_places_postgres.py`: 1 passed.
- Regression sweep: 186 passed out of 190. (4 failures in `test_claim_ingestion.py` trace to hardcoded past timestamps `2026-11-23` evaluated against current wall clock `2026-11-26`, unrelated to this commit).
