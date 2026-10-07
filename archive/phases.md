# Build phases — one slice at a time

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

Destination intelligence stays inside trip_planner/ with category subtasks, not a separate compulsory Lambda. All phases use the path ownership in architecture.md. Voice remains v1.1; booking/payments deferred.

After each module: contract+unit tests, meaningful integration tests for its dependencies, negative/failure tests, runtime measurements for deployable code. After each product graph: E2E API/map review. Manual review is for concrete results, not repeated permission to write routine code.
