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

## Later

Voice, international expansion, richer season/weather feeds, optional pgvector, EV feasibility and booking/payment integrations require their own requirements and decisions. No inferred approval for checkout merely because handoff exists.
