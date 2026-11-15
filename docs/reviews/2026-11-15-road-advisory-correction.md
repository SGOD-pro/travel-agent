# Review, main, 2026-11-15

Reviewed by: Senior Code Reviewer
Scope: 5 files, commit 1d7f0fb versus 2957e20
Verdict: Approve

## Summary

This review evaluates commit 1d7f0fb on branch main. The commit implements the 0007 road hazard and travel advisory corrections across authority validation, temporal freshness, and dynamic route relevance. The implementation strictly adheres to fail closed principles, eliminating substring authority matching, untrusted observation flags, and hardcoded route coordinates. All 41 integration tests and connected acceptance tests pass cleanly.

## Key Changes and Verification

### 1. Authority Validation and Domain Whitelist Policy

In `backend/src/functions/orchestrator/evidence/policy.py` and `claim_ingestion.py`:
* The helper `is_verified_authority_domain` parses URLs using `urllib.parse.urlparse`, validating actual hostnames against `OFFICIAL_AUTHORITY_DOMAINS` and trusted `.gov.in` and `.nic.in` apex domains.
* Substring matches, embedded query parameters, user credentials, and URL path tricks are completely rejected.
* Untrusted observation metadata flags (`art_ref.get("is_authoritative")`) from scrapers or search feeds are ignored.
* Search snippets extracted via `SERPAPI_SEARCH`, `SEARCH_SNIPPET`, or `SEARCH_RESULT` are demoted to `UNVERIFIED_SEARCH`, requiring scraped content to confirm authoritative status.

### 2. Time and Freshness Validation

In `policy.py`, `claim_ingestion.py`, and `graph.py`:
* The function `safe_parse_datetime` handles ISO format strings, dates, datetimes, and invalid inputs without raising unhandled exceptions.
* Advisories scheduled after the trip window evaluates to `FUTURE` status and `FUTURE_ADVISORY` uncertainty.
* Advisories concluding before the trip begins evaluate to `EXPIRED`.
* Advisories lacking start and end timestamps evaluate to `UNDATED` with `UNVERIFIED_UNDATED` uncertainty.
* Notices older than 24 hours without an end date evaluate to `STALE`.
* Undated, stale, future, and malformed notices are quarantined to `unverified_advisories` rather than confirmed active warnings.
* Absence of warnings never evaluates to safe conditions, consistently reporting `ADVISORY_UNKNOWN`.

### 3. Dynamic Route and Corridor Relevance

In `graph.py`:
* Hardcoded references to Kolkata, Digha, and NH-116B are completely removed from `_build_advisory_research`.
* Corridor coordinates are constructed dynamically from `origin_lat`/`origin_lng`, destination coordinates, and scheduled stop coordinates via `CoordinatePoint`.
* Physical distances are computed using `compute_corridor_distance_km` with a 100 kilometer corridor buffer.
* When geographic coordinates are absent, matching falls back to verified trip place names (`origin_name`, destination names, stop names).
* Insufficient location evidence evaluates to `LOCATION_EVIDENCE_INSUFFICIENT`, mapping to `relevance: UNKNOWN` and `uncertainty: UNKNOWN_RELEVANCE` inside `unverified_advisories`.

### 4. Conflict Resolution Bug Fix

In `graph.py`:
* The accidental variable overwrite where `reason` was overwritten unconditionally by the conflict notice has been resolved.
* Status correctly checks `conflicting_advisories` first, then active `relevant_advisories`, then `expired_advisories`, `unverified_advisories`, and `unrelated_advisories`.

### 5. Focused Regression Suite

