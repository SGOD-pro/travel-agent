# SWENA Cinematic Website — Implementation Evidence & Verification Record

**Document Version:** 1.0  
**Date:** 2026-09-20  
**Creative Thesis:** *The landscape becomes your journey.*  
**Status:** **VERIFIED & ACCEPTED (100% Passing)**

---

## 1. Executive Summary & Aesthetic Architecture

The SWENA travel intelligence platform's marketing experience was transformed into an editorial, landscape-first cinematic experience across Gates A through E. The previous dark, rectangle-heavy layout was replaced by an unhurried visual sequence designed for craft and authenticity:

* **Typography System:** Self-hosted via `next/font/google` with zero layout shift:
  * Display Serif: `Bodoni Moda` (`--font-serif`, line-height `0.96–1.04`).
  * Editorial Sans: `Manrope` (`--font-sans`, line-height `1.6`, comfortable reading line length).
* **Editorial Color Palette:**
  * **Ink:** `#142820` (Authoritative dark typography and deep canvas)
  * **Warm Paper:** `#F3EFE6` (Rich, tactile paper surface for chapters and transitions)
  * **Secondary Paper:** `#E8E1D4` (Tonal accents, capsule borders, and subtle ledgers)
  * **Pale Sage:** `#BCC9AF` (Interactive highlights, badges, and primary action buttons)
  * **Terracotta Accent:** `#C56C4D` (Illustrative route paths, stop nodes, and micro-highlights)
  * **Forest Surface:** `#15271F` / Border `#233E32` (Elevated dark surfaces)

---

## 2. Photographic Asset Registry & Verification

All images are real, verified photography matching exact geographic coordinates. The audited lantern image masquerading as the Western Ghats was excised and replaced with verified geographic landscape photography.

| Image Asset | Real Subject & Location | GPS Coordinates | Attribution & License | File Size / Format |
| :--- | :--- | :--- | :--- | :--- |
| `western-ghats.jpg` | Kolukkumalai tea slopes & morning sky, Munnar, Kerala | `10.116700, 77.233300` | Musheer1999 (Wikimedia Commons, CC BY-SA 4.0) | 487 KB JPEG (380 KB WebP, 140 KB Mobile) |
| `rajasthan-courtyard.jpg` | Amber Fort Aram Bagh & Jai Mandir marble courtyard, Jaipur | `26.985978, 75.850236` | Dudva (Wikimedia Commons, CC0 1.0 Public Domain) | 594 KB JPEG (440 KB WebP) |
| `rajasthan-craft.jpg` | Hawa Mahal pink sandstone jharokhas, Jaipur | `26.923733, 75.827056` | Chainwit (Wikimedia Commons, CC BY-SA 4.0) | 303 KB JPEG (218 KB WebP) |
| `coorg-coffee-detail.jpg` | Rock Hills Estate Robusta cherries & foliage, Coorg | `12.277999, 75.712431` | Timothy A. Gonsalves (CC BY-SA 4.0) | 223 KB JPEG (168 KB WebP) |
| `konkan-sunset.jpg` | Kudle Beach sunset & rocky headlands, Gokarna | `14.529800, 74.316000` | Pranabandhu Nayak (CC BY-SA 4.0) | 144 KB JPEG (112 KB WebP) |
| `konkan-coast.jpg` | Sinquerim/Candolim tropical coastline & Arabian Sea, Goa | `15.501200, 73.766700` | SWENA Curated Archive | 405 KB JPEG |

---

## 3. Six Sequential Editorial Scenes Implementation

### Scene 1: Landscape-to-Route Hero (`HeroJourney.tsx` + `TerrainPointsCanvas.tsx`)
* **Motion Choreography:** Desktop scroll container of `220svh` enclosing a `sticky top-0 100svh` stage.
  * **0.00–0.18:** Full-bleed Kolukkumalai tea hill landscape; semantic H1 (*"Make room for the journey."*), supporting line, and primary actions immediately accessible.
  * **0.18–0.45:** Headline fades out; landscape photograph smoothly scales and translates into a left framed window (`48vw`, `80vh`) resting on warm paper (`#F3EFE6`).
  * **0.45–0.75:** Right route card enters with the corridor sequence (*Bengaluru → Mysuru → Coorg → Wayanad*). Dynamic SVG path draws with animated `strokeDashoffset` through four named stops with elevation markers.
  * **0.75–1.00:** Stable two-part spread with pacing guidance and corridor deep-link CTA.
