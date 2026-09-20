# SWENA Active Project Memory & Execution State

**Last Audited:** 2026-09-19  
**Current Git Commit:** `8b7592fae383df49a3706b5ec4d938499e6d6b6a` (Branch: `main`)  
**Audit Reference:** [`docs/IMPLEMENTATION-AUDIT.md`](../docs/IMPLEMENTATION-AUDIT.md)  
**Completion Roadmap:** [`docs/PRODUCTION-COMPLETION-PLAN.md`](../docs/PRODUCTION-COMPLETION-PLAN.md)  
**Agent Protocol:** [`GEMINI.md`](../GEMINI.md)

---

## 1. Verified Working Foundations (Current Reality)

* **Verification Pipeline (`./verify.sh`):** Passes 100% cleanly:
  * Backend Ruff linter: `All checks passed!`
  * Backend Mypy strict typecheck: `Success: no issues found in 45 source files`
  * Backend Pytest: `45 passed in 2.98s` (covering Decimal money math, Scrapling web extraction and evidence classification, ReportLab PDF generation, worker claiming, Lambda parity, support inquiries).
  * Frontend Next.js build: Turbopack compilation succeeds with all 15 routes generated cleanly in 1.2s.
  * Playwright E2E suite: `8 passed (8.5s)` in `frontend/e2e/app.spec.ts` verifying the Home Page, proxy protection, authenticated dashboard, benchmarks, public trips, session lifecycle, about page, and contact desk.
  * Marketing Visual Verification: `14 passed, 0 failed` in `frontend/scripts/verify_cinematic_marketing.js` with 17 high-resolution evidence screenshots in `docs/marketing/evidence/`.
* **Cinematic Marketing Platform (`frontend/src/app/page.tsx`):**
  * Creative thesis: *"The landscape becomes your journey."*
  * Typography: `Bodoni Moda` (`--font-serif`) + `Manrope` (`--font-sans`) self-hosted via `next/font/google`.
  * Editorial palette: Ink (`#142820`), Warm Paper (`#F3EFE6`), Secondary Paper (`#E8E1D4`), Pale Sage (`#BCC9AF`), Terracotta (`#C56C4D`), Forest Surface (`#15271F`).
  * Scene 1 (`HeroJourney.tsx` + `TerrainPointsCanvas.tsx`): Sticky `100svh` stage in `220svh` scroll, transition into framed landscape + animated SVG route (Bengaluru → Mysuru → Coorg → Wayanad) with named nodes; zero scroll hijacking; responsive natural-flow fallback.
  * Scene 2 (`DestinationFilmstrip.tsx`): Horizontal strip with 3 spreads (`western-ghats`, `rajasthan`, `konkan`) with direct corridor slug handoff into `/dashboard`.
  * Scene 3 (`EditorialPause.tsx`): Warm paper `#F3EFE6`, portrait coffee detail (`coorg-coffee-detail.jpg`), exact 44-word statement, corridor capsule.
  * Scene 4 (`TripExample.tsx`): Single typed fixture driving Pace toggle (`Unhurried`, `Balanced`, `In-Depth`), day timeline, itemized budget ledger with explicit unknowns (zero-coercion standard).
  * Scene 5 (`PracticalFAQ.tsx`): Calm thin-rule accordion answering 5 key questions honestly.
  * Scene 6 (`ClosingScene.tsx` + `Footer.tsx`): Full-bleed Kudle Beach sunset coastline (`konkan-sunset.jpg`), Bodoni display heading, non-custodial supplier handoff reassurance.
  * Dashboard Corridor Handoff: `frontend/src/app/dashboard/page.tsx` wrapped in `<Suspense>`, consumes `searchParams.get("corridor")` to pre-populate origin and destination stops.
  * Contact Desk Integrity: `frontend/src/components/marketing/ContactForm.tsx` submits authentic inquiries to backend `/api/v1/support/inquiries` with real UUID tickets; completely removed simulated `inq_` IDs.
  * Verified Geographic Assets: Replaced Western Ghats lantern image with verified Kolukkumalai tea hill landscape (GPS: 10.116700, 77.233300, CC BY-SA 4.0), Amber Fort marble courtyard (GPS: 26.985978, 75.850236, CC0), Hawa Mahal (GPS: 26.923733, 75.827056, CC BY-SA 4.0), Rock Hills Estate coffee detail (GPS: 12.277999, 75.712431, CC BY-SA 4.0), Kudle Beach sunset (GPS: 14.5298, 74.3160, CC BY-SA 4.0). Detailed in `docs/marketing/ASSET-MANIFEST.md` and `docs/marketing/IMPLEMENTATION-EVIDENCE.md`.
