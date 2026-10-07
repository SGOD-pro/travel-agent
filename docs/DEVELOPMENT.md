# Development — build, test and operate one slice

## Working rules and reading
Read PROJECT.md + relevant REQUIREMENTS.md/ARCHITECTURE.md sections, then the target TECHNICAL_SPEC.md heading. Load UI_UX.md only for UI work and DIAGRAMS.md only for workflow debugging. File count is not context size: do not load every section by default.

Inspect existing code and repository AGENTS.md before changes. Reuse proven code but do not preserve obsolete architecture. No all-feature scaffolding sprint. Build only the current phase, run meaningful tests and report changed files, results, assumptions and remaining gates. A phase review pauses progression, not routine reversible implementation. Manual review needs a concrete demo, not an abstract permission request.

## Source consolidation and replacement
This seven-file package resolves the mixed versions attached by the user: older global domain/application/workflows folders, separate destination worker, old phase order and forced verified media output are superseded. Archive lowercase/duplicate documents and historical PDF outside active docs. Preserve AGENTS.md and repository root README; point their doc links at PROJECT.md if appropriate. Do not overwrite current working code merely to match prose.

## Environments and commands
Use local, dev/test, staging and production configuration. Separate DB schemas/databases, queues/buckets/prefixes and credentials. Never test cross-owner or destructive failure cases on production data. Runtime Python 3.12 initial target; Node version must be compatible with pinned Next.js/Chromium/SAM runtimes. Choose and pin per-module versions during its slice.

Initial commands below are setup/test patterns, not claims the repository already has these files/scripts:
```bash
python3 -m venv backend/.venv
# Activate the venv using the command appropriate for your shell.
python -m pip install -r backend/src/functions/evidence_collector/requirements.txt
python -m pip install pytest
python -m pytest backend/src/functions/evidence_collector/tests
sam validate --template-file backend/src/functions/evidence_collector/template.yaml
sam build --template-file backend/src/functions/evidence_collector/template.yaml
npm --prefix frontend ci
npm --prefix frontend run dev
```
Create locks, test configuration and shared-file-aware SAM builder in the owning phase. Heavy incompatible module dependencies may use separate local environments. Linux-target native packaging may use a local build container; this is not an ECR/ECS runtime commitment. SAM build succeeds only when configured to package required shared imports. Do not deploy an untested artifact.

Variables/credentials and which phase needs them are in TECHNICAL_SPEC.md section 1. Local boto3 uses the standard credential chain: AWS_PROFILE if configured (for example aws), otherwise environment/session credentials; Lambda uses IAM role and CI uses OIDC. No custom credential wizard or hardcoded key needed. .env files gitignored; example config contains names/placeholders only. Fail fast on missing required settings; fixtures can proceed without paid-provider credentials.

## Build phases
Each phase ends with test evidence, changed files, unresolved dependencies and a short demonstration. Do not start the next dependent phase until its gates pass. No requirement to run live E2E for a pure schema phase.

| Phase | Implement | Check before moving | Manual/provider gate |
|---|---|---|---|
| 0 | Pydantic contracts, generated TS/JSON Schema, example fixtures, provider registry skeleton | Schema/negative/version tests | Review field names, UI labels, money/time units |
| 1 | Orchestrator execution repositories, leases, lifecycle, transactional outbox | PostgreSQL transactions, duplicate/crash recovery | DATABASE_URL; review migrations |
| 2 | Shared logs/errors + module-owned traces/metrics/cost tracking | Correlation and secret-redaction tests | Inspect one trace |
| 3 | SQS delivery, completion, scheduled relay/recovery, minimal LangGraph resume; minimal API health/job endpoint | Duplicate delivery, delayed result, lease races, DLQ, cancellation | AWS test resources, IAM/JWT configuration |
| 4 | Evidence collector + canonical-place/claim verification helpers | Search/extract/provenance, source copy/conflict/freshness tests | SERPAPI_API_KEY; inspect source-backed report |
| 5 | Browser escalation through orchestrator | Permitted dynamic page, blocked target, timeout, SSRF | Packaging benchmark; inspect extraction |
| 6 | LAYA adapter, policy gates, fallback | Fixed decision dataset, disagreement/abstention, ONNX imports/memory | Artifact version/checksum; review confidence thresholds |
| 7 | Optimizer, geocoder/routing adapters, geometry validation | Exact small fixtures, closures, mode transfer, midnight, timeout | Routing endpoint and exclusions; no production route without them |
| 8 | Route graph + API vertical slice | Route-only E2E, changed-road reroute, map GeoJSON | Manually inspect parking/walking/return-home output |
| 9 | Media extractor | Frame/audio timestamps, blur/dedupe, size/duration failures | FFmpeg Lambda runtime measurement |
| 10 | Media analyzer + asynchronous ASR | OCR languages, ASR completion, uncertainty | Transcribe access; model memory/size tests |
| 11 | Media-location graph + API | Upload and public URL; inaccessible/ambiguous/verified outcomes | Review candidate explanations, not only top-1 guess |
| 12 | Trip-planner graph: research, ranking, schedule, budget, weather | Relevant categories, distant dates, food vs business, unknown costs | Weather/license/geocoder/map gates; review example trip |
| 13 | Cross-product composition + full frontend/API | Independent/pair/all-three requests, acceptance boundaries | Manual UX review; no automatic new-product execution |
| 14 | Security, operational and release hardening | E2E/failure/load/package/recovery gates | Release benchmark review and production config |