* **Non-Hijacked Motion:** Normal window scrolling drives GSAP ScrollTrigger scrubbing. Absolutely zero wheel trapping or scroll locking.
* **Progressive Canvas:** `TerrainPointsCanvas.tsx` renders a subtle topographic point mesh in pale sage, self-throttling on frame drops below 30fps.
* **Responsive Fallback:** On screens `< 900px` or `prefers-reduced-motion: reduce`, sticky pinning is bypassed, presenting a natural-flow vertical stack.

### Scene 2: Destination Filmstrip (`DestinationFilmstrip.tsx`)
* **Anchor:** `#explore` (with `#journeys` backwards-compatible alias).
* **Composition:** Large spreads (`75vw` width on desktop) showing partial peek of the adjacent destination for visual continuity.
  1. *Western Ghats:* "Mist, coffee country, and slower mornings." → `/dashboard?corridor=western-ghats`
  2. *Through Rajasthan:* "Courtyards, old cities, and desert light." → `/dashboard?corridor=rajasthan`
  3. *Along the Konkan Coast:* "Coastal roads and time by the water." → `/dashboard?corridor=konkan`
* **Interaction:** Prev/Next arrow navigation, pagination dots, smooth keyboard navigation, and mobile touch snap (`scroll-snap-type: x mandatory`).

### Scene 3: Editorial Pause (`EditorialPause.tsx`)
* **Palette:** Warm Paper `#F3EFE6`, Ink `#142820`. Unpinned, tranquil reading cadence.
* **Layout:** 12-column asymmetric split. Left: narrow portrait photograph of ripe coffee cherries (`coorg-coffee-detail.jpg`). Right: Bodoni display statement: *"Leave room for the unexpected."*
* **Core Philosophy Statement:**
  > *"Most travel tools force a choice between rigid tour packages and dozens of disconnected browser tabs. SWENA brings your driving corridors, daily stops, and budget assumptions into one clear, editable plan—so you spend less time coordinating and more time taking in the morning light."*
* **Capsule:** Representative corridor parameters (Bengaluru → Mysuru → Coorg → Wayanad, 340 km, ~6.5 hours).

### Scene 4: The Plan, Made Tangible (`TripExample.tsx`)
* **Anchor:** `#approach` (with `#how-it-works` alias).
* **Interactive Contract:** Single typed fixture driving:
  1. **Pace Mode:** `Unhurried` (3 days / 2 stops), `Balanced` (4 days / 3 stops), and `In-Depth` (5 days / 4 stops).
  2. **Daily Schedule:** Selecting Day 1, 2, or 3 inspects corresponding departure times, activities, and dwell hours.
  3. **Itemized Budget Ledger:** Fuel (modeled), Highway Tolls (verified), Lodging (estimated), and Ghat Corridor Entry Permit (`Unknown fee — not added to total`).
* **Zero Coercion Standard:** Explicitly preserves unknown fees instead of coercing missing amounts to ₹0.

### Scene 5: Practical Reassurance (`PracticalFAQ.tsx`)
* **Tone:** Restrained, thin-rule accordion with honest technical and commercial answers:
  1. Constraint solving via TSPTW and daylight windows.
  2. Non-custodial booking handoff (no payments captured).
  3. Evidence tiers (`LIVE_OFFER`, `INDICATIVE_SEARCH`, `ESTIMATED_MODEL`).
  4. Dynamic stop reordering and versioning.
  5. Supported Indian corridors.

### Scene 6: Closing Scene & Restrained Footer (`ClosingScene.tsx` & `Footer.tsx`)
* **Visual:** Full-bleed coastal sunset over Kudle Beach headlands (`konkan-sunset.jpg`, `75svh`).
* **Display Headline:** *"Where will you go next?"* in Bodoni Moda serif.
* **Footer:** Brand statement (*"We gave you memory"*), active internal navigation, and non-custodial reassurance.

---

## 4. Corridor Handoff & Dashboard Integration

* **Implementation:** `frontend/src/app/dashboard/page.tsx` was refactored with a `<Suspense>` boundary consuming `useSearchParams()`.
* **Corridor Initialization:**
  * `?corridor=rajasthan`: Sets Origin to Delhi (`28.6139, 77.2090`), destinations to Jaipur, Pushkar, and Jodhpur.
  * `?corridor=konkan`: Sets Origin to Mumbai (`19.0760, 72.8777`), destinations to Alibaug, Ratnagiri, and Goa.
  * `?corridor=western-ghats`: Sets Origin to Bengaluru (`12.9716, 77.5946`), destinations to Mysuru, Coorg, and Wayanad.
