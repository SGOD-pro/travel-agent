# SWENA Production Completion Plan & Vertical Task Backlog

**Version:** 1.0  
**Status:** Executable Implementation Backlog  
**Governing Standard:** Dependency-ordered vertical slices from UI $\to$ API $\to$ Domain $\to$ Database $\to$ Verification.

---

## 1. Milestone Roadmap (C0 through C8)

```
[ C0: Reality & Contract Repair ]
   │
   ▼
[ C1: Identity, Ownership & Data Integrity ]
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

| Milestone ID | Milestone Title | Primary Outcome | Mandatory Exit Evidence |
| :--- | :--- | :--- | :--- |
| **C0** | **Reality & Contract Repair** | Complete audit of current codebase against PRD; repair documentation; establish canonical contracts and test standards. | `docs/IMPLEMENTATION-AUDIT.md`, `END-TO-END-JOURNEYS.md`, `PRODUCTION-COMPLETION-PLAN.md` created; README and spec discrepancies resolved. |
| **C1** | **Identity, Ownership & Data Integrity** | Cryptographically validated OAuth 2.1 sessions; server-derived user identity; ownership authorization on all trip resources; elimination of mock identities and forged tokens. | Forged JWT and cross-user read/write attempts fail closed with 401/404; real login survives page reload; client `owner_id` removed from wire payloads. |
| **C2** | **Durable Planning Vertical Slice** | True async planning execution; background worker owns LangGraph graph execution; client consumes server plan response; eliminates client-side Haversine simulation. | Worker restart preserves running jobs; frontend renders server timetable without local math fallback; version advances cleanly $v1 \to v2$. |
| **C3** | **Routing Engine, Discovery & Evidence** | Real road distance/duration matrices; OR-Tools time-window and return-leg formulation; strict 4-tier evidence categorization; honest 50-scenario benchmark assertions. | All 50 benchmark cases evaluate scenario-specific invariants; motorcycle vs. bicycle routing profile distinction verified; unverified tolls preserved. |
| **C4** | **Complete Planning Workspace** | Fully synchronized Leaflet map and monotonic timeline; differential comparison view on edits; atomic approval transaction; keyboard accessibility compliance. | Editing a stop recomputes adjacent legs only; accepting delta increments version atomically; stale approval attempts return 409 Conflict. |
| **C5** | **Sharing, Exports, Handoff & Privacy** | Public trip projections strictly sanitized; revocable high-entropy share tokens; asynchronous ReportLab PDF generation; domain allowlist handoff; cascade data deletion. | Anonymous visitor views sanitized public route without private PII; revoking link returns immediate 404; PDF streams valid bytes with toll disclaimers. |
| **C6** | **Cinematic Marketing & Support** | Editorial GSAP 3D corridor discovery on marketing home; authentic value proposition copy; durable contact inquiry ingestion with PostgreSQL storage. | Contact form submission persists row to `contact_inquiries` table; GSAP animations satisfy `prefers-reduced-motion`; zero fake confirmation toasts. |
| **C7** | **Weather, Voice & In-Trip Recovery** | Verified meteorological weather feeds; client-native Web Speech dictation with explicit browser cloud disclosure; dynamic in-trip replanning for remaining stops. | Web Speech UI explicitly notifies user of browser cloud streaming; replanning en-route freezes completed stops and routes remaining stops around hazards. |
| **C8** | **Staging, SLOs & Release Governance** | Containerized ECS deployment profile; automated recovery drills; load testing verifying P95 SLOs; complete release gate sign-offs. | P95 first useful map $\le 3.5$s; P95 plan compilation $\le 20$s; disaster recovery drill succeeds; all 8 release gates signed off with evidence. |

---

## 2. Fully Specified Initial Implementation Tasks (C1-T01, C1-T02, C2-T01)

### Task C1-T01: Cryptographically Validated Identity & Fail-Closed Session Boundary

* **ID and Title:** `C1-T01`: Cryptographically Validated Identity & Fail-Closed Session Boundary
* **Requirement IDs & User Outcome:** P18, J02. User authenticates via SWYRA Auth; Next.js BFF validates RS256 token signatures against remote JWKS; forged, expired, or malformed tokens are rejected with 401; mock login methods are eradicated.
* **Current Behavior & Evidence:**
  * `frontend/src/components/providers/AuthProvider.tsx` contains `simulateLogin()` installing mock user `usr_swena_default_traveler` (lines 49–56).
  * `frontend/src/app/api/auth/me/route.ts` decodes base64 JWT payload without verifying cryptographic signature against JWKS (lines 16–23).
  * `frontend/src/proxy.ts` only checks that cookie string is non-empty.
* **Dependencies:** None (First task in Milestone C1).
* **Files / Modules to Inspect:**
  * `frontend/src/components/providers/AuthProvider.tsx`
  * `frontend/src/app/api/auth/me/route.ts`
  * `frontend/src/app/api/auth/callback/route.ts`
  * `frontend/src/proxy.ts`
  * `backend/src/travel/runtime/fastapi/auth.py`
* **Exact Behavior to Add / Change:**
  1. In `AuthProvider.tsx`: Remove `simulateLogin()` entirely. `login()` must initiate real browser redirect to `/api/auth/login`.
  2. In `frontend/src/app/api/auth/me/route.ts`: Use `jose` library (`jwtVerify`) to validate token signature against `JWKS_URL` (`/.well-known/jwks.json`) with algorithm `RS256`, audience binding `CLIENT_ID`, and issuer verification `AUTH_ISSUER`.
  3. If token signature fails, token is expired, or JWKS is unreachable: Clear the invalid `swena_session` cookie and return `401 Unauthorized` with `{ "user": null }`.
  4. In `frontend/src/app/api/auth/callback/route.ts`: Enforce strict state verification against `oauth_state` cookie; enforce presence of PKCE `code_verifier`. Fails closed if state or verifier is missing.
* **Contracts & Migrations Affected:** No database schema migration required. Affects Next.js BFF authentication contracts.
* **Failure, Partial & Recovery Behavior:**
  * If identity server is down, `/login` renders clear alert: "Identity Provider temporarily unavailable. Please try again shortly." Zero synthetic user generation.
* **Security & Data-Rights Constraints:** Zero storage of client secrets in browser; session cookie marked `HttpOnly; Secure; SameSite=Lax`.
* **Tests / Fixtures & Commands:**
  * Unit test verifying `jose` token signature rejection on altered payload.
  * E2E test verifying that injecting a fake base64 string into `swena_session` cookie results in immediate redirect to `/login`.
  * Command: `cd frontend && npm run build && npx playwright test e2e/app.spec.ts`
* **Acceptance Evidence:**
  * Forged JWT token submitted to `/api/auth/me` returns 401 with `{ "user": null }`.
  * `simulateLogin` method completely removed from frontend codebase.
* **Explicit Non-Goals:** Creating local username/password registration tables (SWYRA Auth owns user credentials).
* **Rollback / Compatibility Notes:** Backwards compatible with legitimate tokens issued by SWYRA Auth.
* **External Blocker, If Any:** None; testable locally using self-signed test JWKS or local SWYRA instance.
* **Status & Next Action:** `READY_FOR_EXECUTION`. Next action: Edit `AuthProvider.tsx` and `api/auth/me/route.ts`.

---

### Task C1-T02: Server-Derived Ownership Enforcement & Removal of Client Owner Field

* **ID and Title:** `C1-T02`: Server-Derived Ownership Enforcement & Removal of Client Owner Field
* **Requirement IDs & User Outcome:** P01, P18, J03. User can access and mutate only their own trips; client-supplied `owner_id` is stripped from API schemas; FastAPI extracts and enforces user identity from validated Bearer token.
* **Current Behavior & Evidence:**
  * `frontend/src/app/dashboard/page.tsx` hardcodes `owner_id: "00000000-0000-0000-0000-000000000001"` (line 320).
  * `backend/src/travel/runtime/fastapi/routes/trips.py` defines `CreateTripRequest.owner_id` (line 37) and trusts client input.
  * `routes/trips.py` routes (`create_trip`, `get_trip`, `update_trip_brief`, `plan_trip`) do not inject `auth.py`'s `get_current_user` dependency.
* **Dependencies:** `C1-T01`.
* **Files / Modules to Inspect:**
  * `backend/src/travel/runtime/fastapi/routes/trips.py`
  * `backend/src/travel/runtime/fastapi/routes/exports.py`
  * `backend/src/travel/runtime/fastapi/auth.py`
  * `frontend/src/app/dashboard/page.tsx`
* **Exact Behavior to Add / Change:**
  1. In `routes/trips.py`: Remove `owner_id` from `CreateTripRequest`.
  2. Inject `CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]` on all route handlers in `routes/trips.py` and `routes/exports.py`.
  3. In `create_trip`: Assign `trip = Trip(owner_id=current_user.id, ...)`.
  4. In `get_trip`, `update_trip_brief`, `plan_trip`, `request_export`: Add ownership check:
     ```python
     if trip.owner_id != current_user.id:
         raise HTTPException(status_code=404, detail="Trip not found")
     ```
     (Returns 404 rather than 403 to prevent resource enumeration attacks).
  5. In `frontend/src/app/dashboard/page.tsx`: Remove `owner_id` from request payload. Transmit Next.js session cookie to BFF or forward Bearer header.
* **Contracts & Migrations Affected:** Removes `owner_id` from OpenAPI `CreateTripRequest` schema. Database schema remains unchanged (`trips.owner_id` is populated from token).
* **Failure, Partial & Recovery Behavior:**
  * Unauthenticated requests receive 401 Unauthorized with `WWW-Authenticate: Bearer`.
  * Cross-user requests receive 404 Not Found.
* **Security & Data-Rights Constraints:** Strict tenant isolation; zero trust of client-submitted ownership identifiers.
* **Tests / Fixtures & Commands:**
  * Integration test in `tests/integration/test_ownership.py`: User A creates trip; User B attempts to read and update trip $\to$ asserts 404.
  * Command: `cd backend && uv run pytest tests/integration/test_ownership.py -v`
* **Acceptance Evidence:**
  * Passing two-user cross-tenant test asserting 404 on unowned resource access.
* **Explicit Non-Goals:** Multi-user trip collaboration (single-owner model is current locked decision).
* **Rollback / Compatibility Notes:** Requires frontend payload update to align with stripped `owner_id`.
* **External Blocker, If Any:** None.
* **Status & Next Action:** `PENDING_DEPENDENCY (C1-T01)`.

---

### Task C2-T01: Canonical Server Plan Consumption & Removal of Client-Side Simulation

* **ID and Title:** `C2-T01`: Canonical Server Plan Consumption & Removal of Client-Side Simulation
* **Requirement IDs & User Outcome:** P01, P04, P07, P08, J05, J06. Traveler clicks compile; system runs server optimization and returns canonical schedule and budget; frontend consumes server payload directly, eliminating client-side Haversine math and orphan trip duplication.
* **Current Behavior & Evidence:**
  * `frontend/src/app/dashboard/page.tsx` executes client-side Haversine math at lines 58–85 (`Math.round(R * c * 1.28)`) and builds client schedule.
  * Line 316 calls `POST /api/v1/trips` on every solve trigger, creating a new orphan trip aggregate instead of updating existing trip version.
  * Line 344 calls `POST /api/v1/trips/${tripData.id}/plan`, but completely ignores the returned proposal result, setting local state from client calculations.
* **Dependencies:** `C1-T02`.
* **Files / Modules to Inspect:**
  * `frontend/src/app/dashboard/page.tsx`
  * `backend/src/travel/runtime/fastapi/routes/trips.py`
  * `backend/src/travel/workflows/trip_planning.py`
* **Exact Behavior to Add / Change:**
  1. In `dashboard/page.tsx`: Maintain a single `activeTripId` in state or URL route: `/dashboard/trips/[id]`.
  2. Clicking "Compile & Re-solve Schedule":
     * If trip does not exist: calls `POST /api/v1/trips` once to create aggregate.
     * If trip exists: calls `POST /api/v1/trips/{id}/brief` with `expected_base_version` to commit brief modifications.
     * Calls `POST /api/v1/trips/{id}/plan` to execute server workflow.
  3. When server planning response returns:
     * Parse `PlanJobResponse.result` (`proposal.schedule` and `proposal.budget_summary`).
     * Set `scheduledStops`, `scheduledLegs`, `budgetSummary`, and `budgetLines` strictly from server payload.
  4. Delete the client-side Haversine approximation function (`computeDistanceKm`) and client budget calculator from `dashboard/page.tsx`.
  5. If backend returns an error: Display error alert banner, keep existing plan state, and do not increment version number.
* **Contracts & Migrations Affected:** Frontend consumption of existing OpenAPI `PlanJobResponse` schema.
* **Failure, Partial & Recovery Behavior:**
  * Server failure displays error toast with diagnostic reason; previous valid plan remains rendered on screen.
* **Security & Data-Rights Constraints:** Server remains sole source of mathematical authority.
* **Tests / Fixtures & Commands:**
  * Playwright E2E test verifying that changing vehicle from Car to Motorcycle updates fuel and transit duration according to server calculations.
  * Command: `cd frontend && npx playwright test e2e/app.spec.ts`
* **Acceptance Evidence:**
  * Network tab confirms `POST /plan` response body directly populates the itinerary stops and itemized budget table.
* **Explicit Non-Goals:** Live GPS turn-by-turn navigation.
* **Rollback / Compatibility Notes:** Eliminates duplicate client math logic.
* **External Blocker, If Any:** None.
* **Status & Next Action:** `PENDING_DEPENDENCY (C1-T02)`.

---

## 3. Subsequent Milestone Task Inventory (C2 through C8)

### Milestone C2: Durable Planning Vertical Slice
* **C2-T02: Asynchronous Job Execution & Worker Separation**
  * Decouple HTTP request from workflow execution: `routes/trips.py` returns `202 Accepted` with `run_id`; background `JobWorker` claims job and executes LangGraph.
* **C2-T03: Server-Sent Events (SSE) Progress Streaming**
  * Implement `GET /api/v1/runs/{run_id}/events` streaming monotonic progress events (`run.started`, `section.updated`, `proposal.ready`).

### Milestone C3: Routing Engine, Discovery & Evidence Classification
* **C3-T01: Multi-Modal Transport & Lodging Adapter Normalization**
  * Implement provider adapters adhering strictly to four-tier evidence classes; reject promotion of indicative prices to live offers.
* **C3-T02: OR-Tools Solver Soundness (Time Windows & Round Trips)**
  * Add time-window dimensions, stop opening hours, vehicle-specific speed profiles, and mandatory return leg to `ItineraryScheduler`.
* **C3-T03: Geospatial POI Discovery via PostGIS**
  * Replace static Mysore place cards with spatial corridor queries against PostGIS `places` table with attribution tags.
* **C3-T04: Deterministic 50-Scenario Benchmark Suite Overhaul**
  * Update `backend/src/travel/benchmarks/runner.py` to evaluate each case's unique inputs and scenario-specific invariants; eliminate tautological 100% loop.

### Milestone C4: Complete Multi-Tab Planning Workspace
* **C4-T01: True Network Route Polyline Rendering**
  * Update Leaflet canvas to render road network geometries from route engine rather than straight coordinate lines.
* **C4-T02: Stop Editing, Differential Comparison & Atomic Approval**
  * Implement drag-and-drop reordering, differential comparison modal, and atomic version advance ($N \to N+1$).
* **C4-T03: Full Keyboard Accessibility & Screen-Reader Certification**
  * Implement WCAG 2.1 AA keyboard navigation across timeline, bottom sheets, and modal dialogs.

### Milestone C5: Sharing, Export Artifacts, Handoff & Privacy
* **C5-T01: Dynamic Public Itinerary Projection & Revocation**
  * Update `/trips/[id]` to read from database via sanitized public share token; implement instantaneous revocation.
* **C5-T02: Direct Official Merchant Handoff Engine**
  * Implement domain allowlist validation, context match classification (`EXACT`, `PARTIAL`, `GENERIC`), and handoff audit logging.
* **C5-T03: Long-Term Preferences & Account Deletion Cascade**
  * Build preference consent management and asynchronous multi-tier data erasure across PostgreSQL, Redis, and S3.

### Milestone C6: Cinematic Marketing Experience & Support Inquiries
* **C6-T01: Editorial Marketing Polish & GSAP Motion Refinement**
  * Finalize marketing landing page typography, responsive layouts, and GSAP 3D corridor cards with accurate disclaimer copy.
* **C6-T02: Durable Contact Support Pipeline**
  * Wire `/contact` form to `POST /api/v1/support/inquiries`; store in PostgreSQL `contact_inquiries` table; eliminate fake toast.

### Milestone C7: Regional Weather, Voice Briefing & Dynamic In-Trip Replanning
* **C7-T01: Live Meteorological Corridor Weather Sourcing**
  * Connect Open-Meteo / IMD feeds for stop temperatures, monsoon indices, and Western Ghats hazard warnings.
* **C7-T02: Client-Native Voice Assistant with Privacy Transparency**
  * Add explicit disclosure regarding browser speech streaming; ensure transcript editable before parsing.
* **C7-T03: In-Trip Replanning & Dynamic Hazard Bypass**
  * Build rerouting engine freezing completed stops and recomputing remaining legs around road closures.

### Milestone C8: Staging Deployment, SLO Verification & Release Governance
* **C8-T01: Production ECS Container Packaging & Health Probes**
  * Build hardened multi-stage Dockerfile; configure readiness/liveness probes; verify graceful shutdown handling.
* **C8-T02: Disaster Recovery Drills & Automated Restore Testing**
  * Execute database point-in-time recovery and cache loss drills in staging environment.
* **C8-T03: End-to-End Performance Load Testing (SLO Verification)**
  * Measure P95 first useful map ($\le 3.5$s) and plan compilation ($\le 20$s) under representative concurrent load.
* **C8-T04: Final Release Sign-Off & Governance Audit**
  * Verify all 8 release gates in `TEST-AND-RELEASE-PLAN.md` satisfy mandatory exit evidence.
