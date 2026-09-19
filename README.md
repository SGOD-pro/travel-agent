# SWENA — Sovereign Travel Intelligence Platform

> **Status:** Active Development & Stabilization (Milestone C1).  
> **Tagline:** *"We gave you memory."*  
> **Repository:** `SGOD-pro/travel-agent`  
> **Architecture:** Next.js 16 (App Router) + FastAPI (Modular Monolith) + PostgreSQL 16 / PostGIS (Aiven) + Upstash Redis

---

## 1. Product Summary & Architecture

SWENA is a deterministic, India-first travel planning, comparison, discovery, and official merchant handoff platform. It unifies travel briefs, multi-modal transit comparisons (petrol car, motorcycle, rail, bus, air), constraint-based scheduling via Google OR-Tools, transparent financial arithmetic with zero unknown-cost coercion, and unmediated deep-link handoffs to official booking portals (IRCTC, airlines, state tourism corporations).

### Core Boundaries & Invariants
1. **Zero Hallucination:** Zero fabricated prices, fake seat availability, fictitious flight schedules, or synthetic booking confirmations.
2. **Deterministic Financial Math:** All money uses `Decimal`. Unknown highway tolls and entry permits are preserved as explicit unknowns; they are never coerced to ₹0 to claim false budget compliance.
3. **Canonical Authority:** PostgreSQL is the single source of truth for trips, versions, and jobs. The backend server holds mathematical and scheduling authority; frontend consumes server plans directly.
4. **Non-Custodial Handoff:** SWENA operates zero internal checkout, holds zero customer payments, and creates zero bookings. Fulfillment transfers travelers directly to official merchant portals.
5. **Fail-Closed Security:** User authorization is derived strictly from cryptographically verified Bearer tokens issued by [SWYRA Auth](https://github.com/SGOD-pro/OAuth2.1). Client-supplied `owner_id` fields are rejected.

---

## 2. Honest Capability Matrix

| Subsystem / Capability | Implementation Reality | Evidence & Test Verification | Audit Status |
| :--- | :--- | :--- | :--- |
| **Financial Arithmetic** | Strict Decimal math, currency checking, zero-coercion unquoted toll preservation. | `backend/tests/unit/test_money.py` (8 passing unit tests). | `VERIFIED_LOCAL` |
| **Road Transit Math** | Fuel volume/cost formulas based on distance, mileage, and fuel price. | `backend/tests/unit/test_routing.py` (2 passing unit tests). | `VERIFIED_LOCAL` |
| **PDF Itinerary Export** | ReportLab publication generator with versioning, disclaimers, and S3 artifact tracking. | `backend/tests/unit/test_pdf_export.py`, `test_export_api.py`. | `VERIFIED_LOCAL` |
| **Constraint Solver** | OR-Tools TSP scheduler with monotonic timestamp sequencing. | `backend/tests/unit/test_scheduler.py` (Passes sequencing; time windows in progress). | `PARTIAL` |
| **Next.js Frontend & UI** | Next.js 16 (Turbopack), Tailwind v4, SWENA 2.0 Forest Night theme, GSAP 3D corridor cards. | `frontend/e2e/app.spec.ts` (Playwright suite passes). | `VERIFIED_LOCAL` |
| **Route Protection Proxy** | Next.js 16 `src/proxy.ts` intercepting `/dashboard` routes when session cookie is absent. | `frontend/e2e/app.spec.ts` (Test 2 passes). | `VERIFIED_LOCAL` |
| **OAuth 2.1 BFF Flow** | Next.js BFF handlers (`/api/auth/*`), PKCE S256, HttpOnly cookies, fail-closed diagnostics. | Handlers implemented; cryptographic JWKS signature validation in progress (`C1-T01`). | `PARTIAL` |
| **Server Ownership Gate** | Deriving user ID from token; multi-tenant trip isolation. | Endpoints currently accept client `owner_id`; authorization enforcement underway (`C1-T02`). | `PARTIAL` |
| **Public Itinerary Sharing** | Dynamic route `/trips/[id]` and mobile QR code generation. | Route currently renders static Coorg fixture; dynamic database projection underway (`C5-T01`). | `DEMO_ONLY` |
| **Benchmark Suite (50 Cases)**| 50-scenario runner in `backend/src/travel/benchmarks/runner.py`. | Currently runs single synthetic trip loop; scenario-specific invariant assertions underway (`C3-T04`).| `PARTIAL` |
| **External Live Providers** | Adapters for IRCTC, Mapbox, Google Places, SerpAPI. | All external providers disabled in `docs/PROVIDER-REGISTRY.json` pending live licensing. | `DISABLED_UNVERIFIED` |

---

## 3. Quick Start & Setup Instructions

### 3.1 Prerequisites
* **Python:** 3.12+ with [`uv`](https://github.com/astral-sh/uv) package manager installed.
* **Node.js:** 20+ with `npm` installed.
* **Database (Optional for pure unit tests; required for integration):** Docker with PostgreSQL 16 (PostGIS) and Redis.

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
./start.sh backend    # FastAPI at http://localhost:8000 (Swagger: /docs)
./start.sh frontend   # Next.js at http://localhost:3000
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

## 5. Documentation Directory & Source Precedence

Follow the documentation hierarchy in sequence:
1. **Audit & Reality:** [`docs/IMPLEMENTATION-AUDIT.md`](docs/IMPLEMENTATION-AUDIT.md) — Current code reality, defects, and traceability.
2. **Product Requirements:** [`docs/PRD.md`](docs/PRD.md) — User requirements P01–P18, launch boundaries, and non-goals.
3. **End-to-End Journeys:** [`docs/END-TO-END-JOURNEYS.md`](docs/END-TO-END-JOURNEYS.md) — Screen-to-server contracts for J01 through J15.
4. **Completion Plan:** [`docs/PRODUCTION-COMPLETION-PLAN.md`](docs/PRODUCTION-COMPLETION-PLAN.md) — Atomic vertical backlog (Milestones C0–C8).
5. **Visual & Motion Spec:** [`docs/VISUALIZATION-AND-MOTION-SPEC.md`](docs/VISUALIZATION-AND-MOTION-SPEC.md) — GSAP motion, responsive matrix, accessibility.
6. **Testing & Release:** [`docs/TEST-AND-RELEASE-PLAN.md`](docs/TEST-AND-RELEASE-PLAN.md) — Invariants, 50 benchmark cases, release gates.
7. **Operations & Incident Response:** [`docs/OPERATIONS-RUNBOOK.md`](docs/OPERATIONS-RUNBOOK.md) — Triage, recovery protocols, and support pipelines.
8. **Agent Protocol:** [`GEMINI.md`](GEMINI.md) and [`.agent/RULES.md`](.agent/RULES.md) — Rules of engagement for AI coding assistants.

### Source Precedence
Explicit user decisions in [`.agent/DECISIONS.md`](.agent/DECISIONS.md) supersede all historical documents (including the historical `multi_agent_travel_planner_docs.pdf`). Supabase, Stripe, internal checkout, and universal price locks are explicitly superseded.

---

## 6. Current Implementation Task

* **Current Milestone:** **Milestone C1 — Identity, Ownership & Data Integrity**
* **Active Task:** **`C1-T01`** (Cryptographically Validated Identity & Fail-Closed Session Boundary).
