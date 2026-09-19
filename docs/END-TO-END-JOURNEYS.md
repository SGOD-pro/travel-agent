# SWENA Platform End-to-End User Journeys (J01–J15)

**Version:** 1.0  
**Status:** Canonical Target Specification  
**Architecture:** Next.js BFF + FastAPI Modular Monolith + PostgreSQL/PostGIS  
**Boundary Rules:** Zero Hallucination, Decimal Currency, Authoritative PostgreSQL, Explicit Evidence Classes, Non-Custodial External Handoff.

---

## Overview

This specification establishes the exact contracts, screen states, API interactions, persistence transactions, failure recovery paths, privacy constraints, and verification test IDs for the 15 canonical user journeys of the SWENA platform.

---

### Journey Index
- [J01 — Marketing Discovery to Saved Draft](#j01--marketing-discovery-to-saved-draft)
- [J02 — Identity and Account Entry](#j02--identity-and-account-entry)
- [J03 — My Trips and Lifecycle Management](#j03--my-trips-and-lifecycle-management)
- [J04 — Structured Brief and Natural-Language Intent](#j04--structured-brief-and-natural-language-intent)
- [J05 — Progressive Planning and Provider Resilience](#j05--progressive-planning-and-provider-resilience)
- [J06 — Daily Itinerary and Synchronized Spatial Map](#j06--daily-itinerary-and-synchronized-spatial-map)
- [J07 — Itinerary Editing, Differential Comparison, Approval, and Undo](#j07--itinerary-editing-differential-comparison-approval-and-undo)
- [J08 — Transparent, Non-Coerced Financial Budgeting](#j08--transparent-non-coerced-financial-budgeting)
- [J09 — Multi-Modal Transport, Lodging, and Place Discovery](#j09--multi-modal-transport-lodging-and-place-discovery)
- [J10 — Public Itinerary Sharing, Mobile QR, and Access Revocation](#j10--public-itinerary-sharing-mobile-qr-and-access-revocation)
- [J11 — Sourced Itinerary Export and PDF Download](#j11--sourced-itinerary-export-and-pdf-download)
- [J12 — Direct Official Merchant Handoff](#j12--direct-official-merchant-handoff)
- [J13 — In-Trip Replanning and Live Hazard Avoidance](#j13--in-trip-replanning-and-live-hazard-avoidance)
- [J14 — Long-Term Preferences, Privacy Consent, and Account Deletion](#j14--long-term-preferences-privacy-consent-and-account-deletion)
- [J15 — Client-Native Voice Briefing and Operational Support Inquiries](#j15--client-native-voice-briefing-and-operational-support-inquiries)

---

## J01 — Marketing Discovery to Saved Draft

* **Actor:** Unauthenticated Prospective Traveler.
* **Prerequisites:** Modern web browser with JavaScript enabled.
* **Route / Screen:** `/` (Marketing Home), `/about` (Philosophy), `/contact` (Support).
* **Inputs:**
  * Selected corridor card click (e.g., "Western Ghats Monsoon Odyssey", "Royal Rajputana Circuit").
  * Interactive vehicle toggle (`car` vs. `bike`).
* **Validation & Ambiguity Handling:**
  * Corridor cards display explicit, versioned assumptions (e.g., "Indicative fuel rate: ₹102.5/L, assumed mileage: 15.0 km/L").
  * Disclaimers explicitly label road calculations as geometric approximations until compiled by the server.
* **API Action:**
  * Client preserves selected template ID in encrypted temporary session storage or URL parameter: `?draft_corridor=western-ghats`.
* **Transaction & Persistence:**
  * No database write occurs before authentication. Draft remains in client browser storage (`sessionStorage`) with an integrity checksum.
* **Async Events:** None.
* **Success Outcome:**
  * Smooth GSAP 3D interactive tilt cards react to pointer coordinates.
  * Clicking "Explore Corridor" transfers the template into the planning state and navigates user toward the authenticated workspace (`/dashboard`).
* **Empty / Partial / Stale / Offline / Failure States:**
  * *Offline / Disconnected:* Service renders cached static content; CTA notifies user that network connectivity is required to compile live plans.
  * *WebGL / Canvas Failure:* System falls back to static CSS cards without 3D perspective distortion (`prefers-reduced-motion` compliant).
* **Recovery Path:**
  * User can manually configure a blank travel brief if draft storage cannot be parsed.
* **Privacy & Data Rights:**
  * Zero PII collected; zero cookies stored prior to consent; no tracking beacons.
* **Acceptance Criteria:**
  * First Contentful Paint (FCP) $\le 1.2$s, Largest Contentful Paint (LCP) $\le 2.5$s on 4G network profile.
  * Zero simulated live prices; cards explicitly labeled "Representative Route".
* **Test IDs:** `TEST-J01-01` (Hero rendering), `TEST-J01-02` (Reduced motion fallback), `TEST-J01-03` (Draft parameter preservation).

---

## J02 — Identity and Account Entry

* **Actor:** Traveler entering protected workspace.
* **Prerequisites:** SWYRA Auth ([SGOD-pro/OAuth2.1](https://github.com/SGOD-pro/OAuth2.1)) deployed and reachable.
* **Route / Screen:** `/login`, `/api/auth/login`, `/api/auth/callback`, `/api/auth/logout`.
* **Inputs:** User credentials entered exclusively on external SWYRA authorization page.
* **Validation & Ambiguity Handling:**
  * Next.js BFF generates cryptographically secure PKCE `code_verifier` (32 random bytes, base64url encoded) and `code_challenge` ($SHA256(verifier)$).
  * Generates random 16-byte hex `state` token for CSRF mitigation.
  * Rejects redirect URLs not strictly matching the registered callback route.
* **API Action:**
  * `GET /api/auth/login` $\to$ Redirects to `${AUTH_ISSUER}/auth` with PKCE parameters.
  * `GET /api/auth/callback?code={code}&state={state}` $\to$ Verifies state against HttpOnly cookie; issues backend POST to `${AUTH_ISSUER}/api/auth/oauth2/token` using basic auth (`CLIENT_ID` + `CLIENT_SECRET`).
* **Transaction & Persistence:**
  * Upon successful token exchange, BFF sets `swena_session` HttpOnly, Secure, SameSite=Lax cookie containing the validated JWT.
  * Cleans up temporary `oauth_pkce_verifier` and `oauth_state` cookies.
  * Backend PostgreSQL verifies or maps user via `users` table: `INSERT INTO users (identity_issuer, identity_subject) VALUES ($1, $2) ON CONFLICT (identity_issuer, identity_subject) DO NOTHING RETURNING id;`.
* **Async Events:** None.
* **Success Outcome:**
  * User is redirected to `/dashboard` (or previous `return_to` path); navbar reflects user identity (e.g., name/email avatar).
* **Empty / Partial / Stale / Offline / Failure States:**
  * *Unconfigured Credentials:* Redirects to `/login?error=oauth_unconfigured` showing clear configuration guidance.
  * *State Mismatch (CSRF):* Fails immediately with `/login?error=state_mismatch_csrf_detected`.
  * *Token Exchange Rejection:* Renders upstream provider failure message without exposing client secrets.
  * *Session Expiry:* Requests to `/api/auth/me` return 401 `{ user: null }`; proxy redirects user to `/login`.
* **Recovery Path:**
  * Single-click retry redirects user cleanly to authorization gateway with fresh PKCE state.
* **Privacy & Data Rights:**
  * Zero passwords, hashes, or TOTP seeds touch SWENA application servers or databases.
* **Acceptance Criteria:**
  * No access token or secret visible in browser `localStorage`, client bundle, or URL query strings.
  * Session cookie marked `HttpOnly; Secure; SameSite=Lax; Path=/`.
* **Test IDs:** `TEST-J02-01` (PKCE exchange), `TEST-J02-02` (CSRF mismatch rejection), `TEST-J02-03` (Fail-closed unconfigured state), `TEST-J02-04` (Logout invalidation).

---

## J03 — My Trips and Lifecycle Management

* **Actor:** Authenticated Traveler.
* **Prerequisites:** Validated active session.
* **Route / Screen:** `/dashboard/trips` (Trip List), `/dashboard/trips/[id]` (Trip Detail/Workspace).
* **Inputs:** Search query, status filter (Draft, Planned, Active, Archived), "Create New Trip" action, "Duplicate Trip" action, "Delete Trip" action.
* **Validation & Ambiguity Handling:**
  * User can view, edit, duplicate, and delete only trips where `trips.owner_id == authenticated_user.id`.
  * Unauthorized IDs return 404 (preventing user enumeration).
* **API Action:**
  * `GET /api/v1/trips?cursor={cursor}&limit=20`
  * `POST /api/v1/trips` (Creates new blank trip aggregate)
  * `POST /api/v1/trips/{id}/duplicate` (Deep-copies brief into new trip with unique IDs)
  * `DELETE /api/v1/trips/{id}` (Transitions state to `ARCHIVED` or marks for cascade deletion)
* **Transaction & Persistence:**
  * Ownership validated in SQL: `SELECT * FROM trips WHERE id = $1 AND owner_id = $2;`.
  * Cursor-based pagination on `(created_at, id)`.
* **Async Events:** None.
* **Success Outcome:**
  * Displays user's saved journeys with origin, destinations, date summary, latest version number, and cost summary.
* **Empty / Partial / Stale / Offline / Failure States:**
  * *Empty State:* Friendly prompt to launch first journey with template shortcuts.
  * *Stale Data:* Revalidates on focus via SWR/React Query.
* **Recovery Path:**
  * Accidentally archived trips can be unarchived within 30-day grace period.
* **Privacy & Data Rights:**
  * Deleting a trip cascades to `trip_versions`, `itineraries`, `artifacts` in S3, and public share tokens.
* **Acceptance Criteria:**
  * Two distinct test users running concurrently cannot inspect or mutate each other's trips.
* **Test IDs:** `TEST-J03-01` (List ownership isolation), `TEST-J03-02` (Trip creation), `TEST-J03-03` (Duplication ID remapping), `TEST-J03-04` (Cascade deletion).

---

## J04 — Structured Brief and Natural-Language Intent

* **Actor:** Authenticated Traveler planning a route.
* **Prerequisites:** Active trip instance in `DRAFT` state.
* **Route / Screen:** `/dashboard` (Travel Brief Form & Natural Language Assistant).
* **Inputs:**
  * Origin hub (e.g., "Bengaluru", "Delhi").
  * Ordered stops (e.g., "Mysuru", "Coorg", "Wayanad") with individual stay durations (days).
  * Travel calendar window (start date, end date).
  * Party composition: Adults ($\ge 1$), children (with explicit age inputs), requested hotel rooms.
  * Total monetary budget constraint (optional ceiling).
  * Vehicle preference: `CAR_PETROL`, `MOTORCYCLE_PETROL`, `BICYCLE`, `WALK`, `TRAIN`, `BUS`, `FLIGHT`.
  * Natural language prompt (e.g., "3-day coffee estate road trip with my 8yo son").
* **Validation & Ambiguity Handling:**
  * *Ambiguous Place Names:* If user enters "Coorg", system prompts: "Did you mean Madikeri (district center), Kushalnagar, or Virajpet?".
  * *Vehicle Clarification:* If user inputs "bike", system asks: "Do you intend a petrol motorcycle (NH highway speeds) or pedal bicycle?".
  * *EV Prohibition:* Electric vehicle inputs reject with: "EV battery and charging station planning is currently deferred. Please select petrol vehicle or public transit."
  * *Temporal Invariants:* `end_date >= start_date`; $\sum \text{stay\_days} \le \text{total\_trip\_days}$.
  * *Coordinate Resolution:* Modifying a destination name forces re-resolution of coordinates; stale coordinates are invalidated immediately.
* **API Action:**
  * `POST /api/v1/trips/{id}/brief`
  * Body: `UpdateBriefRequest { expected_base_version: int, brief: TripBrief, command_id: UUID }`
* **Transaction & Persistence:**
  * Validates optimistic concurrency: `IF trip.current_version != expected_base_version THEN RAISE 409 CONFLICT;`.
  * Stores immutable snapshot in `trip_versions` and increments `trip.current_version`.
* **Async Events:** None.
* **Success Outcome:**
  * Brief validated; version advanced ($v1 \to v2$); UI displays updated summary and enables the "Compile Schedule" button.
* **Empty / Partial / Stale / Offline / Failure States:**
  * *Concurrent Edit Collision:* Returns 409 Conflict with diff of current server version and local edits.
  * *Schema Violation:* Returns 422 with exact invalid field names and constraint rules.
* **Recovery Path:**
  * Conflict resolution dialog offers "Merge changes", "Overwrite with my edits", or "Reload server state".
* **Privacy & Data Rights:**
  * Child ages stored without names or birthdates; origin stored at city/neighborhood precision, never residential street address.
* **Acceptance Criteria:**
  * Invalid dates (e.g., return before departure) fail validation before server dispatch.
* **Test IDs:** `TEST-J04-01` (Validation rules), `TEST-J04-02` (409 conflict handling), `TEST-J04-03` (EV rejection), `TEST-J04-04` (Coordinate re-resolution).

---

## J05 — Progressive Planning and Provider Resilience

* **Actor:** Authenticated Traveler initiating optimization.
* **Prerequisites:** Validated `TripBrief` at current version $N$.
* **Route / Screen:** `/dashboard` (Planning Console / Stage Indicator).
* **Inputs:** User clicks "Compile & Re-solve Schedule".
* **Validation & Ambiguity Handling:**
  * Ensures brief has at least 1 destination and valid date range.
* **API Action:**
  * `POST /api/v1/trips/{id}/plans` $\to$ Returns `202 Accepted` with payload: `{ run_id: UUID, status_url: string, events_url: string }`.
  * Client opens Server-Sent Events stream: `GET /api/v1/runs/{run_id}/events`.
* **Transaction & Persistence:**
  * API transaction inserts record into `jobs` table with `status = 'PENDING'` and `type = 'trip_planning'`.
  * Inserts tracking row in `planning_runs`.
  * Background worker (`JobWorker`) acquires lease via `SELECT ... FOR UPDATE SKIP LOCKED`.
  * Executes `trip_planning_workflow` (LangGraph) storing node checkpoints in PostgreSQL checkpoint schema.
* **Async Events (SSE Event Stream):**
  1. `run.started`: Run acknowledged, worker assigned.
  2. `section.updated`: `{ section: "routing", status: "completed", data: { legs: 3, distance_km: 340 } }`
  3. `section.updated`: `{ section: "lodging", status: "completed", data: { hotels_found: 4 } }`
  4. `section.updated`: `{ section: "budget", status: "completed", data: { completeness: "incomplete", unknown_items: 2 } }`
  5. `proposal.ready`: `{ run_id: UUID, proposal_hash: string, base_version: N }`
* **Success Outcome:**
  * Console transitions from "Compiling..." through stages (Route $\to$ Places $\to$ Schedule $\to$ Budget $\to$ Synthesis) within P95 target $\le 20$s.
  * Plan results rendered in Solved Schedule tab.
* **Empty / Partial / Stale / Offline / Failure States:**
  * *Provider Outage:* If external hotel search fails, lodging section marks `UNAVAILABLE` with reason; route and schedule sections complete successfully.
  * *Browser Disconnect:* Planning run continues executing on backend worker; client reconnects using `Last-Event-ID` or polls `GET /api/v1/runs/{run_id}`.
  * *Solver Infeasibility:* If travel time exceeds daily daylight horizon, solver returns partial schedule with warning: "Day 2 requires 9h transit; recommend adding layover stop."
* **Recovery Path:**
  * User can click "Retry Failed Sections" without recomputing successful routing nodes.
* **Privacy & Data Rights:**
  * Worker logs redact traveler identity and raw GPS coordinates.
* **Acceptance Criteria:**
  * HTTP POST returns within 250ms with 202 Accepted; long execution never blocks web request thread.
* **Test IDs:** `TEST-J05-01` (202 async response), `TEST-J05-02` (SSE stream re-connection), `TEST-J05-03` (Partial provider failure tolerance).

---

## J06 — Daily Itinerary and Synchronized Spatial Map

* **Actor:** Traveler reviewing proposed trip.
* **Prerequisites:** Completed planning run with proposal proposal.
* **Route / Screen:** `/dashboard` (Split View: Itinerary Timeline & Geospatial Map Canvas).
* **Inputs:** Selecting a day tab (e.g., "Day 1", "Day 2"), clicking a stop or transit leg, zooming map canvas.
* **Validation & Ambiguity Handling:**
  * Transit leg durations match solver matrix; stop arrival/departure times advance monotonically: $\text{arrival}_{i} + \text{dwell}_{i} \le \text{departure}_{i} \le \text{arrival}_{i+1}$.
  * Geographic coordinates validated within Indian territorial bounds (lat $6^\circ$–$38^\circ$N, lng $68^\circ$–$98^\circ$E).
* **API Action:**
  * `GET /api/v1/trips/{id}/proposals/{proposal_id}` or consumed directly from completed run payload.
* **Transaction & Persistence:**
  * Persisted in `itineraries`, `itinerary_stops`, and `itinerary_legs`.
* **Async Events:** None.
* **Success Outcome:**
  * Left Panel: Chronological card list with arrival, dwell duration, activities, and departure.
  * Right Panel: Leaflet map with CartoDB dark matter tiles; rendered polylines trace road corridors; numbered markers synchronize with stop cards.
  * Clicking stop on timeline centers and opens popup on map; clicking map marker scrolls timeline into view.
* **Empty / Partial / Stale / Offline / Failure States:**
  * *Map Tile Failure:* Leaflet tile loading error displays fallback banner: "Map tiles temporarily unavailable; itinerary list fully operational."
  * *Coarse Geometry:* Straight-line fallback links display label: "Illustrative straight-line transit; actual road geometry pending route engine connection."
* **Recovery Path:**
  * Full keyboard accessibility: Tab navigation visits every stop, arrow keys switch days, enter key expands stop detail.
* **Privacy & Data Rights:**
  * Map tile server receives only bounding-box requests; no user ID or trip aggregate ID transmitted to tile providers.
* **Acceptance Criteria:**
  * 100% accessible via keyboard; mobile viewport collapses map into toggleable bottom sheet.
* **Test IDs:** `TEST-J06-01` (Timeline monotonicity), `TEST-J06-02` (Map selection sync), `TEST-J06-03` (Tile failure fallback).

---

## J07 — Itinerary Editing, Differential Comparison, Approval, and Undo

* **Actor:** Authenticated Traveler refining schedule.
* **Prerequisites:** Plan proposal presented on workspace.
* **Route / Screen:** `/dashboard` (Interactive Timeline, Stop Controls, Diff Modal).
* **Inputs:**
  * Reorder stop (drag-and-drop or "Move Up" / "Move Down" buttons).
  * Change stop stay duration (e.g., from 2h to 4h).
  * Remove destination or add side-trip stop.
  * Click "Accept & Commit Version".
* **Validation & Ambiguity Handling:**
  * System detects impact of edits: recalculates travel time for adjacent legs only.
  * If a hard user constraint is violated (e.g., museum visit moved to Monday when museum is closed), system flags warning: "Mysuru Palace is closed on selected holiday."
* **API Action:**
  * `POST /api/v1/trips/{id}/deltas` $\to$ Submits proposed change set against base version $N$.
  * `POST /api/v1/trips/{id}/approvals` $\to$ Body: `{ delta_id: UUID, base_version: N, proposal_hash: string, decision: "APPROVED" }`.
* **Transaction & Persistence:**
  * Atomic commit:
    ```sql
    BEGIN;
    SELECT current_version FROM trips WHERE id = $1 FOR UPDATE;
    -- verify current_version == base_version
    INSERT INTO trip_versions (trip_id, version, brief_json, ...) VALUES ($1, $base_version + 1, ...);
    INSERT INTO approvals (trip_id, base_version, delta_id, decision, ...) VALUES (...);
    UPDATE trips SET current_version = current_version + 1, updated_at = NOW() WHERE id = $1;
    COMMIT;
    ```
* **Async Events:** None.
* **Success Outcome:**
  * Visual diff highlights added/removed stops in sage green / amber.
  * Accepting changes advances aggregate version ($v2 \to v3$); updates URL snapshot; creates undo history point.
* **Empty / Partial / Stale / Offline / Failure States:**
  * *Stale Proposal:* If trip was updated in another tab, approval rejects with 409: "Trip version has advanced. Please review current version before approving."
  * *Undo Action:* User can click "Revert to v2" which commits a new version with v2 state (preserving immutable audit history).
* **Recovery Path:**
  * System never mutates historical version rows; rollback is an append-only commit.
* **Privacy & Data Rights:**
  * Approval audit record stores internal user UUID and timestamp.
* **Acceptance Criteria:**
  * Base version mismatch strictly rejects approval commit.
* **Test IDs:** `TEST-J07-01` (Diff generation), `TEST-J07-02` (Atomic version advance), `TEST-J07-03` (409 stale approval rejection).

---

## J08 — Transparent, Non-Coerced Financial Budgeting

* **Actor:** Traveler reviewing trip costs.
* **Prerequisites:** Generated plan with computed budget lines.
* **Route / Screen:** `/dashboard` (Itemized Budget Tab).
* **Inputs:** Editable fuel price (₹/L), vehicle fuel mileage (km/L), hotel tier selection.
* **Validation & Ambiguity Handling:**
  * *Arithmetic Precision:* All monetary amounts represented internally as `Decimal` with 2 decimal places.
  * *Zero-Coercion Invariant:* If a mandatory toll, park fee, or permit is unquoted, it is represented as `BudgetLine(amount=None, estimated=True, unknown_reason="...")`.
  * It is NEVER converted to ₹0.00.
  * Total line displays: `"Known/estimated subtotal; tolls unknown"`.
  * Completeness badge displays: `Budget Completeness: Incomplete (Preserved)`.
* **API Action:**
  * Consumes `BudgetSummary` from proposal.
* **Transaction & Persistence:**
  * Stored in `budget_lines` table with foreign key to itinerary.
* **Async Events:** None.
* **Success Outcome:**
  * Transparent categorical breakdown:
    * **Road Fuel:** Calculated from exact route distance: $\text{Liters} = \frac{\text{Distance}}{\text{Mileage}}$, $\text{Cost} = \text{Liters} \times \text{Price}$.
    * **Highway Tolls:** ₹320.00 (verified) + 1 Unknown toll plaza (explicitly listed with bypass reason).
    * **Lodging:** Sourced hotel rates $\times$ rooms $\times$ nights.
    * **Attractions & Meals:** Estimated daily allowance per adult/child.
* **Empty / Partial / Stale / Offline / Failure States:**
  * *Currency Mismatch:* Reject mixed currencies; all line items converted via verified `fx_rates` or flagged as multi-currency error.
* **Recovery Path:**
  * Traveler can enter known toll override to transition budget status to `Complete`.
* **Privacy & Data Rights:**
  * Financial assumptions stored per trip, never linked to personal credit cards or banking profiles.
* **Acceptance Criteria:**
  * Unknown cost lines never sum to ₹0; unquoted tolls explicitly prevent an unconditional "Within Budget" badge.
* **Test IDs:** `TEST-J08-01` (Decimal precision), `TEST-J08-02` (Zero-coercion preservation), `TEST-J08-03` (Mileage override math).

---

## J09 — Multi-Modal Transport, Lodging, and Place Discovery

* **Actor:** Traveler comparing transit and stay options.
* **Prerequisites:** Proposed route corridors.
* **Route / Screen:** `/dashboard` (Corridor Places & Transport Options Tab).
* **Inputs:** Transport mode filters (Train, Flight, Driving, Bus), Lodging filters (Homestay, Heritage, Budget).
* **Validation & Ambiguity Handling:**
  * Options classified into strict four-tier evidence system:
    1. `LIVE_OFFER`: Verified supplier availability within freshness TTL.
    2. `INDICATIVE_SEARCH`: Observed web/scraped fare; requires verification on portal.
    3. `EDITORIAL_DISCOVERY`: Curated heritage/scenic recommendation with source reference.
    4. `UNAVAILABLE`: Provider down or no matching inventory.
  * Never promotes indicative observation to live offer.
* **API Action:**
  * `GET /api/v1/trips/{id}/options?category=transport`
  * `GET /api/v1/trips/{id}/options?category=lodging`
* **Transaction & Persistence:**
  * Stored in `transport_options` and `hotel_options` referencing `evidence` records.
* **Async Events:** None.
* **Success Outcome:**
  * Train comparison card shows IRCTC train number, class (2A/3A/SL), departure/arrival times, and fare evidence status.
  * Hotel card shows check-in/out dates matching trip days, room capacity matching party size, and verified amenities.
* **Empty / Partial / Stale / Offline / Failure States:**
  * *No Availability:* Displays explicit `UNAVAILABLE` badge with reason (e.g., "All 2A berths waitlisted on selected date; bus or road transit recommended").
* **Recovery Path:**
  * Traveler clicks "Search Bus Alternatives" or adjusts travel dates by $\pm 1$ day.
* **Privacy & Data Rights:**
  * Scraped data stripped of third-party user reviews containing personal names.
* **Acceptance Criteria:**
  * Every displayed fare displays source checked timestamp and evidence tier badge.
* **Test IDs:** `TEST-J09-01` (Evidence tier classification), `TEST-J09-02` (Occupancy matching), `TEST-J09-03` (Unavailable state rendering).

---

## J10 — Public Itinerary Sharing, Mobile QR, and Access Revocation

* **Actor:** Trip Owner sharing route with travel companions or roadside driver.
* **Prerequisites:** Validated saved trip with version $\ge 1$.
* **Route / Screen:** `/dashboard` (Share Modal) $\to$ `/trips/[id]` (Public Mobile Route View).
* **Inputs:**
  * Owner clicks "Share Route".
  * Selects sharing scope: "Published Version Snapshot" (default) or "Live Collaborator View".
  * Clicks "Revoke Public Link".
* **Validation & Ambiguity Handling:**
  * Public view is an authorized projection strictly filtered by `PublicTripProjection` schema.
  * **Sanitized Fields:** Strips owner email, user ID, private budget notes, dietary notes, exact home origin address, and external supplier transaction references.
* **API Action:**
  * `POST /api/v1/trips/{id}/shares` $\to$ Creates 128-bit high-entropy public share token.
  * `GET /api/v1/public/trips/{share_token}` $\to$ Returns sanitized public itinerary.
  * `DELETE /api/v1/trips/{id}/shares/{share_token}` $\to$ Revokes access immediately.
* **Transaction & Persistence:**
  * Stored in `public_shares` table: `(id, trip_id, version, token_hash, expires_at, revoked_at)`.
* **Async Events:** None.
* **Success Outcome:**
  * Dashboard displays share link (e.g., `https://swena.travel/trips/sh_8f93a0b2...`) and renders client-side SVG QR code.
  * Companion scans QR code with smartphone camera $\to$ opens mobile-optimized `/trips/sh_8f93a0b2...`.
  * Public page displays verified itinerary, stop timeline, map polyline, and weather advisories.
* **Empty / Partial / Stale / Offline / Failure States:**
  * *Revoked / Expired Token:* Returns 404 with message: "This travel itinerary link has been revoked by the owner or has expired."
  * *Invalid ID in URL:* Never displays a default stock trip; strictly returns 404 Not Found.
* **Recovery Path:**
  * Owner can regenerate a fresh share link with a single click.
* **Privacy & Data Rights:**
  * Public routes tagged with `X-Robots-Tag: noindex, nofollow` to prevent search engine indexing.
* **Acceptance Criteria:**
  * Revoking a share token renders the link immediately inaccessible (within 0s TTL; verified bypassing CDN cache).
* **Test IDs:** `TEST-J10-01` (Public projection sanitization), `TEST-J10-02` (Revocation enforcement), `TEST-J10-03` (Invalid slug 404).

---

## J11 — Sourced Itinerary Export and PDF Download

* **Actor:** Authenticated Traveler needing offline / printable documentation.
* **Prerequisites:** Saved trip itinerary version.
* **Route / Screen:** `/dashboard` (Export Menu) or `/trips/[id]` (Print/Download button).
* **Inputs:** Format selection (`PDF`), include budget breakdown toggle, include emergency contacts toggle.
* **Validation & Ambiguity Handling:**
  * Export binds to specific immutable trip version $N$.
  * If version is currently being re-solved, export is queued until version commits.
* **API Action:**
  * `POST /api/v1/trips/{id}/exports` $\to$ Body: `{ trip_version: N, format: "pdf" }`. Returns `202 Accepted` `{ artifact_id: UUID, status: "pending" }`.
  * Client polls: `GET /api/v1/artifacts/{artifact_id}`.
  * When ready, client requests: `GET /api/v1/artifacts/{artifact_id}/download` $\to$ Returns 302 redirect to pre-signed, short-lived (15 min) private S3 URL.
* **Transaction & Persistence:**
  * Recorded in `artifacts` table with `object_key`, `content_type = 'application/pdf'`, and `expires_at`.
  * Background worker executes `PdfExportService` using ReportLab; streams bytes to private S3 bucket.
* **Async Events:**
  * Background job completion updates artifact status to `READY`.
* **Success Outcome:**
  * Traveler downloads branded, publication-grade PDF containing:
    * Itinerary summary, dates, vehicle profile.
    * Monotonic timetable with stop arrivals, dwell, and departures.
    * Explicit itemized budget with "Tolls Unknown" warning banner if applicable.
    * PostGIS geographic evidence verification hashes.
    * Official merchant booking handoff URLs.
* **Empty / Partial / Stale / Offline / Failure States:**
  * *Worker Crash:* If PDF generation fails, artifact status updates to `FAILED` with retry action; never returns corrupted or 0-byte file.
* **Recovery Path:**
  * User can click "Regenerate PDF" which triggers fresh worker task.
* **Privacy & Data Rights:**
  * S3 bucket blocks all public access; files accessible solely via authenticated pre-signed URLs.
* **Acceptance Criteria:**
  * Generated PDF contains explicit unknown cost disclosures and matches exact requested version number.
* **Test IDs:** `TEST-J11-01` (PDF content validation), `TEST-J11-02` (Signed S3 URL expiry), `TEST-J11-03` (Version binding).

---

## J12 — Direct Official Merchant Handoff

* **Actor:** Traveler ready to book transport or lodging.
* **Prerequisites:** Itinerary containing verified provider options.
* **Route / Screen:** `/dashboard` or `/trips/[id]` (Option Detail Card $\to$ Merchant Handoff Modal).
* **Inputs:** User clicks "View on IRCTC" or "Search on KSTDC".
* **Validation & Ambiguity Handling:**
  * Handoff engine validates merchant URL against strict HTTPS domain allowlist (`irctc.co.in`, `makemytrip.com`, `booking.com`, `kstdc.co`).
  * Classifies context preservation:
    * `EXACT`: Deep-link directly pre-fills date, train number, and passengers.
    * `PARTIAL`: Pre-fills destination and date; traveler must pick room.
    * `GENERIC`: Navigates to verified portal search page.
  * Button text dynamically mirrors context: "Book on [Merchant]" vs. "Search on [Merchant]".
* **API Action:**
  * `POST /api/v1/trips/{id}/handoffs`
  * Body: `{ trip_version: N, option_id: UUID }`
  * Response: `{ intent_id: UUID, destination_url: string, merchant: string, context_match: "EXACT"|"PARTIAL"|"GENERIC" }`
* **Transaction & Persistence:**
  * Logs immutable handoff event in `handoff_events` table for conversion auditing (no PII stored).
* **Async Events:** None.
* **Success Outcome:**
  * Modal renders transparent handoff notice: "You are leaving SWENA to complete your reservation directly with official provider [Merchant]. SWENA charges no fees and holds no payments."
  * Opens verified merchant portal in new browser tab with `rel="noopener noreferrer"`.
* **Empty / Partial / Stale / Offline / Failure States:**
  * *Unapproved Domain:* If provider returns unexpected redirect domain, handoff engine blocks execution with error: "Target destination failed domain security audit."
* **Recovery Path:**
  * Traveler provided with merchant name and search terms to copy manually.
* **Privacy & Data Rights:**
  * Zero traveler financial credentials or payment cards are handled.
* **Acceptance Criteria:**
  * No internal checkout or payment UI exists; return from merchant does not display "Booking Confirmed".
* **Test IDs:** `TEST-J12-01` (Domain allowlist enforcement), `TEST-J12-02` (Context match classification), `TEST-J12-03` (No-checkout guarantee).

---

## J13 — In-Trip Replanning and Live Hazard Avoidance

* **Actor:** Traveler en-route experiencing delay or weather alert.
* **Prerequisites:** Active trip in progress with completed and remaining stops.
* **Route / Screen:** `/dashboard` (In-Trip Assistant Mode).
* **Inputs:**
  * User triggers "Replan Remaining Route".
  * Inputs: Permitted current location (GPS or manual town name), current time, remaining stops.
  * System detects meteorological hazard (e.g., IMD Western Ghats monsoon landslide warning).
* **Validation & Ambiguity Handling:**
  * System freezes all past, completed stops: completed legs are immutable historical records.
  * Recomputes schedule and route geometry only for future, remaining stops.
* **API Action:**
  * `POST /api/v1/trips/{id}/reroutes`
  * Body: `{ base_version: N, current_stop_index: int, current_time: ISO8601, bypass_corridors: ["shiradi-ghat"] }`
* **Transaction & Persistence:**
  * Generates new candidate itinerary version $N+1$; requires user approval before updating canonical trip.
* **Async Events:**
  * Worker runs OR-Tools solver with updated start time and excluded road segments.
* **Success Outcome:**
  * Workspace displays proposed route diff: bypasses blocked ghat pass via alternate highway; updates arrival timestamps for subsequent stops.
  * Budget differential reflects updated distance, fuel, and tolls.
* **Empty / Partial / Stale / Offline / Failure States:**
  * *No Feasible Alternate:* If no road bypass exists within time horizon, system alerts: "No road access feasible before nightfall. Recommend overnight layover at [Town]."
* **Recovery Path:**
  * Traveler can reject reroute proposal and maintain current plan.
* **Privacy & Data Rights:**
  * Current GPS location used in-memory for routing matrix calculation; not persisted to long-term database logs.
* **Acceptance Criteria:**
  * Completed stops are never shifted or deleted during replanning.
* **Test IDs:** `TEST-J13-01` (Past stop immutability), `TEST-J13-02` (Ghat hazard bypass), `TEST-J13-03` (Delta version generation).

---

## J14 — Long-Term Preferences, Privacy Consent, and Account Deletion

* **Actor:** Authenticated Traveler managing personal data.
* **Prerequisites:** Active account.
* **Route / Screen:** `/dashboard/settings` (Privacy & Account Management).
* **Inputs:**
  * Toggle long-term preference saving (dietary restrictions, driving pace, preferred hotel chains).
  * Consent checkboxes with specific policy versions.
  * "Delete All My Data" button with confirmation modal.
* **Validation & Ambiguity Handling:**
  * Long-term personalization disabled by default; requires affirmative explicit consent.
  * Deletion requires typing "DELETE" to prevent accidental clicks.
* **API Action:**
  * `PUT /api/v1/me/preferences` $\to$ `{ consent_granted: true, preferences: { pace: "relaxed", veg_only: true } }`
  * `DELETE /api/v1/me/account` $\to$ Returns `202 Accepted` `{ deletion_job_id: UUID }`
* **Transaction & Persistence:**
  * Deletion initiates cascade:
    1. Sets `users.deletion_state = 'PENDING'`.
    2. Revokes active session tokens in Redis.
    3. Worker deletes all `trips`, `trip_versions`, `itineraries`, `budget_lines`, `public_shares`.
    4. Issues S3 batch delete for all associated export artifacts.
    5. Clears LangGraph checkpoints associated with user.
    6. Updates `users.deletion_state = 'COMPLETED'` with zero personal claims retained.
* **Async Events:**
  * `deletion_jobs` updates progress per storage tier.
* **Success Outcome:**
  * User receives confirmed deletion receipt; automatically signed out; redirected to marketing home.
* **Empty / Partial / Stale / Offline / Failure States:**
  * *S3 Failure:* If S3 deletion times out, deletion job retries; user record remains tombstoned to prevent access.
* **Recovery Path:**
  * Admin runbook provides audit trail of deletion job execution.
* **Privacy & Data Rights:**
  * Complete DPDP / GDPR compliant data erasure across PostgreSQL, Redis, and S3.
* **Acceptance Criteria:**
  * Querying user ID in database after deletion returns 0 rows across all domain tables.
* **Test IDs:** `TEST-J14-01` (Explicit consent gate), `TEST-J14-02` (Cascade deletion across stores), `TEST-J14-03` (Session revocation).

---

## J15 — Client-Native Voice Briefing and Operational Support Inquiries

* **Actor:** Traveler dictating a brief or seeking support.
* **Prerequisites:** Supported browser (Chromium/Edge for Web Speech; standard modern browser for support).
* **Route / Screen:** `/dashboard` (Voice Assistant Modal), `/contact` (Support Page).
* **Inputs:**
  * Voice: Click "Start Listening", dictate: "Four day family holiday from Bengaluru to Ooty with my wife and 10 year old daughter."
  * Contact: Support form inputs (Name, Email, Category, Trip ID, Message).
* **Validation & Ambiguity Handling:**
  * *Voice Privacy Transparency:* UI displays persistent badge: "Speech recognition is processed by your browser engine. Audio is not recorded or stored by SWENA servers."
  * Voice transcript populates an editable text preview before any intent extraction runs.
  * *Contact Validation:* Validates email format, checks rate limit (max 3 inquiries/hour/IP), sanitizes HTML input.
* **API Action:**
  * Voice: Transcript submitted to `POST /api/v1/intent/extract` (standard structured brief extractor).
  * Contact: `POST /api/v1/support/inquiries` $\to$ Returns `201 Created` `{ inquiry_id: UUID, status: "received" }`.
* **Transaction & Persistence:**
  * Contact inquiry inserted into `contact_inquiries` table with status `QUEUED`.
  * Outbox event published to notify support operations.
* **Async Events:** None.
* **Success Outcome:**
  * Voice: Transcript converts to structured parameters (Origin: Bengaluru, Dest: Ooty, Days: 4, Adults: 2, Children: [10]); opens brief editor for traveler confirmation.
  * Contact: Form displays confirmation banner with inquiry ticket ID and expected response window; persists inquiry in backend database.
* **Empty / Partial / Stale / Offline / Failure States:**
  * *Unsupported Browser:* Voice assistant displays clean fallback: "Voice recognition unavailable in this browser. Please use the brief editor form."
  * *Network Failure on Contact:* Contact form preserves user message in local state; provides "Retry Submission" button; never displays fake success.
* **Recovery Path:**
  * Traveler can edit recognized voice transcript manually before submission.
* **Privacy & Data Rights:**
  * Support inquiries retained for 90 days for operational resolution, then purged.
* **Acceptance Criteria:**
  * Contact form submission verified writing row to PostgreSQL `contact_inquiries` table.
* **Test IDs:** `TEST-J15-01` (Voice privacy disclosure), `TEST-J15-02` (Transcript editable preview), `TEST-J15-03` (Durable contact persistence).
