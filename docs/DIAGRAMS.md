# Diagrams — workflow and development debug map

Read ARCHITECTURE.md for ownership and TECHNICAL_SPEC.md for payloads. Each diagram shows one relationship. Worker arrows represent task/completion contracts, not direct function imports. Limits and waiting paths are explicit.

## Debug lookup
| Symptom | Diagram | Check |
|---|---|---|
| Wrong worker/tool or unexpected extra capability | 1 and 8 | Accepted scope, typed plan, LAYA proposal, Policy gate |
| Stuck/duplicate job | 2 | Outbox, queue age, lease fencing, pending IDs and terminal outcomes |
| Unsupported confident answer | 3 | Claim evidence, duplicates, scope, freshness and policy transition |
| Route crosses closure/vehicle zone | 4 | Restriction geometry, provider support, actual traversal and hub mode continuity |
| Reroute changes visited stops | 5 | Completed-stop snapshot, current GPS/time, expected route version |
| Wrong/forced media identity | 6 | Frame/audio timestamps, OCR/ASR support, candidate uncertainty |
| Generic itinerary missing local experiences | 7 | Relevant category tasks, missing-data branches, ranking and schedule |
| Phase order/path drift | Development diagrams | DEVELOPMENT.md phase table and ARCHITECTURE.md folder ownership |

Diagrams visualize the specifications; they do not override contracts. All loops have execution deadline/call/repair limits. A “no budget” branch is terminal partial/failed/waiting-user as appropriate.

## 1. Harness: LLM, LAYA, policy and tool calls
```mermaid
flowchart TD
 U["User request"] --> API["API: authenticate and validate"]
 API --> JOB["Persist execution and start outbox"]
 JOB --> O["Orchestrator: lease and checkpoint"]
 O --> P["Hosted LLM: typed task plan"]
 P --> Y["LAYA: bounded worker or tool proposal"]
 Y --> G{"Policy approval?"}
 G -->|Approved| D["Persist tasks and dispatch outbox"]
 G -->|Needs clarification| ASK["WAITING_USER"]
 G -->|Invalid or over budget| STOP["Partial result or failure"]
 ASK -->|User reply with new context version| O
 D --> Q["SQS worker queues"]
 Q --> W["Selected worker: bounded tool calls"]
 W --> C["Persist result and completion outbox"]
 C --> CQ["Completion queue"]
 CQ --> JOIN["Deduplicate and reconcile pending tasks"]
 JOIN --> O
 O --> READY{"Graph ready for final result?"}
 READY -->|Tasks pending| WAIT["Checkpoint and exit"]
 READY -->|Ready| V["Validate claims and calculations"]
 V --> S["Hosted LLM: supported explanation"]
 S --> CHECK["Final schema and citation checks"]
 CHECK --> OUT["Persist result for owner-scoped API"]
```
Initial plan proposal runs once per needed plan revision, not on every completion. Ready nodes may be deterministic and skip LLM/LAYA. Store does not contain hidden chain-of-thought; short explanations and evidence references are sufficient.

## 2. Durable task delivery and recovery
```mermaid
flowchart TD
 T["Task persisted with outbox"] --> RELAY["Scheduled or completion-triggered relay"]
 RELAY --> Q["SQS delivers at least once"]
 Q --> CLAIM{"Claim task lease?"}
 CLAIM -->|Already terminal| ACK["Acknowledge duplicate"]
 CLAIM -->|Active valid lease| LATER["Defer duplicate delivery"]
 CLAIM -->|Lease acquired| RUN["Execute bounded work"]
 RUN --> COMMIT["Transaction: result and completion outbox"]
 COMMIT --> RELAY
 COMMIT --> FIN["Ack input after durable result"]
 RELAY --> EVENT["Completion reconciler"]
 EVENT --> FENCE{"Current attempt and context?"}
 FENCE -->|No| IGNORE["Record stale completion; no resume"]
 FENCE -->|Yes| RESUME["Lease-protected graph update"]
 RUN -->|Crash or transient failure| REC["Bounded retry after lease expiry"]
 REC --> Q
 REC -->|Deadline or retries exhausted| FAIL["Terminal outcome and DLQ reconciliation"]
```
A send that succeeds before marking an outbox row delivered can duplicate messages; consumers deduplicate by logical IDs. Transactional results survive ack/send crashes. DB unavailability is retried without pretending task success.

