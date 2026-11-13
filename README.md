# SWENA — Sovereign Travel Intelligence Platform

> **Status:** Active Development (spec 0007 — trip planner end-to-end).  
> **Tagline:** *"We gave you memory."*  
> **Repository:** `SGOD-pro/travel-agent`  
> **Architecture:** Next.js 16 (App Router) + FastAPI (Modular Monolith) + PostgreSQL 16 / PostGIS + Redis

---

## 1. Product Summary & Architecture

SWENA is a deterministic, India-first travel planning, comparison, discovery, and official merchant handoff platform. It unifies travel briefs, multi-modal transit comparisons (petrol car, motorcycle, rail, bus, air), constraint-based scheduling via Google OR-Tools, transparent financial arithmetic with zero unknown-cost coercion, and unmediated deep-link handoffs to official booking portals (IRCTC, airlines, state tourism corporations).

### Core Boundaries & Invariants
1. **Zero Hallucination:** Zero fabricated prices, fake seat availability, fictitious flight schedules, or synthetic booking confirmations.
2. **Deterministic Financial Math:** All money uses Decimal. Unknown highway tolls and entry permits are preserved as explicit unknowns; they are never coerced to ₹0 to claim false budget compliance.
3. **Canonical Authority:** PostgreSQL is the single source of truth for trips, versions, and jobs. The backend server holds mathematical and scheduling authority; frontend consumes server plans directly.
4. **Non-Custodial Handoff:** SWENA operates zero internal checkout, holds zero customer payments, and creates zero bookings. Fulfillment transfers travelers directly to official merchant portals.
5. **Fail-Closed Security:** User authorization is derived strictly from cryptographically verified Bearer tokens issued by [SWYRA Auth](https://github.com/SGOD-pro/OAuth2.1). Client-supplied owner_id fields are rejected.

---

## 2. Honest Capability Matrix

| Subsystem / Capability | Implementation Reality | Evidence & Test Verification | Audit Status |
| :--- | :--- | :--- | :--- |
| **Durable Execution Engine** | PostgreSQL persistence, worker lease fencing, transactional outbox relay, crash recovery, and background daemon supervision. | `backend/tests/unit/test_daemon_supervision.py`, integration suite (spec 0001). | `VERIFIED_LOCAL` |
| **Evidence Collection** | SerpAPI search, permitted page extraction, SSRF defense, canonical place resolution, deterministic claim policy engine. | `backend/tests/unit/test_serpapi_adapter.py`, `backend/tests/unit/test_ssrf_protection.py`, `backend/tests/unit/test_claim_policy_engine_unit.py`, `backend/tests/unit/test_places_resolution_unit.py`. | `VERIFIED_LOCAL` |
| **Browser Escalation** | Fenced dynamic rendering for JS-heavy pages with immutable observation persistence. | `backend/tests/unit/test_browser_escalation_slice2.py`, `backend/tests/unit/test_browser_network_and_driver_mocked.py`. | `VERIFIED_LOCAL` |
| **Constraint Solver** | OR-Tools stop-order solving inside the route optimizer state machine. | `backend/tests/unit/test_route_optimizer_slice1.py` and slice 3–7 suites. | `VERIFIED_LOCAL` |
| **Multi-modal Route Math** | Fuel volume/cost, rail/bus/air leg costing, restriction-aware polygons (PostGIS). | `backend/tests/unit/test_route_optimizer_slice5.py`, `backend/tests/integration/test_canonical_places_spatial.py`. | `VERIFIED_LOCAL` |
| **PDF Itinerary Export** | ReportLab publication generator with versioning and disclaimers. | `backend/tests/unit/test_route_optimizer_slice7.py`. | `VERIFIED_LOCAL` |
| **Itinerary API & Ownership** | Owner-authorized itinerary endpoints and Next.js proposal view (spec 0007 slice 6). | `backend/tests/unit/test_trip_itinerary_api_slice6.py`, `frontend/e2e/itinerary-view.spec.ts`. | `VERIFIED_LOCAL` |
| **Next.js Frontend & UI** | Next.js 16 (Turbopack), Tailwind v4, GSAP scroll choreography, Leaflet route maps. | `frontend/e2e/route-optimization-e2e.spec.ts` and itinerary suites. | `VERIFIED_LOCAL` |
| **Route Protection Proxy** | frontend/src/proxy.ts intercepts /dashboard routes when session cookie is absent. | Covered by the frontend E2E suite. | `VERIFIED_LOCAL` |
| **OAuth 2.1 BFF Flow** | Next.js BFF handlers (frontend/src/app/api/auth/*), PKCE S256, HttpOnly cookies. | `backend/tests/unit/test_auth_real.py`; JWKS signature validation in progress. | `PARTIAL` |
| **Public Itinerary Sharing** | Dynamic route /trips/[id] and mobile QR code generation. | Route renders fixture-backed data; database projection in progress. | `DEMO_ONLY` |
| **Benchmark Suite** | Scenario runner at backend/src/functions/api/benchmarks/runner.py driven by tests/evaluations/cases.json. | Currently runs a synthetic trip loop; per-scenario assertions underway. | `PARTIAL` |
| **External Live Providers** | Adapters for SerpAPI, Open-Meteo, AWS Bedrock, and official booking portals. | Fixture-backed adapters tested; live credentials not provisioned. | `DISABLED_UNVERIFIED` |

---

## 3. Quick Start & Setup Instructions

### 3.1 Prerequisites
- **Python:** 3.12+ with [uv](https://github.com/astral-sh/uv) package manager installed.
- **Node.js:** 20+ with npm installed.
- **Database (Optional for pure unit tests; required for integration):** Docker with PostgreSQL 16 (PostGIS) and Redis 7.

### 3.2 Environment Configuration
```bash
# Backend environment setup
cp backend/.env.example backend/.env

# Frontend environment setup
cp frontend/.env.example frontend/.env.local
```

### 3.3 Starting Local Development Servers
```bash
# Option 1: Start both Backend (port 8000) and Frontend (port 3000) concurrently:
./start.sh all
# or via Makefile:
make run-all

# Option 2: Run servers individually:
./start.sh backend # FastAPI at http://localhost:8000 (Swagger: /docs)
./start.sh frontend # Next.js at http://localhost:3000
```

### 3.4 Local Infrastructure
```bash
make local-up # PostgreSQL 16 (PostGIS) + Redis 7 via deployment/local/docker-compose.yml
make backend-migrate
```

---

## 4. Verification & Testing Commands
```bash
# 1. Run the master verification pipeline (Lint + Mypy + Pytest + Next.js Build + Playwright)
./verify.sh

# 2. Run backend checks only:
cd backend && uv run ruff check . && uv run mypy src && uv run pytest tests/unit/ -v

# 3. Run frontend production build & Playwright E2E tests:
cd frontend && npm run build && npx playwright test
```

---

## 5. Documentation Directory
All documentation lives under [docs/](docs/). Read in this order:

| Document | Purpose |
| :--- | :--- |
| [docs/PROJECT.md](docs/PROJECT.md) | Project baseline and current positioning. |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | User behavior and acceptance requirements. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Component ownership, module boundaries, folder layout. |
| [docs/TECHNICAL_SPEC.md](docs/TECHNICAL_SPEC.md) | Implementation-level technical specification. |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | How to build, test, and operate one vertical slice. |
| [docs/DIAGRAMS.md](docs/DIAGRAMS.md) | Workflow diagrams and development debug map. |
| [docs/UI-UX-DESIGN-BRIEF.md](docs/UI-UX-DESIGN-BRIEF.md) | SWENA sensory design system and motion spec. |
| [docs/reference/API.md](docs/reference/API.md) | Public HTTP contract. |
| [docs/reference/DATABASE.md](docs/reference/DATABASE.md) | Persistence and schema reference. |
| [docs/scope/scope.md](docs/scope/scope.md) | Launch scope and non-goals. |

### Numbered Specs
Vertical slice specifications, each with index, rationale, and verification notes:
- [docs/specs/0001-orchestrator-execution-engine.md](docs/specs/0001-orchestrator-execution-engine.md) — durable execution engine
- [docs/specs/0002-evidence-collection-and-verification.md](docs/specs/0002-evidence-collection-and-verification.md) — evidence collection & claim verification
- [docs/specs/0003-browser-escalation.md](docs/specs/0003-browser-escalation.md) — browser escalation worker
- [docs/specs/0004-remote-laya-decision-router.md](docs/specs/0004-remote-laya-decision-router.md) — LAYA decision router
- [docs/specs/0005-route-optimizer.md](docs/specs/0005-route-optimizer.md) — multi-modal route optimizer
- [docs/specs/0006-route-graph-and-map-e2e.md](docs/specs/0006-route-graph-and-map-e2e.md) — route graph & map end-to-end
- [docs/specs/0007-trip-planner-e2e/index.md](docs/specs/0007-trip-planner-e2e/index.md) — trip planner end-to-end (in progress)

Dated slice reviews live in [docs/reviews/](docs/reviews/); superseded historical documents are in [docs/archive/](docs/archive/) and [archive/](archive/) (see [archive/MANIFEST.md](archive/MANIFEST.md)).

### Source Precedence
Explicit user decisions recorded in reviews supersede older historical documents (including the historical `multi_agent_travel_planner_docs.pdf`). Supabase, Stripe, internal checkout, and universal price locks are explicitly superseded.

---

## 6. Current Implementation Focus
- **Active spec:** [docs/specs/0007-trip-planner-e2e/index.md](docs/specs/0007-trip-planner-e2e/index.md) — trip planner end-to-end (itinerary API, proposal view, invariant badges).
- **Changelog:** See [CHANGELOG.md](CHANGELOG.md) for the complete, dated delivery log per spec.
- **AI assistant rules of engagement:** [AGENTS.md](AGENTS.md) (loaded by [CLAUDE.md](CLAUDE.md)).