Destination intelligence stays inside trip_planner/ with category subtasks, not a separate compulsory Lambda. All phases use the path ownership in ARCHITECTURE.md. Voice remains v1.1; booking/payments deferred.

After each module: contract+unit tests, meaningful integration tests for its dependencies, negative/failure tests, runtime measurements for deployable code. After each product graph: E2E API/map review. Manual review is for concrete results, not repeated permission to write routine code.


## ZIP-first SAM
One SAM stack/template per function plus shared infrastructure stack. Each worker template references provisioned queue/resource parameters. Queues and permissions belong to shared infrastructure; handlers do not invent resources. Scheduled recovery/outbox invocation and completion consumer belong to orchestrator deployment. SAM architecture/runtime must match native wheels/binaries.

Build function folder + declared shared imports into staging. Use a custom SAM build/Makefile or equivalent build script because shared files are outside module CodeUri. Stage Python package as src/functions/<module> and src/config/contracts/utils as required; add __init__.py and test exact handler path. Exclude other modules, tests and export/training dependencies from artifact. Browser contracts are generated JSON Schema/TypeScript, not Python imports.

50 MB compressed direct-upload limit; larger ZIP via S3; 250 MB uncompressed combined function/layers/custom runtime limit. Layers do not bypass it. Regular function timeout max 15 minutes. Models loaded from S3 into /tmp still incur cold latency, memory and download cost. Source: https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html

