# SWENA Project Build Phases & Implementation Roadmap

**Version:** 2.0  
**Status:** Reconciled Engineering Roadmap  
**Structure:** Preserves Historical Stages (Stages 0–6 and Later.1–Later.6) and establishes the canonical Production Completion Milestones (C0–C8).

---

## 1. Historical Engineering Stages (Retrospective Record)

The following stages represent the initial prototype development and architectural exploration phases:

* **Stage 0 — Documentation Baseline:** Initial requirements, architectural decisions D001–D017, and provider registry.
* **Stage 1 — Domain Foundation:** Typed domain models, Pydantic contracts, Decimal monetary arithmetic, initial SQLAlchemy repositories, and Alembic migrations (`0001_initial_schema.py`).
* **Stage 2 — Workflows & Scheduling:** Initial FastAPI routes, LangGraph prototype graph, and OR-Tools TSP scheduler.
* **Stage 3 — Frontend Workspace:** Next.js 16 setup, Tailwind CSS v4, multi-tab planning console, Leaflet map canvas, and ReportLab PDF export.
* **Stage 4 — Portable Jobs:** Background job schema, worker claiming loops, and ReportLab PDF export service (`0002_add_artifacts.py`).
* **Stage 5 — Acceptance Scaffolding:** Initial test harnesses, mock test scripts, and local Docker compose profiles.
* **Stage 6 — EC2 Profile:** Local and containerized entrypoints (`start.sh`, `Makefile`).
* **Stage Later (Prototypes):**
  * *Later.1:* 50-scenario benchmark runner (`travel.benchmarks.runner`).
  * *Later.2:* Leaflet geospatial corridor canvas with CartoDB Dark Matter styling.
  * *Later.3:* Client-native Web Speech API voice brief assistant.
  * *Later.4:* Corridor weather & Western Ghats hazard advisory card.
  * *Later.5:* Public shareable route `/trips/[id]` and client-side SVG QR code generator.
  * *Later.6:* Next.js BFF OAuth 2.1 integration with SWYRA Auth.

---

## 2. Canonical Production Completion Milestones (C0 through C8)

As documented in [`docs/IMPLEMENTATION-AUDIT.md`](IMPLEMENTATION-AUDIT.md), initial prototype implementations relied on client-side math fallbacks, unverified scenario loops, and mock identity fallbacks. Milestones **C0 through C8** represent the official, dependency-ordered engineering plan to transition the platform into an operational, verifiable production release:

```
[ C0: Reality & Contract Repair ] (Completed)
   │
   ▼
[ C1: Identity, Ownership & Data Integrity ] (Active Milestone)
   │
   ▼
[ C2: Durable Planning Vertical Slice ]
   │
   ▼
[ C3: Routing Engine, Discovery & Evidence Classification ]
   │
   ▼
[ C4: Complete Multi-Tab Planning Workspace ]
   │
   ▼
[ C5: Sharing, Export Artifacts, Handoff & Privacy ]
   │
   ▼
[ C6: Cinematic Marketing Experience & Support Inquiries ]
   │
   ▼
[ C7: Regional Weather, Voice Briefing & Dynamic In-Trip Replanning ]
   │
   ▼
[ C8: Staging Deployment, SLO Verification & Release Governance ]
```

### Milestone C0 — Reality & Contract Repair (Status: Completed)
* **Scope:** Full codebase reality audit, defect inventory, reconciliation of specifications, creation of companion specs (`IMPLEMENTATION-AUDIT.md`, `END-TO-END-JOURNEYS.md`, `PRODUCTION-COMPLETION-PLAN.md`, `VISUALIZATION-AND-MOTION-SPEC.md`, `TEST-AND-RELEASE-PLAN.md`, `OPERATIONS-RUNBOOK.md`, `GEMINI.md`).
* **Exit Gate:** Complete traceability matrix covering P01–P18 and all companion specifications approved.

### Milestone C1 — Identity, Ownership & Data Integrity (Status: Active Milestone)
* **Scope:** Cryptographically verified OAuth 2.1 JWT sessions (`jose` RS256 validation against JWKS); removal of client `owner_id` fields; strict server-derived ownership enforcement on all trip routes; removal of `simulateLogin()` mock states.
* **Exit Gate:** Two-user cross-tenant integration test proves zero unauthorized resource access; forged tokens strictly return 401/404; real login survives page reloads.

### Milestone C2 — Durable Planning Vertical Slice
* **Scope:** True asynchronous job execution; worker claims job and executes LangGraph; frontend consumes server plan response; complete removal of client-side Haversine simulation in dashboard.
* **Exit Gate:** Re-solve updates canonical database plan; worker restart preserves running jobs; version advances cleanly $v1 \to v2$.

### Milestone C3 — Routing Engine, Discovery & Evidence Classification
* **Scope:** Real road distance/duration matrices; OR-Tools time-window and return-leg formulation; strict four-tier evidence normalization; comprehensive overhaul of 50 benchmark cases enforcing scenario-specific invariants.
* **Exit Gate:** 50/50 benchmark cases pass with unique, scenario-specific assertions; zero tautological looping.

### Milestone C4 — Complete Multi-Tab Planning Workspace
* **Scope:** Synchronized Leaflet map and monotonic timeline; differential comparison view on edits; atomic approval transactions; full WCAG 2.1 AA keyboard accessibility.
* **Exit Gate:** Stop edits recompute adjacent legs; approval commits version atomically; 409 Conflict handled cleanly.

### Milestone C5 — Sharing, Export Artifacts, Handoff & Privacy
* **Scope:** Dynamic public trip projection reading from database; instant share link revocation; verified ReportLab PDF generation; domain allowlist handoffs; DPDP/GDPR account deletion cascade.
* **Exit Gate:** Anonymous visitor views sanitized public route without private PII; revoking link returns immediate 404; account deletion cascade clears all stores.

### Milestone C6 — Cinematic Marketing Experience & Support Inquiries
* **Scope:** Editorial GSAP 3D corridor discovery on marketing home; authentic value proposition copy; durable contact inquiry ingestion with PostgreSQL storage.
* **Exit Gate:** Contact form submission persists row to `contact_inquiries` table; GSAP animations satisfy `prefers-reduced-motion`.

### Milestone C7 — Regional Weather, Voice Briefing & Dynamic In-Trip Replanning
* **Scope:** Verified meteorological weather feeds; client-native Web Speech dictation with explicit browser cloud disclosure; dynamic in-trip replanning for remaining stops.
* **Exit Gate:** Web Speech UI explicitly notifies user of browser cloud streaming; in-trip replan freezes completed stops.

### Milestone C8 — Staging Deployment, SLO Verification & Release Governance
* **Scope:** Production ECS container packaging; automated recovery drills; load testing verifying P95 SLOs; complete release gate sign-offs.
* **Exit Gate:** P95 first useful map $\le 3.5$s; P95 plan compilation $\le 20$s; all 8 release gates signed off with evidence.
