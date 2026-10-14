# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Core execution engine components for asynchronous travel planning workflows (see spec 0001). This includes PostgreSQL persistence migrations for executions, tasks, outbox events, worker results, completion receipts, job events, and API idempotency records.
- API job acceptance with idempotency reservations, owner isolated job timeline polling, and job cancellation endpoints (see spec 0001).
- Worker lease fencing service with optimistic concurrency controls, preventing stale worker claims and duplicate completions on live PostgreSQL (see spec 0001).
- Transactional outbox relay service that claims due events with skip locked semantics and publishes events using bounded batch limits (see spec 0001).
- Scheduled task recovery service that reclaims expired worker leases, applies exponential backoff, and marks exhausted attempts as failed (see spec 0001).
- Completion reconciliation service that deduplicates completion receipts, serializes execution row updates, and merges outputs into LangGraph checkpoint state (see spec 0001).
- Graph runner consumers for `EXECUTION_START` and `EXECUTION_RESUME` events, driving durable LangGraph workflow execution and continuation (see spec 0001).
- SQS consumer handlers and continuous background daemon supervision with crash detection, exponential backoff, and concurrent partial batch processing (see spec 0001).
- Worker task queue consumer and completion reconciliation consumer with explicit executor registry dispatching (see spec 0001).
- Transaction bound LangGraph checkpointer with atomic rollback of buffered writes when enclosing database transactions abort (see spec 0001).
- Connected end to end execution trace verifying the complete flow from HTTP acceptance through worker execution, reconciliation, graph continuation, and terminal status polling (see spec 0001). This connected test uses an injected test executor to verify execution engine wiring rather than real evidence collection.
- AWS SAM template defining SQS queues, dead letter queues with redrive policies, partial batch failure reporting, and EventBridge schedules (see spec 0001). Live deployed AWS behavior remains deferred until cloud rollout.
- Full test suite expanded to 90 passing test cases covering all isolation boundaries, regression scenarios, crash recovery loops, and checkpoint fencing on live PostgreSQL (see spec 0001).
- Evidence collection and observation persistence for web search and permitted page extraction (see spec 0002). This includes PostgreSQL migrations for immutable `source_observations` with canonical URL normalization, tracking parameter stripping, and SHA256 content hashing.
- Canonical place resolution and verifiable claim persistence with PostGIS spatial models (see spec 0002). This adds `canonical_places`, `claims`, and `claim_evidence` tables with GiST indexing and spatial distance band queries.
- Live web search integration through SerpAPI with query throttling, latency tracking, minor currency unit spend tracking, and credential masking in logs and error details (see spec 0002).
- Web page extraction using Scrapling CSS selectors for lodging rates, transit schedules, destination attractions, and festival advisories (see spec 0002). Unparseable rates are preserved as unknown rather than coerced to zero.
- Server side request forgery defense with pre connection IP address validation, carrier grade NAT blocking, IPv4 mapped IPv6 normalization, redirect hop verification, and payload size bounds (see spec 0002).
- Syndication and non independent corroboration detection linking duplicate articles sharing identical content hashes across different web addresses (see spec 0002).
- Durable budget reservation and reconciliation across concurrent tasks, enforcing call limits and financial caps in PostgreSQL before external network calls (see spec 0002).
- Dynamic page detection emitting browser requests when client side JavaScript or anti bot checks are required, avoiding unnecessary browser automation (see spec 0002).
- Test coverage expanded with 181 unit and integration tests for spec 0002, bringing total repository coverage to 287 passing tests on live PostgreSQL.
- Canonical place spatial discovery and candidate resolution service (see spec 0002). Adds PostGIS spatial annular band filtering using ST_DWithin for 2000 to 3000 meters and 5000 to 7000 meters, pure Python token similarity matching, and non destructive entity coexistence preserving ambiguous places across localities without lossy merges.
- Deterministic Policy Engine for verifiable claims and road advisories (see spec 0002). Enforces deterministic state transitions (VERIFIED, PARTIALLY_VERIFIED, CONFLICTED, UNVERIFIED) preventing generative model proposals from directly mutating verification truth, requires authoritative government or police domain evidence for critical road closures, preserves contradictory assertions side by side as conflicted claims, and transitions expired assertions to stale without altering historical records.
- Evidence collector AWS SAM serverless packaging and worker registration (see spec 0002). Defines independent AWS Lambda packaging with staged build sizes under 50 MB compressed and 250 MB uncompressed, registers the worker handler in the 0001 executor registry under ("EVIDENCE", "collect"), and connects end to end collection, spatial resolution, and policy evaluation on live PostgreSQL.

### Removed
- Unreferenced MapLibre GL wrapper, unused UI component scaffolds, unregistered middleware, and orphaned Redis cache adapters totaling over 3,400 lines of dead code.
- Four unneeded external dependencies (maplibre-gl, cn, upstash-redis, and redis), keeping the staging and container footprint lean and minimal.

