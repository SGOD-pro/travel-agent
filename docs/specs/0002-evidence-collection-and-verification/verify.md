# Verify: Evidence Collection and Verification · spec 0002

Steps derived from spec 0002 acceptance criteria and specced surfaces. `/check verify` runs these; `/test` locks the durable regression tests.

## Acceptance criteria

- [ ] AC-1: SerpAPI search adapter returns normalized organic search and place results with latency and minor unit spend tracking.
- [ ] AC-2: Scrapling extraction adapter parses lodging and transit listings safely using CSS selectors without zero coercing unparseable prices.
- [x] AC-3: Source pages and searches persist immutable `source_observations` records with SHA256 content hashes, canonical URLs, and excerpts.
- [ ] AC-4: Copied or syndicated articles sharing identical content hashes are linked but flagged as non independent corroboration.
- [ ] AC-5: Canonical places persist in `canonical_places` with reviewed names, locality, PostGIS `geometry(Point, 4326)`, and GiST indexing.
- [ ] AC-6: Spatial queries filter nearby places across configurable annular bands (2000 to 3000 meters and 5000 to 7000 meters) using `ST_DWithin`.
- [ ] AC-7: Candidate place matching resolves observations against canonical places within 100 meters and name similarity without destructive entity merging.
- [ ] AC-8: Extracted claims persist in `claims` linked to observations via `claim_evidence` junction table with typed values and geographic scope.
- [ ] AC-9: Deterministic Policy Engine evaluates claim verification states; generative model proposals cannot directly transition a claim to `VERIFIED`.
- [ ] AC-10: Critical road closures and festival advisories require authoritative government or police domain evidence to reach `VERIFIED`.
- [ ] AC-11: Contradictory assertions for the same subject and category are preserved side by side as `CONFLICTED` claims cross referencing each other.
- [ ] AC-12: Claim freshness is evaluated against category policies, transitioning expired claims to `STALE` without altering historical records.
- [ ] AC-13: Dynamic pages requiring JavaScript emit `browser_requests` without attempting headless browser execution.
- [ ] AC-14: Metered tasks stop dispatching external calls when tool call or cost budgets are exhausted, returning partial results.
- [x] AC-15: Worker handler registers in 0001 `EXECUTOR_REGISTRY` under `("EVIDENCE", "collect")` and commits results through worker fencing.
- [x] AC-16: Local tests run entirely offline using canned fixture adapters without requiring network access or `SERPAPI_API_KEY`.
- [ ] AC-17: Evidence collector packages independently via SAM `template.yaml` within AWS Lambda compressed and uncompressed size limits.

## Specced surfaces

- [x] Table `source_observations` in PostgreSQL with content hash and canonical URL indexes.
- [ ] Table `canonical_places` in PostgreSQL with PostGIS `geometry(Point, 4326)` and GiST spatial index.
- [ ] Table `claims` in PostgreSQL with subject, category, verification, and freshness indexes.
- [ ] Table `claim_evidence` junction table in PostgreSQL with composite primary key.
- [ ] Adapter `SerpApiAdapter` in `backend/src/functions/evidence_collector/serpapi_adapter.py`.
- [x] Adapter `ScraplingExtractionAdapter` in `backend/src/functions/evidence_collector/scrape.py`.
- [x] Adapter `FixtureSearchAdapter` and `FixtureWebExtractionAdapter` in `backend/src/functions/evidence_collector/fixtures.py`.
- [ ] Repository `CanonicalPlacesRepository` in `backend/src/functions/orchestrator/places/repository.py`.
- [ ] Repository `ClaimsRepository` in `backend/src/functions/orchestrator/evidence/repository.py`.
- [ ] Policy `ClaimVerificationPolicy` in `backend/src/functions/orchestrator/evidence/policy.py`.
- [x] Worker service `EvidenceCollectorService` in `backend/src/functions/evidence_collector/service.py`.
- [ ] Registration `EXECUTOR_REGISTRY[("EVIDENCE", "collect")]` in `backend/src/functions/orchestrator/execution/executor_registry.py`.
- [ ] Template `backend/src/functions/evidence_collector/template.yaml` validating under `sam validate`.

## Verification evidence gates

### 1. Fixture evidence gate (Local and CI offline verification)
All core functional logic must be verified deterministically without external network calls or paid API keys:
- Execute unit and integration tests using canned HTTP responses and HTML fixtures.
- Test Scrapling extraction against recorded MakeMyTrip and RedBus sample HTML pages.
- Test content hashing, duplicate syndication detection, and canonical URL normalization.
- Test PostGIS spatial queries, annular band filtering, and GiST indexing on local PostgreSQL container.
- Test deterministic verification transitions (`VERIFIED`, `PARTIALLY_VERIFIED`, `CONFLICTED`, `UNVERIFIED`).
- Test category freshness expiration (`FRESH`, `STALE`, `UNKNOWN`).
- Test budget exhaustion and partial result return behavior.
- Test `TASK_DISPATCH` execution via 0001 `EXECUTOR_REGISTRY` and `WorkerFencingService`.

### 2. Live provider evidence gate (Credentialed external smoke testing)
Live provider interactions require explicit environment credentials and are gated separately:
- Activated only when `SERPAPI_API_KEY` is present in the environment.
- Executes real search queries for representative travel destinations (for example Kolkata festival traffic or Goa stay rates).
- Validates real response latency, schema parsing, and minor unit cost logging.
- Skipped automatically during normal offline CI and unit test runs.

### 3. Cloud deployment evidence gate (Deferred live AWS infrastructure)
Live AWS infrastructure operations are tracked separately and deferred until full stack deployment:
- SAM deployment of `backend/src/functions/evidence_collector/template.yaml`.
- Real SQS task dispatch queue delivery and DLQ redrive policies.
- Live Aiven PostgreSQL/PostGIS connectivity from deployed Lambda runtime.
- Cold start and warm start Lambda memory and duration measurements under CloudWatch.