## 3. Research and verification
```mermaid
flowchart TD
 R["Scoped research task"] --> QUERY["LLM proposes queries when needed"]
 QUERY --> P["Policy: sources and call budget"]
 P --> S["Collector: SerpAPI searches"]
 S --> F["Scrapling static extraction"]
 F --> KIND{"Extraction outcome?"}
 KIND -->|Content| OBS["Observations with source locators"]
 KIND -->|Permitted dynamic page| REQUEST["NEEDS_BROWSER result"]
 REQUEST --> O["Orchestrator approves browser task"]
 O --> B["Playwright worker"]
 B --> OBS
 KIND -->|Denied or inaccessible| UN["Unavailable source"]
 OBS --> DEDUP["Deduplicate copied sources"]
 DEDUP --> LLM["LLM proposes normalized claims"]
 LLM --> VERIFY["Policy: support, scope, freshness and conflict"]
 UN --> VERIFY
 VERIFY --> ENOUGH{"Evidence sufficient?"}
 ENOUGH -->|Yes| OUT["Claims with verification and freshness"]
 ENOUGH -->|No and budget remains| R
 ENOUGH -->|No budget or no access| PART["Candidates, gaps or abstention"]
```
Browser is not an anti-bot bypass. Unknown dates do not become fresh publication dates. Claims may stay conflicted even when multiple sources repeat them.

## 4. Route optimization and closures
```mermaid
flowchart TD
 REQ["Stops, start, end, modes and windows"] --> GEO["Resolve canonical places and access points"]
 GEO --> AMB{"Coordinates unambiguous?"}
 AMB -->|No| ASK["Ask user or map-pin confirmation"]
 AMB -->|Yes| EV["Research and verify applicable restrictions"]
 EV --> MAP["Resolve restriction geometry and effective hours"]
 MAP --> CAP{"Routing provider supports hard restrictions?"}
 CAP -->|No| LIMIT["Unavailable or limited result; no feasibility claim"]
 CAP -->|Yes| MATRIX["Restriction-aware road and walking costs"]
 MATRIX --> SOLVE["OR-Tools: order, windows and optional stops"]
 SOLVE --> PATH["Fetch actual legs at planned departures"]
 PATH --> VALID{"Geometry and traversal-time checks pass?"}
 VALID -->|Yes| COST["Calculate driving fuel and elapsed time"]
 COST --> OUT["Versioned route, map, alternatives and explanations"]
 VALID -->|No and repair budget remains| REPAIR["Update affected costs and constraints"]
 REPAIR --> MATRIX
 VALID -->|No budget or impossible| BAD["Infeasible or marked partial route"]
 SOLVE -->|No feasible solution| BAD
```
Vehicle-zone visits create permitted parking/access hubs plus walking legs; check walking limits and parking-return requirements. Time-based closure affects passage through a road, not just arrival at an attraction. Unverified closure is a warning and labeled avoidance alternative.

## 5. Mid-trip replanning
```mermaid
flowchart TD
 UPDATE["User reports blockage or new conditions"] --> SNAP["Read route version and completed stops"]
 SNAP --> POS{"Current position reliable?"}
 POS -->|No| ASK["Ask position or confirm last stop"]
 POS -->|Yes| FREEZE["Freeze history; start remaining route here"]
 FREEZE --> RESTR["Verify reported closure or mark user report"]
 RESTR --> REBUILD["Recompute affected paths and remaining costs"]
 REBUILD --> SOLVE["Solve remaining stops with current time"]
 SOLVE --> CHECK["Validate access, windows and return deadline"]
 CHECK --> COMMIT{"Still current route version?"}
 COMMIT -->|Yes| OUT["Publish next route version and map diff"]
 COMMIT -->|No| RETRY["Reconcile newer update within budget"]
 RETRY --> SNAP
 RETRY -->|Budget exhausted| STALE["Stop with conflict; keep current route"]
```
A visited A/B stays unchanged. A B→C blockage while travelling uses current position, not teleportation back to B. Changed arrival times may require reordering C/D/E and changing skipped stops.

