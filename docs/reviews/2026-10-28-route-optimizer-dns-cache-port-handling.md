# Review, main, 2026-10-28

**Reviewed by**: Senior Reviewer (inline evaluation, author confirmed Gemini 3.8 Flash)
**Scope**: 6 files, uncommitted changes on main (base: `cfef45d3e195d232357e11fa14fb003eee37633a`)
**Verdict**: Approve

## Summary

This change resolves the DNS cache port handling defect identified at commit `cfef45d3e195d232357e11fa14fb003eee37633a` within slice 0006:

1. **Port preservation across cache lookups and formatting**:
   - In `backend/src/acceptance_database.py`, added `_clean_host` to normalize hostnames for lookup, `_normalize_port` to reliably convert integer ports, string ports, or service names into valid integer ports (defaulting to 0 when omitted or unresolvable), and `_format_addr_info_with_port` to reconstruct `socket.getaddrinfo` return structures preserving family, type, proto, canonname, and IP while setting `sockaddr` port strictly to the caller requested port for both IPv4 (2-tuple) and IPv6 (4-tuple) address formats.
   - Updated `_resilient_getaddrinfo` so all returned socket addresses (whether matched directly by `(host, port)` key, matched by normalized hostname, or resolved via fallback on network failure) pass through `_format_addr_info_with_port`.
   - Updated `resolve_host_ips` to resolve uncached hosts using port 0 and store results only under cache key `(cleaned, 0)`. Removed previous pollution that pre populated keys `(cleaned, 20266)` and `(cleaned, 5432)` with socket addresses embedding port 5432.

2. **Unified getaddrinfo wrappers**:
   - Replaced duplicate and diverging `_dns_cache` and `_resilient_getaddrinfo` definitions in `backend/src/acceptance_server.py`, `backend/src/acceptance_worker.py`, `backend/src/acceptance_seeder.py`, and `backend/tests/integration/conftest.py` with imports from `acceptance_database`.
   - Eliminated previous fallback loops that returned cached entries from other ports as is, which previously altered caller requested ports whenever an uncached port was requested on a known host.

3. **Controlled regression test coverage**:
   - Added unit test `test_dns_caching_preserves_requested_port_regression` in `backend/tests/unit/test_acceptance_isolation_safety.py`.
   - Uses a controlled in memory resolver with no live database or network access required.
   - Tests the original counterexample (uncached host resolved with `resolve_host_ips`, followed by lookups requesting port 20266, 5432, and 65432).
   - Tests integer ports, non default ports, string ports (`"20266"`, `"5432"`), port 0, port `None`, and service names (`"http"`).
   - Tests repeated alternating interleaved lookups across multiple ports to confirm cache entries do not contaminate one another.
   - Tests fallback behavior on `socket.gaierror` when resolver fails, confirming cached host identity returns the caller requested port.
   - Verifies database equivalence guard behavior: same host with same port evaluates to equivalent, while same host with different ports evaluates to non equivalent.
   - Preserves fail closed isolation checks.

## Strengths

- **Caller requested port invariant**: Reconstructing socket address structures dynamically from cached IP records guarantees that the caller port is always honored, regardless of what port was used during initial resolution.
- **Single source of truth**: Centralizing `_resilient_getaddrinfo` across server, worker, seeder, and test configuration prevents subtle drift and eliminates duplicate monkeypatching.
- **Controlled reproduction**: Pure in memory unit test allows rapid, deterministic verification without relying on network DNS or external database instances.

## Test coverage

- `backend/tests/unit/test_acceptance_isolation_safety.py`: 11 passed in 11.63s (including new `test_dns_caching_preserves_requested_port_regression`).
- `backend/tests/integration/test_checkpoint_protection.py`: 1 passed in 4.69s (`test_fresh_checkpointer_reads_persisted_state`).
- `backend/tests/integration/test_route_replan_concurrency_slice2.py`: 2 passed in 8.93s (`test_concurrent`).
- `frontend/e2e/route-optimization-acceptance.spec.ts`: 7 passed in 2.2m.
