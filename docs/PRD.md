# SWENA Product Requirements Document (PRD)

**Version:** 2.0  
**Owner:** Product Management  
**Status:** Approved Product Specification  
**Architecture:** Next.js 16 + FastAPI + PostgreSQL/PostGIS + Upstash Redis  
**Companion Specifications:** [`docs/END-TO-END-JOURNEYS.md`](END-TO-END-JOURNEYS.md) | [`docs/IMPLEMENTATION-AUDIT.md`](IMPLEMENTATION-AUDIT.md) | [`docs/PRODUCTION-COMPLETION-PLAN.md`](PRODUCTION-COMPLETION-PLAN.md)

---

## 1. Product Vision & Audience

SWENA is a sovereign, deterministic travel intelligence platform designed for India-first domestic travelers. It serves families, road-trippers, cultural heritage explorers, and solo travelers planning journeys across Indian transport corridors.

### 1.1 Core Value Proposition
* **Truth in Mathematics:** Eliminates travel hallucinations. If a highway toll, monument permit, or parking charge is unquoted, SWENA explicitly preserves it as an unknown cost rather than quietly defaulting to ₹0 to claim false budget compliance.
* **Corridor Topography:** Accounts for realistic terrain transit times (e.g. Western Ghats hairpins, monsoon road conditions) rather than flat straight-line approximations.
* **Non-Custodial External Handoff:** SWENA respects merchant sovereignty. It operates zero internal checkout, holds zero customer payments, and adds zero deceptive markups. All bookings hand off directly to official verified portals (IRCTC, airlines, hotel direct).

---

## 2. Comprehensive Requirements Matrix (P01–P18, Operational, Marketing)