## 6. Media location: extraction, analysis and uncertainty
```mermaid
flowchart TD
 INPUT["Upload or public URL with optional hints"] --> ACCESS{"Owned or permitted media accessible?"}
 ACCESS -->|No| ALT["Unavailable; offer upload or manual clues"]
 ACCESS -->|Yes| EX["FFprobe and FFmpeg: metadata, audio and frames"]
 EX --> FILTER["Non-LLM dedupe, blur and scene sampling"]
 FILTER --> GATE["LAYA proposal and policy for relevant analysis"]
 GATE --> OCR["OCR on selected frames"]
 GATE --> ASR["Hosted ASR: submit and exit"]
 GATE --> VISION["Optional evaluated vision model"]
 ASR --> CALLBACK["Scheduled status completion resumes graph"]
 OCR --> JOIN["Fuse observations and timestamps"]
 CALLBACK --> JOIN
 VISION --> JOIN
 JOIN --> CAND["LLM proposes location candidates from clues"]
 CAND --> SEARCH["Evidence and geocoding verification tasks"]
 SEARCH --> DEC{"Unique supported location?"}
 DEC -->|Yes| OUT["Verified identity and coordinates with evidence"]
 DEC -->|Ambiguous| CHOOSE["Ranked candidates and user confirmation"]
 DEC -->|No evidence and budget remains| SEARCH
 DEC -->|No evidence or budget exhausted| GAP["Insufficient evidence"]
```
Optional/skipped nodes yield a terminal skipped outcome so joins cannot wait forever. Missing audio is not extraction failure for a still image. No automatic trip starts after output.

## 7. Travel: destination intelligence and practical plan
```mermaid
flowchart TD
 BRIEF["Dates, place, interests, mobility and budget"] --> PLAN["LLM proposes relevant research categories"]
 PLAN --> GATE["LAYA proposal plus deterministic policy"]
 GATE --> TASKS["Fan-out bounded category tasks"]
 TASKS --> RESEARCH["Weather, places, food, nearby and practical evidence"]
 RESEARCH --> JOIN["Join terminal tasks; verify each claim"]
 JOIN --> RANK["Rank by user fit, evidence, access and diversity"]
 RANK --> ROUTE{"Movement calculations needed?"}
 ROUTE -->|Yes| OPT["Internal route workflow"]
 ROUTE -->|No| SCHED["Deterministic schedule"]
 OPT --> SCHED
 SCHED --> BUDGET["Known, estimated and unknown costs"]
 BUDGET --> CHECK{"Hard requirements feasible?"}
 CHECK -->|Yes| SYN["LLM explanation grounded in plan"]
 SYN --> OUT["TripPlan with citations and map"]
 CHECK -->|Repair possible within budget| ADJUST["Adjust optional experiences; retain hard needs"]
 ADJUST --> RANK
 CHECK -->|No| PART["Partial or infeasible with reasons"]
```
Relevant research includes weather/season, festivals/events, landmarks, nearby/day trips, dishes/eateries, hidden candidates, scenic/photo, culture/nature, hours/fees/permits, crowds/safety, stays/transport. Each task carries category and evidence IDs; this is not a thirteen-agent mandatory fan-out.

## 8. Product composition and acceptance
```mermaid
flowchart TD
 REQUEST["User request"] --> SCOPE["Select requested capabilities and necessary dependencies"]
 SCOPE --> MEDIA["Media graph"]
 SCOPE --> ROUTE["Route graph"]
 SCOPE --> TRAVEL["Travel graph"]
 MEDIA -->|Requested composition| TRAVEL
 MEDIA -->|Requested composition| ROUTE
 TRAVEL -->|Necessary movement dependency| ROUTE
 MEDIA --> RESULT["Return requested result"]
 ROUTE --> RESULT
 TRAVEL --> RESULT
 RESULT --> SUGGEST["Optional relevant next step"]
 SUGGEST --> ACCEPT{"User accepts new capability?"}
 ACCEPT -->|Yes| REQUEST
 ACCEPT -->|No| END["Finish"]
```
These are logical product dependencies. The queue/lease flow in diagram 1 governs execution. A travel graph consumes route output; it does not recursively start a second travel graph.


