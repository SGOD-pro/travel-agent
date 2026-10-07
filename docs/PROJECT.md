# SWENA Travel Agent — project baseline

> We give you memories.

Build a travel-intelligence application that helps people discover worthwhile experiences and execute practical trips. The three capabilities run independently or compose when requested: **Travel Planner**, **Route Optimizer**, **Media Location Identifier**.

## Users and outcomes
Travelers, festival visitors, road trippers, food/culture explorers, families, solo travelers, couples and photographers. They need useful destination research, feasible movement and trustworthy explanations rather than a polished screen filled with fabricated facts.

| Capability | User provides | Receives |
|---|---|---|
| Travel Planner | Destination, dates, interests, travelers, budget and constraints | Local experiences, food, nearby discoveries, weather guidance, schedule, route and cost basis |
| Route Optimizer | Stops, start/end, mode, visit windows and priorities | Practical order, road/walking paths, parking hubs, alternatives, skipped-stop reasons and return time |
| Media Location | Image/video upload or accessible public URL; optional hints | Evidence-supported location candidates, uncertainty, or verified identity/coordinates |

## Scope
V1 includes evidence research and verification, all three workflows, real geocoding/routing integration, maps, replanning, asynchronous progress and cancellation, owner isolation, estimates and merchant handoff links when useful. Travel research covers the categories in REQUIREMENTS.md; destination intelligence is not an optional marketing feature.

Trip alternatives may compare duration, experience selection and cost basis. Live commercial search/booking requires separately enabled provider adapters; editorial links are not live offers. No v1 payment, reservation execution, price locks or guaranteed total when costs are unknown. Voice is v1.1. No new roadmap is introduced here.

## Constraints and principles
- Next.js frontend; FastAPI API; module-owned backend folders; independent Lambda ZIP builds; AWS SAM and changed-module CI/CD.
- Lambda-first; no assumed ECR/ECS/EC2 deployment. Heavy workers must pass measured packaging/runtime gates.
- Aiven PostgreSQL/PostGIS authoritative state, S3 artifacts, Upstash Redis optional ephemeral cache/rate coordination.
- LangGraph owns execution; hosted LLM proposes reasoning/plans; LAYA proposes bounded decisions; deterministic Policy controls execution and hard truth/math.
- Use the minimum requested capability set. An accepted itinerary may call routing internally. A new trip suggested after media identification needs acceptance.
- No fabricated places, weather, hours, restrictions, prices, bookings or live traffic. Unknown means unknown.

## Seven-document loading guide
| File | Owns | Load when |
|---|---|---|
| PROJECT.md | Scope, goals, document map | Every task |
| REQUIREMENTS.md | Required behavior and acceptance IDs | Relevant requirement sections every task |
| ARCHITECTURE.md | Boundaries, folders and architectural decisions | Relevant component sections every task |
| TECHNICAL_SPEC.md | Packages, agents/tools, APIs, contracts, database, algorithms, security/error protocols | Backend/contract implementation; read the target section |
| UI_UX.md | Screens, interactions and visual system | Frontend work |
| DEVELOPMENT.md | Environment, phase gates, tests, CI/CD, operations, metrics and cost | Coding, verification or deployment |
| DIAGRAMS.md | Visual workflow/debug map | Workflow changes or debugging |

Do not feed all seven files in full for every edit. Start from this map, then open the relevant headings. Smaller file count alone does not prevent hallucinations: authoritative contracts, narrow scope and verification do.

## Coverage of the larger document list
| Original topics | Single owner here |
|---|---|
| Overview, scope, MVP | PROJECT.md |
| PRD, functional/non-functional requirements | REQUIREMENTS.md |
| Architecture, component boundaries, decisions/ADRs | ARCHITECTURE.md |
| System design, modules/tools, agents/memory, API, database, DTO/events, security and error handling | TECHNICAL_SPEC.md |
| Screens/user flows and design system | UI_UX.md |
| Setup/environments, phased build, tests/evaluation, CI/CD, infrastructure, observability, performance and cost | DEVELOPMENT.md |
| Visual execution, state branches and debugging | DIAGRAMS.md |

There is no separate ROADMAP.md, reference folder or duplicate contract source. The agreed phase plan remains in DEVELOPMENT.md.

## Authority and adoption
Current user instruction > existing repository AGENTS.md within its applicable scope > this baseline. Within these documents, each topic has the owner above; links replace duplicated definitions. Stop the affected implementation if two topics conflict, repair the owner and update dependent diagrams/tests. Never choose an older statement silently.

These seven documents supersede the earlier lowercase document package and historical PDF. Archive those outside the active agent context; do not keep competing requirements beside this set. Preserve repository AGENTS.md; update its doc links if needed rather than overwrite it. Paths use backend/src/functions/ consistently.

This is a pre-development specification. Phase 0 creates executable schemas. Live providers, Lambda sizes, models and production routing have not passed implementation tests. Gate details are in DEVELOPMENT.md.
