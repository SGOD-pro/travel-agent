# Review, uncommitted, 2026-11-20

**Reviewed by**: Staff Reviewer (subagent) (author on Gemini)
**Scope**: 9 files, uncommitted
**Verdict**: Approve with nits

## Summary
This change enforces credential safe failure logging in the SerpAPI search adapter and establishes deterministic search query routing across the trip planner workflow. Catch blocks and logging filters now sanitize exception arguments, request URLs, and chained exception causes to prevent raw credential exposure in log records and tracebacks. Explicit query purpose strictly governs engine selection, removing keyword inference as authority, while Bedrock responses parse structured purpose metadata with backwards compatible legacy string support. All 60 unit, integration, and start contract tests pass cleanly with zero linting or type check warnings.

## Minor
### 🟡 Chained exception cycle causes recursion error during sanitization, `backend/src/functions/evidence_collector/serpapi_adapter.py:114`
**Problem**: In `_sanitize_exception`, recursive traversal into `__cause__` and `__context__` does not maintain a visited set of exception identifiers. When an exception chain contains a cycle (for instance when an exception is caught and raised again across asynchronous tasks or retry loops), `_sanitize_exception` triggers a recursion error.
**Why it matters**: If a recursion error occurs during error handling or logging filter evaluation, the original error context is dropped, exception classification is lost, and the logging pipeline crashes.
**Suggested fix**: Track visited exception object identifiers using a set in `_sanitize_exception` and return early if an identifier was already visited.

### 🟡 Dictionary origin lookup fails hasattr check during query validation, `backend/src/functions/orchestrator/graphs/trip_planner/graph.py:5115`
**Problem**: In `route_evidence_decision_node`, extracting `origin_name_for_val` checks `hasattr(brief, "origin_name")`. Because `brief` in `TripPlanningState` is almost always a dictionary, `hasattr` evaluates to false, resulting in an empty string rather than the origin name.
**Why it matters**: While `QueryPlanningService.structure_and_validate_queries` does not currently consume the origin parameter for existing planned queries, passing an empty string is inconsistent with earlier dictionary lookups in `graph.py` and risks losing origin context if query planner defaults are invoked.
**Suggested fix**: Check `brief.get("origin_name", "")` when `brief` is a dictionary, falling back to `getattr(brief, "origin_name", "")`.

## Nits
- ⚪ `backend/src/functions/evidence_collector/service.py:640`, in `EvidenceCollectorService`, `q_extra` is passed to `active_adapter.search` before `q_purpose` normalizes `q_engine`; normalizing `q_extra["engine"]` prior to calling the adapter would ensure non SerpAPI adapters also receive the normalized engine.
- ⚪ `backend/src/functions/orchestrator/llm/adapters/bedrock.py:233`, in `BedrockLlmClient.generate_search_queries`, checking `parsed.get("search_queries") or parsed.get("queries")` would add resilience if the model outputs query items under the alternative key.

## Strengths
- Robust exception sanitization in `SerpApiAdapter` redacts sensitive credentials from exception messages, `httpx.URL` request properties, and chained causes before invoking `logger.exception`, preventing credential leaks in logs and formatted tracebacks while preserving error classification.
- Strict query purpose governance deterministically binds road advisories to web search (`google`) and venue discovery categories (`attractions`, `dining`, `lodging`) to place search (`google_maps`), eliminating keyword inference as purpose authority.
- `BedrockLlmClient` now generates and parses structured JSON items with explicit purpose and destination metadata while maintaining safe, backwards compatible fallback handling for legacy plain string lists.
- Reused planned queries in `trip_planner` graph execution undergo mandatory structure validation and destination rebalancing before evidence task dispatch, guaranteeing adherence to query limits and budget constraints.
- Comprehensive unit test coverage thoroughly exercises credential redaction across transport errors, timeouts, and unexpected socket failures, as well as purpose engine binding under conflicting metadata.

## Test coverage
Test coverage is comprehensive across all touched components. The test suite includes 60 passing tests across unit and integration suites with zero regressions. In `backend/tests/unit/test_serpapi_adapter.py`, new tests verify credential redaction in logs and tracebacks across transport errors, timeouts, and unexpected exceptions, as well as adapter engine normalization. In `backend/tests/unit/test_destination_research_coverage.py`, new tests verify query policy normalization with conflicting metadata, keyword inference removal with misleading venue and advisory names, legacy plain string response handling, Bedrock structured response parsing, and graph validation of reused queries prior to evidence dispatch. Linting with ruff is clean and strict mypy verification succeeds across all 14 files without errors.
