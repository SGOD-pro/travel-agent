# Travel Planning Platform

Documentation baseline v1.0 — 2026-09-12. Status: pre-implementation specification, not a working application.

India-first travel planning, comparison, discovery, and official external booking handoff. Product intent: help travelers create lasting memories. Working tagline supplied by the owner: “we gave you memory”. Final public naming/copy remains editable.

One repository, one Next.js frontend, one shared Python modular backend, and thin HTTP, worker, CLI and Lambda entrypoints. ECS is the primary long-running runtime; Docker/EC2 use the same container application. Lambda boundaries evolve from measurements rather than feature count.

## Read in this order

1. [PRD](docs/PRD.md): scope and product acceptance.
2. [Architecture](docs/ARCHITECTURE.md) and [TRD](docs/TRD.md): boundaries and technical requirements.
3. [Database schema](docs/DATABASE-SCHEMA.md), [API specification](docs/API-SPEC.md), [AI specification](docs/AI-LLM-SPEC.md).
4. [UI/UX brief](docs/UI-UX-DESIGN-BRIEF.md), [Security](docs/SECURITY.md), [Deployment](docs/DEPLOYMENT.md).
5. [Build phases](docs/PHASES.md), [provider registry](docs/PROVIDER-REGISTRY.json), [evaluation cases](tests/evaluations/cases.json).
6. Agent contributors read [.agent/RULES.md](.agent/RULES.md), [BOUNDARIES](.agent/BOUNDARIES.md), [DECISIONS](.agent/DECISIONS.md), [MEMORY](.agent/MEMORY.md), and [WORKFLOW](.agent/WORKFLOW.md).

## Selected stack

Next.js/TypeScript; Python 3.12 baseline, FastAPI, Pydantic v2, LangGraph, OR-Tools; Aiven PostgreSQL/PostGIS; Upstash Redis; S3; Bedrock behind an LLM interface; Mapbox and OSRM routing adapters; Google Places adapter; SerpAPI and permitted Scrapling extraction. Dependency versions must be compatibility-tested and pinned during foundation work. No Supabase, Qdrant or initial pgvector.

SWYRA Auth is an external identity candidate with separate MongoDB infrastructure. It is not part of the travel application's single-store rule. Production use requires security acceptance.

## Current state

This pack contains specifications and test scenario definitions only. No application, credentials, database migrations, API integrations, runnable tests, or deployments exist. Do not invent launch commands. The registry deliberately reports unverified provider access.

EV energy/range/charging planning is deferred for both e-bikes and electric cars. Petrol motorcycle/car estimates and ordinary bicycle routing remain in scope. An electric vehicle must not silently receive a petrol cost model.

## Source precedence

Latest explicit user decisions supersede the attached original PDF. The PDF's internal payments, Stripe, Supabase, universal price holds, historical cost estimates and unsupported competitor assertions are not current requirements. [DECISIONS](.agent/DECISIONS.md) records supersession.

Map-provider display/retention rights and actual provider access remain integration gates, not excuses to delay provider-independent foundation work. See [Sources](docs/SOURCES.md).