* **Scrapling Web Scraping Engine Integration (`backend/src/travel/infrastructure/scraping/`):**
  * Installed and integrated `scrapling` (v0.4.15) with `curl_cffi`, `playwright`, and `patchright`.
  * Created domain evidence models in [`backend/src/travel/domain/evidence.py`](file:///home/swyra/projects/travel-agent/backend/src/travel/domain/evidence.py) enforcing the 4-tier taxonomy (`LIVE_OFFER`, `INDICATIVE_SEARCH`, `EDITORIAL_DISCOVERY`, `ESTIMATED_MODEL`).
  * Implemented [`WebExtractionPort`](file:///home/swyra/projects/travel-agent/backend/src/travel/application/ports/web_extractor.py) and [`ScraplingExtractionAdapter`](file:///home/swyra/projects/travel-agent/backend/src/travel/infrastructure/scraping/scrapling_adapter.py) with CSS selector extraction, safe Indian Rupee `Decimal` price parsing, and zero-hallucination fail-closed resilience.
  * Verified with 5 comprehensive unit tests in [`backend/tests/unit/test_scrapling_extractor.py`](file:///home/swyra/projects/travel-agent/backend/tests/unit/test_scrapling_extractor.py).
* **Design & Motion:**
  * SWENA 2.0 Forest Night theme (`#0D1915`, `#15271F`, `#F7F7F2`, `#A9B8AD`, `#B7C9AD`) implemented in Tailwind v4 and Radix UI components.
  * GSAP 3D interactive corridor cards (`travel-card-3d.tsx`) with zero framer-motion dependencies; fully compliant with `prefers-reduced-motion`.
* **Map & Geographic Visualization (`mapcn` / `maplibre-gl`):**
  * Installed `maplibre-gl` (`^6.10.0`) in frontend dependencies.
  * Installed full official `mapcn` component suite at `frontend/src/components/ui/map.tsx` from `mapcn.dev` registry (Map, MapMarker, MarkerContent, MarkerPopup, MarkerTooltip, MarkerLabel, MapPopup, MapControls, MapRoute, MapArc, MapGeoJSON, MapClusterLayer).
  * Tested with zero TypeScript compilation errors.
* **Temporary Development Auth Bypass (Per User Instruction):**
  * In `frontend/src/proxy.ts`: Commented out `/dashboard` route interception and redirect.
  * In `frontend/src/components/providers/AuthProvider.tsx`: Commented out `/api/auth/me` network fetch; defaulted context to static user (`usr_swena_traveler`, "Karnataka Explorer", `traveler@swena.internal`).
  * In `frontend/src/app/api/auth/me/route.ts`: Commented out session cookie parsing and return static user payload.
  * Backend `backend/src/travel/runtime/fastapi/auth.py` defaults to unauthenticated fallback `00000000-0000-0000-0000-000000000001` (`traveler@swena.internal`).
* **Unified Tooling & Entrypoints:**
  * `./start.sh [all|backend|frontend]` with concurrent SIGINT/SIGTERM trap handlers.
  * Top-level `backend/main.py` and package-level `travel.main:app` entrypoints.
  * Next.js 16 route proxy in `frontend/src/proxy.ts`.

---

## 2. Completed Milestone C0: Reality & Contract Repair (2026-09-19)

1. **Reality Audit & Traceability Matrix:** Created [`docs/IMPLEMENTATION-AUDIT.md`](../docs/IMPLEMENTATION-AUDIT.md), cataloging 21 codebase defects and establishing the P01–P18 status matrix.
2. **End-to-End User Journeys:** Created [`docs/END-TO-END-JOURNEYS.md`](../docs/END-TO-END-JOURNEYS.md), specifying complete screen-to-server interaction contracts for J01 through J15.
3. **Production Completion Plan:** Created [`docs/PRODUCTION-COMPLETION-PLAN.md`](../docs/PRODUCTION-COMPLETION-PLAN.md), breaking work into dependency-ordered milestones C0–C8 with fully specified tasks (C1-T01, C1-T02, C2-T01).
4. **Visualization & Motion Contract:** Created [`docs/VISUALIZATION-AND-MOTION-SPEC.md`](../docs/VISUALIZATION-AND-MOTION-SPEC.md), defining GSAP animation budgets, responsive layouts, and WebGPU optional rules.
5. **Test & Release Plan:** Created [`docs/TEST-AND-RELEASE-PLAN.md`](../docs/TEST-AND-RELEASE-PLAN.md), detailing 8 verification layers, 50-case benchmark requirements, and 8 release gates.
6. **Operations Runbook:** Created [`docs/OPERATIONS-RUNBOOK.md`](../docs/OPERATIONS-RUNBOOK.md), documenting recovery procedures, incident response, and contact inquiry triage.
7. **Gemini CLI Entrypoint:** Created [`GEMINI.md`](../GEMINI.md) at the repository root.
8. **Specification Reconciliation:** Updated and aligned all 16 specification and governance documents in place (`README.md`, `docs/PRD.md`, `docs/TRD.md`, `docs/ARCHITECTURE.md`, `docs/UI-UX-DESIGN-BRIEF.md`, `docs/DATABASE-SCHEMA.md`, `docs/API-SPEC.md`, `docs/AI-LLM-SPEC.md`, `docs/SECURITY.md`, `docs/DEPLOYMENT.md`, `docs/PHASES.md`, `docs/SOURCES.md`, `docs/PROVIDER-REGISTRY.json`, `tests/evaluations/cases.json`, `.agent/RULES.md`, `.agent/BOUNDARIES.md`, `.agent/WORKFLOW.md`, `.agent/DECISIONS.md`).

---

## 3. Active Milestone & Immediate Implementation Tasks

* **Active Milestone:** **Milestone C1 — Identity, Ownership & Data Integrity**
* **Immediate Next Task:** **`C1-T01` — Cryptographically Validated Identity & Fail-Closed Session Boundary**
  * *Objective:* Eliminate `simulateLogin()` in `AuthProvider.tsx`; replace unverified base64 decoding in `frontend/src/app/api/auth/me/route.ts` with `jose` RS256 JWKS signature verification; enforce fail-closed redirect on expired or forged tokens.
* **Subsequent Task:** **`C1-T02` — Server-Derived Ownership Enforcement**
  * *Objective:* Remove client-supplied `owner_id` from `CreateTripRequest`; inject `get_current_user` auth dependency on all FastAPI `/api/v1/trips/*` routes; enforce `trip.owner_id == current_user.id` (return 404 for unowned resources).

---

## 4. Open Integration & Release Gates

1. **Gate 1 (Auth Barrier):** Cryptographic token verification and server ownership isolation pending (Milestone C1).
2. **Gate 2 (Solver Soundness):** Time windows, round-trip depot return leg, and 50 scenario-specific invariants pending (Milestone C3).
3. **Gate 3 (Display Rights):** Google Places and Mapbox usage verified against commercial terms; POI attribution tags pending.
4. **Gate 4 (Merchant Handoff):** Direct deep-link handoff allowlists and ReportLab PDF S3 streaming verification pending.
5. **Gate 5 (Privacy & Sharing):** Dynamic public route projection on `/trips/[id]` and DPDP deletion cascade pending.
6. **Gate 6 (Transparency):** Web Speech API disclosure and meteorological weather feeds pending.
7. **Gate 7 (Operational Readiness):** Durable PostgreSQL contact inquiry ingestion pending.
8. **Gate 8 (Production Deployment):** ECS container deployment, automated PITR restore drills, and SLO load verification pending.
