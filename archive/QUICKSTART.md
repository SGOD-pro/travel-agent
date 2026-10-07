# The project in one page

## Three user experiences
| Capability | Input | Result |
|---|---|---|
| Travel Planner | Dates, destination, interests, budget | Evidence-backed experiences, food, nearby places, schedule, map and honest costs |
| Route Optimizer | Stops, start/end, mode, hours | Feasible stop order, real road/walking legs, parking, skipped-stop reasons and return time |
| Media Location | Image/video upload or accessible public URL | Location candidates, supporting clues, uncertainty or verified place |

Only execute requested capabilities and their necessary internal dependencies. A route calculation inside an accepted itinerary does not need a second confirmation. A new unrelated trip suggested after identifying an image does.

## Eight planned deployment folders
api, orchestrator, decision_router, evidence_collector, browser, media_extractor, media_analyzer, optimizer. Eight is a target decomposition, not eight functions required on day one. Destination intelligence is trip-planner graph logic, not a ninth mandatory worker.

LLM proposes plans and interprets evidence. LAYA proposes bounded routes. Policy validates every action. Workers execute bounded tasks. PostgreSQL records durable progress; S3 holds artifacts; LangGraph resumes from completion events.

## Build now / stop later
1. Contracts → durable execution → observability → queue + minimal orchestrator.
2. Evidence → browser → LAYA → optimizer; test each separately.
3. Route-only graph and API vertical slice; manually inspect the map.
4. Media extraction → analysis → location graph; manually review ambiguous examples.
5. Travel graph → three-capability composition → E2E release gates.

Credentials are supplied through environment/secret storage, never pasted in source or chat. Live provider tests stop at that module if credentials are missing; fixtures remain usable. Routing production endpoint, closure support, commercial weather terms, tile source and model CPU measurements remain explicit readiness gates.