## Change selection
| Path | Action |
|---|---|
| src/functions/<module>/** including template/lock | Build/test/deploy that module |
| src/contracts/** | Regenerate schemas/TS; test and rebuild all consumers including browser/frontend |
| src/config/** or src/utils/** | Dependency manifest selects consumers; if uncertain rebuild all Python functions |
| src/middleware/** | API only unless a declared consumer exists |
| infrastructure/shared/** | Validate infrastructure change; test/deploy affected dependent stacks in order |
| infrastructure/migrations/** | Migration safety check; one serialized migration job before dependent app rollout |
| scripts/build* or workflow definitions | Recheck all affected packaging/deployment targets |
| docs only | Links/schema/diagram consistency checks; no application deploy |

Manifest per function declares shared_paths, runtime, package_file, template, handler, generated_contracts. detect_changes checks base/head commit diff including renames/deletions; outputs CI matrix. Root/global build changes cannot be ignored. OIDC IAM credentials for GitHub Actions; deploy from trusted branches only. No secret access for untrusted PRs. Deploy immutable artifact hashes with environment concurrency and smoke-test rollback procedure.

## Required packaging checks
Module tests → SAM validate/build → size sum including layers → import test in matching environment → cold/warm memory/timeout benchmark → deploy test stack → queue/API smoke test. No exact package size or latency is asserted before measurement.

## Stop points
Missing provider credentials: stop live integration for that module, show fixtures/test results. ZIP fails: report dependency-size breakdown and propose bounded split/lean runtime before changing infrastructure. Routing endpoint/exclusions absent: no live safe-route claim. Infrastructure production deployment follows explicit environment authorization; building and reviewable test artifacts proceeds independently.


## Observability and operating limits
Structured logs include trace_id, execution_id, workflow, task_id, attempt/context_version, module, provider/model version, latency, outcome, usage and safe error code. Do not log secrets, full raw personal media or hidden chain-of-thought. Record compact rationales and evidence IDs. HTTP request ID propagates through task/completion metadata.

CloudWatch logs/metrics/alarms initially; OpenTelemetry instrumentation optional if package/runtime measurements justify it. Track API errors, job latency, queue age/depth/DLQ, outbox age, lease expiry, retry/repair counts, provider errors, solver timeouts, false verification, model spend and cold starts. Initial alarm policies: any unreconciled DLQ, outbox overdue beyond recovery SLA, repeated lease failures or configured spend cap. Numerical alert/latency ceilings are reviewed from measured dev baselines, not invented success claims.

Caching: source/artifact content hashes and provider-specific expiry; geocoder results by normalized query/locality/provider version; routing matrices by mode/coordinates/restriction snapshot/departure bucket; no cache key shared across private owner context accidentally. Stale safety/closure/weather cache cannot serve as fresh truth. Replan invalidates only dependent data but reevaluates remaining schedule.

## Performance and cost model
Bound fan-out per job/provider; reserve global/job spend before dispatch. Start fixture/load tests at concurrency 1/4/8, tune with CPU/memory and provider quotas. Async HTTP for I/O; bounded threads only for measured blocking libraries; no unlimited thread pools. Workers have explicit input/time/memory caps. Publish cold/warm p50/p95/p99, package size, peak memory, throughput, provider waits and cost per scenario.

Estimated job cost = Lambda GB-seconds + requests + SQS operations + S3 requests/storage + DB allocation + metered search + hosted LLM input/output tokens + ASR audio duration + paid provider charges. Record source date/region/unit rate before numeric estimates. Rates are not assumed free or fixed. Use minor-unit accounting/reservations; reconcile provider actuals and show uncertain cost. Persistent routing infrastructure has its own fixed allocation if selected. No deployment savings or latency is claimed without measurement.

Stop metered tasks when remaining cost/token/call/deadline budget cannot cover them. Missing environment spend ceiling blocks paid dispatch. Budgets are environment-configured, with defaults/initial limits in TECHNICAL_SPEC.md; review actual ceilings before live tests.

## Testing and evaluation gates — proposed, reviewed before release
| Gate | Dataset/setup | Pass criterion |
|---|---|---|
| Contract integrity | All documented valid/invalid fixtures, schema v1.0 | All expected outcomes; generated consumer types agree |
| Small routing correctness | >=20 tiny cases with enumerated feasible orders | Zero hard violations; optimal match only when solver claims optimum |
| Operational integrity | Duplicate, stale attempt, crash/outbox and cancel traces | One accepted completion/checkpoint transition; no lost committed task |
| Evidence correctness | >=50 independently annotated claims across categories | Zero false VERIFIED for critical closure/safety claims; >=95% valid citation links/support overall |
| Media uncertainty | >=30 verified and ambiguous/unavailable examples | No forced verified match for known ambiguous/unavailable cases; report top-k/abstention |
| LAYA routing | >=100 versioned bounded routing requests | >=95% policy-valid expected decisions; zero unauthorized tool execution |
| Packaging | Actual function+layers, selected runtime/architecture | <=250 MB uncompressed; no incompatible imports/binaries |
| Runtime | Cold+warm samples, 1/4/8 concurrency, fixed region/memory/input sizes | No timeout/OOM; publish p50/p95/p99 and cost; set user-facing latency ceilings after baseline |

These are proposed release thresholds, not measured scores. Version fixture data, annotation rubric, model/provider snapshots and artifact hashes. Separate fixtures from live tests. Cold/warm model benchmarks include artifact download, not only inference. Report traffic coverage and routing limitations. A regression blocks that module/composition. Do not tune evaluation labels to implementation output.


## Test progression and completion
- Schema phase: contract/version/negative fixtures only; generated TS agrees with Python.
- Worker phase: unit tests plus meaningful dependency integration and failure cases; credentialed smoke test only when configured.
- Product phase: independent API E2E and manual output/map review.
- Composition/release: pair/all-three scenarios, cancellation/recovery/auth/network/failure/load checks.

Maintain requirement ID → scenario/test mapping. Golden cases: Goa food/hidden/photo/weather, Kolkata Puja parking/closures, hill destination sunrise/roads, ambiguous image, route-only and accepted media→travel→route. Include forged/duplicate/late completions, DB/S3 failure, provider 429, stale/conflicting claims, SSRF/prompt injection and missing keys. Exact tiny route fixtures enumerate candidate orders; larger heuristic runs cannot prove optimum by assertion.

## Specification validation and dry-run status
The previous corrected workflow review covered 38 happy/failure scenarios, including closure traversal, hub walking, midnight, remaining-route recalculation, media ambiguity, ASR wait, copied/conflicted sources, outbox crashes, stale attempts, cancellation, cross-owner access and SSRF. This consolidation preserves those branches in DIAGRAMS.md and implementation protocols. Theoretical branch coverage is not executable correctness. No live API, deployment, package-size, solver or model tests are claimed.

Before Phase 0 begins, review exact fields/enums and proposed defaults. Before each live dependent slice, satisfy its provider/credential/runtime gate. Archive old packages; do not mark the whole project “production ready” after document checks.

## Consolidation checks performed
This seven-file revision passed internal filename/fence/module-path checks and JSON-fixture syntax checks. All 13 Mermaid blocks passed the Mermaid 11 parser. Browser rendering/layout was not tested. This is document/syntax evidence only; executable Pydantic contracts, live integrations and application tests remain Phase 0 onward.

## Coding-agent handoff
```text
Read docs/PROJECT.md and applicable repository AGENTS.md. Identify the current phase in docs/DEVELOPMENT.md. Load only relevant requirement IDs, architecture boundaries and TECHNICAL_SPEC sections. Implement this phase/module only; preserve the frontend contract and independent Lambda packaging. Run appropriate checks, report evidence and unresolved live gates, update the owning document if an approved decision changes. Stop at the phase review with a concrete result; do not implement the entire app or invent provider behavior.
```
