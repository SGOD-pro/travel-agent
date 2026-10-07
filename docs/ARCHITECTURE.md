# Architecture — component ownership

## Topology
Next.js frontend → FastAPI API Gateway/Lambda → durable job → LangGraph orchestrator → policy-approved task dispatch → SQS workers → durable completion → resume → owner-scoped result.

LLM proposes plan/evidence interpretation/report. LAYA proposes finite decisions. Deterministic Policy owns authorization, budgets, freshness transitions and constraints. Routing provider owns road geometry/costs; OR-Tools owns stop-order solving; MapCN/MapLibre renders output. DIAGRAMS.md shows the branches.

## Module boundaries
| Deployment module | Owns | Does not own |
|---|---|---|
| api | HTTP validation, auth, owner-scoped queries and upload grants | Research/model/solver computation |
| orchestrator | Job lifecycle, graph resume, fan-out/join, policy, canonical places/claims, trip logic | Browser/media native runtime or stop-order solver |
| decision_router | Bounded LAYA inference | Authorization, final verification or direct dispatch |
| evidence_collector | Search, permitted extraction, provenance | Final truth or browser invocation |
| browser | Approved dynamic rendering, DOM/metadata/screenshots | Access-control bypass or open-ended autonomous browsing |
| media_extractor | Probe, audio/frame extraction/filtering | Guaranteed semantic scene/location identification |
| media_analyzer | OCR/ASR signals and optional evaluated vision | Final verified identity |
| optimizer | Supported restriction-aware routing, solver and cost output | Full destination trip research or payment |

Research subagents are scoped graph tasks, not one Lambda each. Destination intelligence is trip_planner graph logic, not a compulsory ninth worker. Eight modules are the target decomposition; build only the current verified slice.

## Folder ownership
```text
backend/
  src/
    functions/
      api/                      # app.py, routes/, service.py, repository.py
      orchestrator/             # handler, dispatcher, lifecycle, checkpoints
        execution/              # leases, idempotency, outbox, recovery
        evidence/               # claim verification and freshness policies
        places/                 # canonical identity, geocoding policies/repository
        graphs/
          route_optimizer/      # graph.py, nodes.py, state.py, policies.py
          media_location/       # graph.py, nodes.py, state.py, policies.py
          trip_planner/         # graph, research, ranking, schedule, budget, prompts
        tools/                  # weather/geocoder/imagery provider clients
        prompts/
      decision_router/          # LAYA service, schemas, policies
      evidence_collector/       # search.py, scrape.py, repository.py
      browser/                  # handler.ts, service.ts, schemas.ts
      media_extractor/          # audio.py, frames.py, repository.py
      media_analyzer/           # ocr.py, transcription.py, scene_filter.py
      optimizer/                # routing.py, constraints.py, solver.py, costs.py
    config/
      config.py                 # settings only; secrets from environment
      aws.py                    # client factories; no clients on import
      db.py                     # connection factory; no queries/business rules
    middleware/
      auth.py                   # HTTP middleware used by API only
      request_id.py
      exception_handler.py
    contracts/                  # Pydantic v2 source; generated JSON Schema/TS
    utils/                      # logger, error types, bounded retry helpers
  infrastructure/
    shared/template.yaml        # queues, bucket policies, shared resource IDs
    migrations/                 # ordered, forward-safe PostgreSQL migrations
  scripts/                      # detect_changes, package, size checks
  tests/integration/
  tests/e2e/
```
Every function folder owns handler, service, schemas, tests, dependency lock, template.yaml and samconfig.toml. Python uses requirements.txt plus a reproducible lock; browser uses package.json/package-lock.json/tsconfig.json. repository.py exists only where the module writes/reads owned records. Per-module helpers stay there. Shared folders contain no feature/provider/model business logic.

No worker imports another worker implementation. The orchestrator owns feature graphs and adapters. Each deployment includes only its module and explicitly declared shared files. Preserve import paths consistently in local tests and ZIP builds. See DEVELOPMENT.md for packaging and CI/CD.


## State and communication
Aiven PostgreSQL/PostGIS stores executions, checkpoints, sources/claims, place identity and route versions. S3 stores raw media/evidence and large results. Upstash Redis is an optional ephemeral cache/rate coordinator. Durable protocol/table details belong to TECHNICAL_SPEC.md.

No worker-to-worker business imports or direct downstream invocations. A collector requests browser escalation through a result; orchestrator validates and dispatches. Shared Python folders contain settings, client factories, contracts and tiny utilities. Browser consumes generated TS/JSON Schema contracts.

Three product graphs are independently testable; they compose only under accepted scope. Travel may invoke routing as a necessary internal dependency. Media-only does not research a complete trip after identification.

## Architectural decisions
| ID | Decision | Reason |
|---|---|---|
| AD-01 | Three independent capabilities | Avoid automatic irrelevant execution and cost |
| AD-02 | LangGraph is the single execution owner | Recoverable state and controlled joins/resume |
| AD-03 | LLM/LAYA proposals pass Policy | Model output cannot become deterministic authority |
| AD-04 | Module-owned code/repositories; slim shared folders | Readability, independent ZIPs and selective CI |
| AD-05 | ZIP-first Lambda + SAM; API uses FastAPI/Mangum only | Bounded serverless work without assumed ECR/ECS |
| AD-06 | PostgreSQL authority, S3 blobs, Redis ephemeral | Recoverability without cache dependence |
| AD-07 | Evidence verification and freshness are separate | A previously verified fact can become stale |
| AD-08 | Routing adapter + OR-Tools | Stop ordering does not replace road-network routing |
| AD-09 | Report candidate/partial/infeasible results honestly | No forced media match or fake route/price |
| AD-10 | Hosted generative inference; local bounded CPU candidates | Avoid large LLM weights in ordinary ZIP Lambda |
| AD-11 | Tests/modules before composition | Limit change scope and catch contract mismatch |

Append architectural changes here with date, reason, alternatives and migration impact. Update dependent diagrams/tests; do not create a competing decisions document.

## Open readiness gates
Production Valhalla endpoint/hosting and exact exclusion capabilities; lawful public-media retrieval; geocoder scale policy; weather/tile commercial use; auth issuer; OCR-language/vision suitability; LAYA/FFmpeg/Chromium/OR-Tools package and CPU measurements. These do not block Phase 0 fixtures, but block dependent live-release claims. No public Valhalla demo is production infrastructure. No container migration is automatic if packaging fails.