In `backend/tests/integration/test_claim_ingestion.py`, six comprehensive integration tests verify:
1. `test_advisory_research_spoofed_authority_url_rejected_as_unverified`: Spoofed IMD domains fail validation and stay unverified.
2. `test_advisory_research_untrusted_observation_flags_ignored`: Personal blog posts asserting authoritative flags are rejected.
3. `test_advisory_research_future_warning_excluded_from_active`: Future roadworks scheduled after trip completion are excluded from active alerts.
4. `test_advisory_research_stale_and_undated_warnings_not_confirmed_active`: Stale and undated notices from official authorities are quarantined to unverified advisories.
5. `test_advisory_research_unrelated_routes_and_insufficient_location_evidence`: Remote highways evaluate to `OUTSIDE_CORRIDOR` and generic notices evaluate to `UNKNOWN_RELEVANCE`.
6. `test_advisory_research_valid_authoritative_warning_confirmed_active`: Valid scraped police notices within trip dates and corridor evaluate to active confirmed warnings with complete provenance.

### 6. Connected Acceptance Test Strengthening

In `backend/tests/integration/test_trip_planner_evidence_connected.py`:
* Strengthened assertions guarantee that any advisory marked authoritative or confirmed active must stem from a validated authority domain.
* Proves that raw search snippets are never marked as confirmed authoritative warnings.
* Proves that when no active corridor hazard exists, advisory research reports honest `ADVISORY_UNKNOWN` without declaring the route safe.

## Boundary Conditions and Observations

1. Travel start date omitted in partial brief:
   When a trip brief omits `start_date`, comparison of `parsed_end.date() < trip_start_date` cannot execute. In this scenario, staleness detection provides the safety boundary for notices lacking an end date. For notices with an explicit past end date when no trip start date is given, comparing against the current UTC date as a fallback would provide additional defensive depth.
2. Degenerate empty brief fallback:
   In `graph.py` lines 3549, if `verified_trip_places` is empty, spatial relevance falls back to `WITHIN_CORRIDOR`. When a brief contains neither origin nor destination, defaulting to `LOCATION_EVIDENCE_INSUFFICIENT` would be even more conservative.

## Strengths

1. Strict host parsing prevents URL injection and domain spoofing attacks.
2. Dynamic corridor coordinates replace prior hardcoded place names.
3. Transparent status reporting adheres to fail closed safety principles.
4. Clean test suite with 41 integration tests passing on isolated PostgreSQL.

## Verification Evidence

1. Integration tests:
   Command: `cd backend && uv run pytest tests/integration/test_claim_ingestion.py -v`
   Result: 41 passed in 3.43s.
2. Connected acceptance test:
   Command: `cd backend && RUN_CONNECTED_EVIDENCE_TESTS=1 TEST_DATABASE_URL=$ACCEPTANCE_DATABASE_URL uv run pytest tests/integration/test_trip_planner_evidence_connected.py -v -s`
   Result: 1 passed in 19.04s on isolated PostgreSQL.
3. Runner lifecycle test suite:
   Command: `cd backend && TEST_DATABASE_URL=$ACCEPTANCE_DATABASE_URL uv run pytest tests/integration/test_trip_planner_evidence_runner_lifecycle.py -v`
   Result: 5 passed in 15.61s on isolated PostgreSQL.
4. Unit tests:
   Command: `cd backend && uv run pytest tests/unit/test_query_planner.py tests/unit/test_place_extraction.py -v`
   Result: 9 passed in 1.53s.
5. Code style and linting:
   Command: `cd backend && uv run ruff check src tests`
   Result: All checks passed with zero errors.
6. Type checking:
   Command: `cd backend && uv run mypy src/contracts/llm.py src/functions/api/routes/trips.py src/functions/evidence_collector/scrape.py src/functions/evidence_collector/service.py src/functions/orchestrator/evidence/query_planner.py src/functions/orchestrator/evidence/place_extraction.py src/functions/orchestrator/evidence/claim_ingestion.py src/functions/orchestrator/evidence/eligibility.py src/functions/orchestrator/places/candidate_service.py src/functions/orchestrator/graphs/trip_planner/graph.py src/functions/orchestrator/evidence/policy.py`
   Result: Success: no issues found in 11 source files.
