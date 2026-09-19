# Boundaries

**Version:** 2.0  
**Status:** Permanent Architectural & Domain Boundaries

---

## 1. Domain Ownership

* **Trips:** Owns canonical brief, version snapshots, optimistic locking, and deltas.
* **Itinerary:** Owns monotonic timetable schedules, stop sequences, and transit legs.
* **Transport & Hotels:** Owns provider-normalized options and four-tier evidence states.
* **Budget:** Owns exact Decimal financial arithmetic, fuel math, and unknown cost tracking.
* **Places:** Owns identity and spatial records in PostGIS subject to provider display rights.
* **Recommendations:** Owns candidate ranking based on normalized scores and evidence.
* **Users:** Owns user preferences and privacy consent records.
* **SWYRA Auth:** Sole external owner of identity infrastructure (users, credentials, MFA/TOTP, OAuth 2.1 server, and JWKS). `travel-agent` consumes offline-verified JWT tokens with `sub` claims; never stores passwords or session secrets.

---

## 2. Architecture & Operational Scope

* **Hexagonal Boundaries:** Domain code must never import FastAPI, LangGraph, SQLAlchemy, AWS SDKs, or database drivers. Adapters implement typed application ports.
* **One Canonical Plan:** The backend server holds mathematical and scheduling authority. The frontend UI displays server-derived plans; client-side approximations must not impersonate server routes.
* **Execution Profiles:** ECS is the primary production runtime; Docker and EC2 share the same container image. Bounded Lambda tasks are permitted only when package size ($\le 250$ MB) and cold-start profiling justify extraction.
* **Bounded Concurrency:** Concurrency is strictly capped per worker (8 provider tasks, 2 scrape tasks/origin, 4 blocking threads, 1 browser context).

---

## 3. Product & Commercial Boundaries

* **India-First Planning & Comparison:** Unified travel workspace for Indian domestic corridors.
* **Strict Non-Custodial Handoff:** No internal checkout, payment capture, booking creation, cancellation handling, or price locks. All fulfillment transfers travelers directly to official provider portals (IRCTC, state tourism corporations, airlines).
* **Supported Vehicle Modes:** Petrol cars, petrol motorcycles, pedal bicycles, and walking. Electric vehicle (EV) energy, range, battery degradation, and charging station modeling is explicitly deferred (Decision D010).
* **Client-Native Voice Processing:** Operates via browser Web Speech API. Speech waveforms are processed by the browser's engine (which may utilize remote cloud recognition in Chrome/Edge); zero speech audio is transmitted to or stored on SWENA backend servers.