* **Security Adherence:** Non-authenticated dashboard access fails-closed to `/login?return_to=...` via `proxy.ts`. Authenticated sessions pre-populate the selected corridor immediately.

---

## 5. Contact Desk & Fail-Closed Integrity

* **Backend Contract:** `POST /api/v1/support/inquiries` in `backend/src/travel/runtime/fastapi/routes/support.py`.
* **Frontend Submission:** `ContactForm.tsx` submits through `/api/support/inquiries` Next.js proxy route, falling back to direct FastAPI base URL.
* **Zero Hallucination Standard Enforced:**
  * Successful submissions parse and display the authentic server UUID ticket (`Reference Ticket: <uuid>`).
  * Offline or rejected submissions display a truthful error message with direct email contact.
  * **The previous simulated ticket fallback (`inq_` random string) was completely deleted.**

---

## 6. Verification Evidence Manifest

### A. Dedicated Visual Verification Suite (`node scripts/verify_cinematic_marketing.js`)
* **Test Result:** **14 PASSED, 0 FAILED** across desktop, mobile, corridor handoff, contact desk, and about pages.
* **Generated Screenshots:** Stored in `docs/marketing/evidence/`:

| Screenshot File | Resolution / Mode | Verified Visual Element |
| :--- | :--- | :--- |
| `desktop_01_hero_arrival.png` | 1440 × 900 (2x) | Kolukkumalai tea hill hero, Bodoni H1, CTAs, location stamp |
| `desktop_02_hero_route_revealed.png` | 1440 × 900 (2x) | ScrollTrigger progress: framed card + animated SVG route with 4 named nodes |
| `desktop_03_destination_filmstrip.png` | 1440 × 900 (2x) | Scene 2 filmstrip showing Western Ghats spread and partial Rajasthan peek |
| `desktop_04_filmstrip_rajasthan.png` | 1440 × 900 (2x) | Carousel Next navigation: Rajasthan spread with Amber Fort courtyard |
| `desktop_05_editorial_pause.png` | 1440 × 900 (2x) | Scene 3: Warm paper `#F3EFE6`, portrait coffee detail, Bodoni statement |
| `desktop_06_trip_example_balanced.png` | 1440 × 900 (2x) | Scene 4: Balanced pace (4 days / 3 nights) with daylight guidance |
| `desktop_07_trip_example_unhurried.png` | 1440 × 900 (2x) | Scene 4: Unhurried pace toggle (3 days / 2 nights) updated timeline |
| `desktop_08_trip_example_indepth.png` | 1440 × 900 (2x) | Scene 4: In-depth pace (5 days / 4 nights) + Ghat Corridor unknown fee badge |
| `desktop_09_practical_faq.png` | 1440 × 900 (2x) | Scene 5: Expanded FAQ item with constraint-solving explanation |
| `desktop_10_closing_scene.png` | 1440 × 900 (2x) | Scene 6: Gokarna sunset coastline, Bodoni display heading, footer |
| `mobile_01_hero.png` | 390 × 844 (2x) | Mobile natural-flow vertical stack, zero horizontal overflow |
| `mobile_02_filmstrip.png` | 390 × 844 (2x) | Mobile touch scroll-snap destination cards |
| `mobile_03_trip_example.png` | 390 × 844 (2x) | Mobile trip planner example and budget card |
| `corridor_handoff_rajasthan.png` | 1440 × 900 (1x) | Authenticated dashboard pre-populated with Delhi → Jaipur → Pushkar → Jodhpur |
| `corridor_handoff_konkan.png` | 1440 × 900 (1x) | Authenticated dashboard pre-populated with Mumbai → Alibaug → Ratnagiri → Goa |
| `contact_form_submitted.png` | 1440 × 900 (2x) | Live inquiry confirmation displaying verified server ticket UUID |
| `about_page.png` | 1440 × 900 (2x) | About page with Bodoni display typography and warm paper corridor section |

### B. Master Verification Pipeline (`./verify.sh`)
* **Step 1 (Linter & Typecheck):** Ruff clean, Mypy clean across 45 backend source files (`Success: no issues found`).
* **Step 2 (Backend Unit Tests):** 45 of 45 tests passed in 2.98s (`pytest -v`).
* **Step 3 (Next.js Production Build):** Turbopack production compilation succeeded in 1.2s; all 15 static/dynamic pages generated without type errors.
* **Step 4 (Playwright E2E Suite):** 8 of 8 E2E journeys passed in 8.5s (`app.spec.ts`).
* **Final Status:** `=== ALL VERIFICATIONS PASSED SUCCESSFULLY ===`
