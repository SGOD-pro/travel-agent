# Review, main (uncommitted), 2026-11-20

**Reviewed by**: Gemini (independent review subagent) (author on Gemini)
**Scope**: 15 files, uncommitted
**Verdict**: Approve with nits

## Summary
This review evaluates the bounded destination research coverage and search routing implementation, including the recent dynamic derivation of covered and uncovered destinations under constrained query limits and caller injection rejection across core SerpAPI search parameters. Credential protection operates reliably across HTTP client logging, error messages, and parameter sanitization. Query planning enforces destination balancing and isolates road travel advisories from venue discovery within the strict budget ceiling. Thorough unit and integration tests validate the complete workflow with clean linting and zero type check issues.

## Nits
- ⚪ `backend/src/functions/evidence_collector/serpapi_adapter.py:57`, in `_redact_arg`, dictionary sanitization recurses into values without inspecting dictionary keys; checking whether key names match known secret names such as `api_key` or `token` before inspecting values would redact bare secret tokens in dictionary arguments.
- ⚪ `backend/src/functions/evidence_collector/serpapi_adapter.py:452`, in `SerpApiAdapter.search`, logging with `logger.exception` on unexpected errors outputs the chained exception traceback; registering `CredentialRedactionFilter` on the adapter logger or sanitizing traceback strings would add defense in depth against exception text leaking query parameters.

## Strengths
- Dynamic derivation of covered and uncovered destinations in `plan_destination_queries` ensures mathematical consistency across constrained query limits and prevents misreporting coverage when query slots are reduced.
- Credential protection is comprehensive: `CredentialRedactionFilter` masks `api_key` in log record messages, parameterized URL objects, and dictionary arguments, while parameter whitelisting and dictionary precedence prevent credential or query override via caller extra parameters.
- Query planning balances destination discovery and corridor travel advisories cleanly within the strict four query ceiling, isolating road advisories to web search and venue exploration to place search.
- Place extraction and claim ingestion isolate advisory observations from physical place candidates, preventing road condition alerts from polluting sightseeing recommendations.
- Test coverage is exemplary, with fifteen unit tests, two PostgreSQL backed integration tests, and start contract tests verifying serialized task dispatch, database persistence, and budget accounting.

## Test coverage
Test coverage is comprehensive across all touched components. The unit test suite in `backend/tests/unit/test_destination_research_coverage.py` verifies multi destination balancing, single destination coverage across all four research categories, query budget ceilings, credential redaction across string messages and parameterized arguments, place extraction isolation, zero destination briefs, query eviction rebalancing, and constrained budget coverage derivation. The integration suite in `backend/tests/integration/test_destination_research_dispatch.py` confirms end to end dispatch, controlled SerpAPI provider responses, observation metadata persistence in PostgreSQL, and worker budget accounting. In `backend/tests/unit/test_serpapi_adapter.py`, `test_extra_params_cannot_override_api_key_or_core_params` confirms that caller extra parameters cannot replace adapter credentials or core query parameters. All 42 unit and integration tests pass cleanly with zero linting or type check warnings.