## Development phase diagrams

## Foundation: phases 0–3
```mermaid
flowchart TD
 C["0: Contracts and examples"] --> E["1: Execution, leases and outbox"]
 E --> O["2: Logs, metrics and cost tracking"]
 O --> Q["3: Queues and minimal graph resume"]
 Q --> TEST{"Recovery and contract tests pass?"}
 TEST -->|No| FIX["Repair this slice"]
 FIX --> Q
 TEST -->|Yes| WORKERS["Begin independent workers"]
```
Paths: contracts/; functions/orchestrator/execution/; utils/logger.py and module-owned telemetry; functions/orchestrator/dispatcher.py. Minimal API health/job route lives in functions/api/.

## Evidence and decision: phases 4–6
```mermaid
flowchart TD
 E["4: Search, extract and provenance"] --> ET{"Evidence tests pass?"}
 ET -->|Yes| B["5: Approved browser escalation"]
 B --> BT{"Runtime and network tests pass?"}
 BT -->|Yes| L["6: LAYA and policy gate"]
 L --> LT{"Decision and CPU tests pass?"}
 ET -->|No| RE["Repair collector"]
 RE --> E
 BT -->|No| RB["Repair browser"]
 RB --> B
 LT -->|No| RL["Repair inference or fallback"]
 RL --> L
 LT -->|Yes| NEXT["Worker foundation ready"]
```
Paths: functions/evidence_collector/; functions/browser/; functions/decision_router/. Independent fixture tests precede credentialed live smoke tests.

## Route vertical slice: phases 7–8
```mermaid
flowchart TD
 R["7: Routing adapter and optimizer"] --> G{"Restrictions and geometry validated?"}
 G -->|No| FIX["Repair or mark provider limitation"]
 FIX --> R
 G -->|Yes| GRAPH["8: Route graph and API"]
 GRAPH --> MAP["Manual map and reroute review"]
 MAP --> DONE["Route-only slice demonstrated"]
```
Paths: functions/optimizer/; functions/orchestrator/graphs/route_optimizer/; functions/api/routes/optimization.py. A provider limitation does not count as completed production closure support.

## Media vertical slice: phases 9–11
```mermaid
flowchart TD
 EX["9: Audio and frames"] --> SIG["10: OCR and asynchronous ASR"]
 SIG --> GRAPH["11: Media-location graph and API"]
 GRAPH --> CASES{"Verified, ambiguous and inaccessible cases handled?"}
 CASES -->|No| FIX["Repair responsible module"]
 FIX --> GRAPH
 CASES -->|Yes| DONE["Media slice demonstrated"]
```
Paths: functions/media_extractor/; functions/media_analyzer/; functions/orchestrator/graphs/media_location/. No duplicated global workflows folder.

## Travel and release: phases 12–14
```mermaid
flowchart TD
 T["12: Relevant research and travel graph"] --> CHECK["Schedule, costs, evidence and weather tests"]
 CHECK --> REVIEW["Manual destination-experience review"]
 REVIEW --> C["13: Accepted composition and frontend"]
 C --> E2E["Independent and combined E2E tests"]
 E2E --> H["14: Failure, security and load gates"]
 H --> RELEASE{"All release dependencies ready?"}
 RELEASE -->|No| BLOCK["Record blocker; no false readiness claim"]
 RELEASE -->|Yes| READY["Reviewable release candidate"]
```
Path: functions/orchestrator/graphs/trip_planner/ owns research/ranking/schedule/budget. No mandatory destination_intelligence Lambda. Phases.md is the authoritative phase table.
