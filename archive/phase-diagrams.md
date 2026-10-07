# phase-diagrams.md — Locked Build Phases

## Global gate

```mermaid
flowchart LR
 S["Spec"] --> C["Contract"] --> T["Tests"] --> B["Build"] --> I["Integration"] --> F["Failure tests"] --> R["Runtime"] --> P["Performance"] --> O["Observability"] --> D["Docs"] --> DONE["DONE"]
```

## Phase 0 — Contracts

```text
backend/src/contracts/
execution.py
task.py
worker_result.py
completion.py
decision.py
evidence.py
media_location.py
route.py
trip.py
```

```mermaid
flowchart LR
 E["Execution"] --> T["Task"] --> R["WorkerResult"] --> C["Completion"]
 T --> D["Decision"]
 EV["Evidence"] --> VP["VerifiedPlace"] --> RR["RouteRequest"] --> OR["OptimizedRoute"] --> TP["TripPlan"]
```

No product code yet.

## Phase 1 — Execution Core

```text
backend/src/application/execution/
service.py
idempotency.py
lease.py
lifecycle.py
```

```mermaid
flowchart LR
 T["Task"] --> I["Idempotency"] --> L["Lease"] --> X["Execution"] --> R["Result"]
 I --> DB["PostgreSQL"]
 L --> DB
```

## Phase 2 — Observability

```text
backend/src/observability/
logging.py
tracing.py
metrics.py
audit.py
cost.py
```

```mermaid
flowchart LR
 M["Any module"] --> L["Logs"]
 M --> T["Traces"]
 M --> ME["Metrics"]
 M --> A["Audit"]
 M --> C["Cost"]
```

## Phase 3 — Queue Runtime

```text
backend/src/infrastructure/aws/
sqs.py
s3.py
lambda_runtime.py

backend/src/functions/orchestrator/dispatcher.py
```

```mermaid
flowchart LR
 P["Producer"] --> D["Dispatcher"] --> Q["SQS"] --> W["Worker"] --> R["WorkerResult"] --> E["CompletionEvent"]
```

## Phase 4 — Evidence Collector

```text
backend/src/functions/evidence_collector/
handler.py
service.py
schemas.py
repository.py
search.py
scrape.py
```

```mermaid
flowchart TD
 T["EvidenceTask"] --> S["search.py"] --> SERP["SerpAPI"] --> R["Rank"] --> SC["scrape.py / Scrapling"]
 SC --> D{"Dynamic / blocked?"}
 D -->|"yes"| B["Browser Worker"]
 D -->|"no"| N["Normalize"]
 B --> N
 N --> DB["Repository / PostgreSQL"]
 N --> S3["Raw evidence / S3"]
```

## Phase 5 — Browser

```text
backend/src/functions/browser/
handler.ts
service.ts
schemas.ts
```

```mermaid
flowchart TD
 T["BrowserTask"] --> V["validateTarget()"] --> C["createBrowserContext()"] --> P["Playwright"] --> N["navigate()"] --> W["waitForContent()"]
 W --> DOM["extractDOM()"]
 W --> SS["captureScreenshot()"]
 W --> META["extractMetadata()"]
```

## Phase 6 — LAYA

```text
backend/src/functions/decision_router/
handler.py
service.py
schemas.py
policies.py
```

```mermaid
flowchart LR
 R["DecisionRequest"] --> G["DecisionGateway"] --> L["LAYA"] --> C["Confidence"] --> P["Policy"] --> O["DecisionEnvelope"]
```

Functions:

```text
decide()
choice()
score()
noul()
route()
triage()
guardrail()
```

## Phase 7 — Media Extractor

```text
backend/src/functions/media_extractor/
handler.py
service.py
schemas.py
repository.py
audio.py
frames.py
```

```mermaid
flowchart LR
 URL["URL"] --> F["fetch media"] --> META["metadata"]
 F --> A["audio.py"]
 F --> FR["frames.py"] --> SF["scene selection"]
 META --> S3["S3"]
 A --> S3
 SF --> S3
```

## Phase 8 — Media Analyzer

```text
backend/src/functions/media_analyzer/
handler.py
service.py
schemas.py
repository.py
ocr.py
transcription.py
scene_filter.py
```

```mermaid
flowchart TD
 ART["Artifacts"] --> SF["scene_filter.py"] --> L["LAYA gate"]
 L --> O["OCR"]
 L --> A["ASR"]
 L --> V["Vision"]
 O --> F["Observation Fusion"]
 A --> F
 V --> F
 F --> C["Location Candidates"]
```

## Phase 9 — Media Location Product

```text
backend/src/workflows/media_location/
graph.py
state.py
nodes.py
policies.py
```

```mermaid
flowchart TD
 URL["Social URL"] --> EX["Media Extractor"] --> AN["Media Analyzer"] --> C["Candidates"] --> E["Evidence"] --> V["Verification"] --> D{"Enough evidence?"}
 D -->|"yes"| OUT["VerifiedPlace[]"]
 D -->|"no"| E
```

