# Build phases

Version 1.0 | Release Phase 1 is delivered through the following engineering stages.

## Stage 0 — Documentation baseline (current)

Create requested specs, decisions, registry and 50 evaluation definitions. Verify internal links and JSON. Exit: documents distinguish locked decisions, defaults and unverified integrations. No production claim.

## Stage 1 — Foundation

Create one repo/application layout, dependency locks, import checks, typed domains/ports, TripBrief/deltas, Decimal money, version/validation gate, migrations/repositories, PostGIS, Redis adapter, PostgreSQL jobs/outbox and initial telemetry. Build deterministic money, conflict, lease and ownership tests. Exit: provider-independent state survives process/Redis loss; concurrent changes do not overwrite; false budget compliance is rejected.

## Stage 2 — Workflows and adapters

Build FastAPI command/query API, LangGraph persistence/replay, OR-Tools scheduling, Mapbox/OSRM adapters, Places interface and permitted display strategy, flight/hotel/rail/bus interfaces, SerpAPI and authorized Scrapling extraction. Implement fuel/mileage assumptions and toll/fee unknowns. Exit: fixture workflows produce valid partial plans; providers remain disabled until registry authorization; unknowns remain explicit.

## Stage 3 — Frontend

Implement feature modules, brief editor, route/list views, itinerary edits, budget, recommendations, evidence, approval, handoff and streaming recovery. Exit: mobile/keyboard/error state checks; no false live offers or fake confirmations; map-data display rights resolved before provider-backed mapped POIs launch.

## Stage 4 — Portable jobs and exports

Implement thin export, notification and enrichment entrypoints; benchmark runtime options. Do not automatically create three functions if measurements favor shared workers. Add permitted PDF export content, task idempotency, cancellation and replay. Exit: domain results match across local worker and handler contract harnesses; actual AWS tests separately validate packaging/cold starts.

## Stage 5 — ECS and release acceptance

Staging deployment, actual provider credentials/access verification, SWYRA security acceptance, deletion/backup drills, 50-case fixture suite, bounded live probes, SLO/cost baselines and visual regression. Exit: release gates have evidence. An unavailable rail/bus supplier can remain explicitly unavailable; coverage marketing matches registry.

## Stage 6 — EC2 profile

Add host/container deployment configuration, TLS, restart/log/backup operations and parity tests without domain changes. Exit: same image and application behavior verified under EC2/Docker.

## Later — Expansion & Real-World Readiness

The "Later" phase elevates the platform into a production-ready, competition-winning application with automated verification, interactive corridor mapping, accessibility, and sovereign identity integration, while strictly maintaining core boundaries:

### Stage Later.1 — 50-Scenario Deterministic Benchmark Runner (Completed)
- Built `travel.benchmarks.runner.BenchmarkRunner` evaluating all 50 scenarios in `tests/evaluations/cases.json`.
- Enforces invariant verification: zero-coercion preservation (unquoted tolls never become ₹0), temporal feasibility ($arrival \le departure$), price non-negativity, and anti-hallucination isolation.
- Exposed via `GET /api/v1/benchmarks` and interactive `/benchmarks` inspector UI. Achieves 100% pass rate in 31 ms.

### Stage Later.2 — Geospatial Corridor Canvas (Completed)
- Leaflet map with CartoDB Dark Matter tiles matching SWENA 2.0 `forest-night`.
- Renders sequence stops, polyline corridor traces, and recommended detour POIs with uncertainty badges.
- SSR-safe dynamic wrapper embedded in planning console and public trip pages.

### Stage Later.3 — Voice Brief Dictation & Audio Guide (Completed)
- Client-side Web Speech API integration (`SpeechRecognition` + `SpeechSynthesis`).
- Speech-to-text voice brief dictation into planning parameters.
- Text-to-speech audio guide narrating finalized schedules and safety advisories without external cloud audio processing.

### Stage Later.4 — Corridor Weather & Ghat Hazard Advisory (Completed)
- Regional corridor terrain intelligence: live stop temperature, humidity, monsoon indices, and Western Ghats hazard warnings (e.g. foggy pass alerts, low-beam headlight recommendations).

### Stage Later.5 — Public Shareable Itinerary & QR Code (Completed)
- Deep-link sharing at `/trips/[id]` with client-side SVG/Canvas QR Code generation (`qrcode` package) for mobile smartphone scanning on the road.

### Stage Later.6 — SWYRA OAuth 2.1 / OIDC Integration (Underway)
- Integrate with [SWYRA Auth (SGOD-pro/OAuth2.1)](https://github.com/SGOD-pro/OAuth2.1) via standard OAuth 2.1 (PKCE + Authorization Code Flow).
- Next.js BFF route handlers (`/api/auth/login`, `/api/auth/callback`, `/api/auth/me`, `/api/auth/logout`) with secure HttpOnly cookies.
- Offline RS256 JWT validation using remote JWKS (`/.well-known/jwks.json`).
- Zero credentials stored in `travel-agent` database; identity delegated completely to SWYRA Auth.

### Future Expansion Gates (Explicit Approvals Required)
- **International Expansion**: Multi-currency forex conversion, international border/visa regulations, and multi-hub flight connections require separate domain models.
- **EV Feasibility**: Electric vehicle battery state-of-charge, degradation, charging curves, and highway charger availability remain deferred per decision D010.
- **Vector Retrieval**: Optional pgvector embeddings for unstructured traveler reviews require separate storage acceptance.
- **Booking & Payments**: STRICT NON-GOAL. No internal checkout, payment capture, or price locking. All commercial fulfillment remains non-custodial external handoff to verified supplier portals (IRCTC, KSTDC).