| Req ID | Title | Detailed Acceptance Criteria | Journey Ref |
| :--- | :--- | :--- | :--- |
| **P01** | **Trip Brief Creation & Validation** | User creates or edits a trip specifying origin hub, ordered destinations, dates, and party. Ambiguous city names prompt targeted clarification (e.g. "Madikeri vs. Virajpet"). Children require explicit age inputs; party size determines minimum room allocations. Date order validated (`end_date >= start_date`). | J01, J04 |
| **P02** | **Hard Constraint vs. Preference Separation** | System clearly distinguishes fixed requirements (e.g., "Must travel by train", "Only vegetarian dining") from soft preferences. The optimizer cannot silently drop or relax a hard constraint to make an infeasible itinerary feasible. | J04, J07 |
| **P03** | **Multi-Modal Transit & Lodging Comparison** | Compares flights, trains, buses, and hotels matching exact party size and travel dates. Every option displays its explicit four-tier evidence classification (`LIVE_OFFER`, `INDICATIVE_SEARCH`, `EDITORIAL_DISCOVERY`, `UNAVAILABLE`), checked timestamp, and merchant identity. | J09 |
| **P04** | **Road Transit & Fuel Budget Engine** | Calculates road transit fuel consumption from route distance and vehicle efficiency ($\text{Liters} = \text{Dist} / \text{Mileage}$, $\text{Cost} = \text{Liters} \times \text{Price}$). Allows user overrides for mileage and fuel price. Preserves unverified tolls as explicit unknown budget lines. | J08 |
| **P05** | **Distinct Vehicle Transit Modes** | Petrol cars, petrol motorcycles, pedal bicycles, and walking are modeled as distinct transit profiles. Motorcycles are never routed on bicycle paths or expressways with two-wheeler prohibitions. EV modeling is explicitly rejected. | J04, J06 |
| **P06** | **Place Discovery & Categorization** | Curated points of interest, viewpoints, restaurants, and hidden gems validated for geographic detour time, operating hours uncertainty, and provider attribution. Explains why a destination fits the brief. | J09 |
| **P07** | **Personalized Daily Itinerary** | Monotonically sequenced daily timeline with realistic transit buffers, dwell times, meal stops, and monument opening windows. Round trips must include the final return-to-depot transit leg. | J06 |
| **P08** | **Budget Completeness & Zero-Coercion** | Monetary math strictly uses `Decimal`. If any mandatory cost is unknown, `is_complete` is set to `False` and the subtotal is labeled: `"Known/estimated subtotal; tolls unknown"`. Impossible budgets trigger an explicit infeasibility notice. | J08 |
| **P09** | **Partial Plan Resilience & Section Retry** | If an external provider times out or fails, successful sections remain fully usable. The user can retry failed sections independently without re-solving the entire plan or losing previous edits. | J05 |
| **P10** | **Direct Official Booking Handoff** | Generates verified deep-links to official portals (IRCTC, state tourism, airlines). Button copy explicitly distinguishes context preservation: "Book on [Merchant]" (`EXACT`) vs. "Search on [Merchant]" (`PARTIAL` / `GENERIC`). Zero internal checkout. | J12 |
| **P11** | **Spatial Route Canvas & Map Pins** | Interactive Leaflet map canvas rendering road network geometries, sequenced stop markers, and detour POIs. Straight-line fallback links must be explicitly labeled illustrative. Display rights strictly respected. | J06 |
| **P12** | **Privacy Preferences & Consent Gateway** | Long-term storage of user preferences (dietary, driving pace) requires explicit, affirmative opt-in with policy version tracking. Users can review, edit, or delete stored preferences at any time. | J14 |
| **P13** | **Sourced Itinerary Export (PDF)** | Generates version-bound, publication-grade PDF containing brief summary, complete timetable, itemized budget with unknown-cost warning banner, PostGIS evidence hashes, and official merchant links. | J11 |
| **P14** | **Seasonality & Weather Intelligence** | Sourced regional seasonal guidance (best months, monsoon indices) clearly distinguished from short-term real-time forecasts. Extreme weather advisories cite verified meteorological sources. | J13 |
| **P15** | **Client-Native Voice Briefing & Guide** | Speech-to-text brief dictation and text-to-speech itinerary narration using client-native Web Speech API. UI explicitly discloses that browser engines (Chrome/Edge) may process speech on remote cloud servers. | J15 |
| **P16** | **Corridor Hazard & Ghat Advisories** | Evaluates terrain hazards along transit corridors (e.g. Western Ghats fog, monsoon landslide warnings) and provides actionable driver safety advisories (e.g. low-beam lights, daylight transit recommendations). | J13 |
| **P17** | **Public Itinerary Sharing & Mobile QR** | Generates high-entropy public share URL and client-side SVG QR code for roadside companions. Public projection strictly strips all owner PII and private budget notes. Access is instantly revocable by owner. | J10 |
| **P18** | **Sovereign OAuth 2.1 BFF Identity** | Identity delegated to SWYRA Auth ([SGOD-pro/OAuth2.1](https://github.com/SGOD-pro/OAuth2.1)) using RFC 8252 PKCE S256 and secure HttpOnly session cookies. Backend FastAPI validates RS256 JWT signatures offline against remote JWKS. | J02 |
| **OP01** | **Durable Contact & Support Pipeline** | Operational support form on `/contact` persists user inquiries directly to PostgreSQL `contact_inquiries` table with ticket UUID and outbox notification. Zero fake success toasts. | J15 |
| **OP02** | **Full Account & Data Deletion Cascade** | User-initiated deletion completely purges all user data across PostgreSQL (`trips`, `versions`, `itineraries`, `shares`), Redis session keys, and S3 export objects within DPDP / GDPR guidelines. | J14 |
| **OP03** | **Service Health & Readiness Telemetry** | Automated `/health` and `/ready` probes monitoring PostgreSQL connection pool depth, Redis reachability, and disk space for container orchestration. | Ops |
| **M01** | **Editorial Marketing Experience** | Atmospheric, high-craft landing page styled with SWENA 2.0 Forest Night theme, responsive GSAP 3D corridor cards, and honest capability disclaimers. | J01 |
| **M02** | **Deterministic 50-Scenario Benchmark Runner** | Comprehensive test runner evaluating all 50 cases in `tests/evaluations/cases.json`, asserting scenario-specific invariants (child occupancy, budget ceilings, monument hours). | Ops |

---

## 3. Product Scope & Explicit Non-Goals

### 3.1 What SWENA Deliberately Does NOT Do
1. **No Internal Checkout or Payment Processing:** SWENA will never collect credit card details, initiate UPI payment intents, hold customer funds, or charge booking fees. All financial transactions occur directly on official supplier portals.
2. **No Invented Price Holds or Availability Guarantees:** Indicative web observations are never disguised as live inventory. SWENA never promises that a fare will remain available after the traveler leaves the platform.
3. **No EV Battery, Range, or Charging Modeling:** Per architectural decision D010, electric vehicle energy consumption and highway charging networks are deferred. An electric vehicle input is explicitly rejected rather than coerced into a petrol model.
4. **No Unrestricted Autonomous Crawling:** SWENA strictly abhors scraping protection bypass, CAPTCHA solving, or rate limit evasion. Blocked providers immediately return `UNAVAILABLE`.
5. **No Social Feeds or Marketplace Clutter:** SWENA is an operational travel workspace, not a social network or generic tour aggregator.

---

## 4. Marketed Claims vs. Capability Release Gates

Marketing copy on `/` and `/about` must accurately reflect verified operational capabilities. Marketing statements are gated on corresponding technical verification:

| Public Marketing Claim | Underlying Capability Gate | Required Verification Before Publication |
| :--- | :--- | :--- |
| *"Zero-coercion budget engine"* | Gate 2 (Financial Integrity) | Unit tests prove unquoted tolls are preserved as explicit unknowns and never summed as ₹0. |
| *"Verified road corridor mathematics"* | Gate 2 (Solver Soundness) | OR-Tools scheduler executes real road distance matrices, time windows, and mandatory return legs. |
| *"Direct official supplier handoff"* | Gate 4 (Merchant Handoff) | Domain allowlist enforces valid HTTPS redirects to official portals (IRCTC, airlines) with context badges. |
| *"Complete roadside mobile route"* | Gate 5 (Public Sharing) | `/trips/[id]` dynamically renders sanitized public trip plans and revokes instantly when commanded. |
| *"Sovereign delegated identity"* | Gate 1 (Auth Barrier) | Next.js BFF and FastAPI cryptographically verify RS256 JWKS signatures; zero client owner trust. |

---

## 5. Measurable Performance Targets (SLOs)

Performance targets are operational objectives measured under representative network conditions, not speculative promises:

1. **First Useful Route & Map Display:** P95 $\le 3.5$ seconds (Cohort: 1–3 destinations, cached road matrix).
2. **Cached Itinerary Reroute:** P95 $\le 2.0$ seconds (Cohort: Stop reordering within existing corridor).
3. **Full Multi-Modal Plan Compilation:** P95 $\le 20.0$ seconds (Cohort: Complete 5-day itinerary with lodging, transit, and POI discovery).
4. **Initial Partial Plan Visibility:** Visible useful skeleton within $\sim 10.0$ seconds via Server-Sent Events.
5. **Frontend Web Vitals (Marketing):** Largest Contentful Paint (LCP) $\le 2.5$s, First Input Delay (FID) / INP $\le 200$ms, Cumulative Layout Shift (CLS) $\le 0.1$.