Output: `VerifiedPlace[]`.

## Phase 10 — Route Optimizer

```text
backend/src/functions/optimizer/
handler.py
service.py
schemas.py
repository.py
solver.py
routing.py
constraints.py
costs.py

backend/src/workflows/route_optimizer/
graph.py
state.py
nodes.py
policies.py
```

```mermaid
flowchart TD
 R["RouteRequest"] --> G["Geocode"] --> RP["Routing API"] --> M["Route Matrix"]
 R --> E["Evidence Collector"]
 E --> FC["Festival / Closure constraints"]
 M --> C["Constraint Engine"]
 FC --> C
 C --> O["OR-Tools"] --> CO["Costs"] --> V["Validation"] --> OUT["OptimizedRoute"]
```

## Phase 11 — Travel Planner + Destination Intelligence

```text
backend/src/workflows/trip_planner/
graph.py
state.py
nodes.py
policies.py

backend/src/functions/destination_intelligence/
handler.py
service.py
schemas.py
repository.py

backend/src/domain/destination/
insights.py
categories.py

backend/src/domain/experience/
ranking.py
```

```mermaid
flowchart TD
 B["Trip Brief"] --> L["LLM: intent + research plan"]
 L --> G["LAYA: category gating"]
 G --> WX["Weather / Season"]
 G --> HE["Holidays / Festivals / Events"]
 G --> FM["Famous / Must-visit"]
 G --> NP["Nearby / Day Trips"]
 G --> FD["Local Food"]
 G --> RS["Restaurants / Food Streets"]
 G --> HG["Hidden / Lesser-known"]
 G --> PH["Scenic / Photo / Sunrise / Sunset"]
 G --> CU["Culture / Markets"]
 G --> NA["Nature / Activities"]
 G --> PR["Access / Opening / Fees / Permits"]
 G --> CR["Crowd / Safety / Current Conditions"]
 G --> ST["Stay / Transport"]
 WX --> E["Evidence Collector"]
 HE --> E
 FM --> E
 NP --> E
 FD --> E
 RS --> E
 HG --> E
 PH --> E
 CU --> E
 NA --> E
 PR --> E
 CR --> E
 ST --> E
 E --> V["Verification"]
 V --> F["DestinationInsight[] / VerifiedPlace[]"]
 F --> RANK["Experience Ranker / Policy"]
 RANK --> R["Route Optimizer when needed"]
 F --> S["Schedule"]
 R --> S
 S --> BU["Budget"]
 BU --> SYN["LLM Synthesis"]
 SYN --> OUT["TripPlan"]
```

### Phase 11 acceptance

A Travel Planner implementation is not complete if it only produces attractions + route + budget. It must demonstrate category-aware destination research for relevant scenarios and preserve evidence/freshness for time-sensitive claims.

## Phase 12 — Orchestrator + Composition

```text
backend/src/functions/orchestrator/
handler.py
service.py
repository.py
dispatcher.py
state.py
llm.py
tools/
```

```mermaid
flowchart TD
 U["User"] --> L["LLM: understand/decompose"] --> Y["LAYA: minimum required capabilities"] --> P["Policy"] --> D["Dispatcher"]
 D --> M["Media only"]
 D --> R["Route only"]
 D --> T["Travel only"]
 M -->|"only if requested"| R2["Route"]
 M -->|"only if requested"| T2["Travel"]
 T -->|"when required/requested"| R3["Route"]
 R -->|"when requested"| T3["Travel"]
```

## Phase 13 — Public API

```text
backend/src/functions/api/
handler.py
app.py
service.py
schemas.py
routes/
```

Target:

```text
POST /api/v1/trips
POST /api/v1/trips/{id}/plans
POST /api/v1/discovery/media
POST /api/v1/optimization/routes
GET  /api/v1/jobs/{job_id}
POST /api/v1/jobs/{job_id}/cancel
GET  /api/v1/jobs/{job_id}/events
```

## Phase 14 — E2E + Hardening

```mermaid
flowchart TD
 A["Media only"]
 B["Route only"]
 C["Travel only"]
 D["Media → Travel"]
 E["Media → Route"]
 F["Travel → Route"]
 G["Media → Travel → Route"]
 H["Failure injection"]
 I["Security"]
 J["Performance / Cost"]
 A --> X["Release evidence"]
 B --> X
 C --> X
 D --> X
 E --> X
 F --> X
 G --> X
 H --> X
 I --> X
 J --> X
```

## Locked order

```text
0 Contracts
1 Execution Core
2 Observability
3 Queue Runtime
4 Evidence Collector
5 Browser
6 LAYA
7 Media Extractor
8 Media Analyzer
9 Media Location
10 Route Optimizer
11 Travel Planner
12 Orchestrator + Composition
13 Public API
14 E2E + Hardening
```
