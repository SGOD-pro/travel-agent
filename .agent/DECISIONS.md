# Decision Log

**Policy:** Append important decisions; never rewrite history to hide supersession. Date: 2026-09-12 (Initial) / 2026-09-19 (Audited).

| ID | Status | Decision and Reason |
| :--- | :--- | :--- |
| D001 | Locked by user | One repo, one feature-modular frontend, shared modular-monolith backend; avoid fragmented implementations. |
| D002 | Locked by user | LangGraph single workflow owner; FastAPI HTTP; no competing business orchestrator. |
| D003 | Locked by user | Aiven PostgreSQL/PostGIS authoritative, Redis ephemeral, S3 objects; no Supabase/Qdrant/initial pgvector. |
| D004 | Locked by user | SWYRA external identity candidate; separate MongoDB; production security acceptance required. |
| D005 | Locked by user | Four-tier evidence: LIVE_OFFER, INDICATIVE_SEARCH, EDITORIAL_DISCOVERY, UNAVAILABLE; no silent promotion. |
| D006 | Locked by user | Registry and 50-case evaluation required; no assumed India inventory access. |
| D007 | Locked by user | ECS core; shared code supports Docker/EC2 and bounded Lambda tasks. |
| D008 | Latest user refinement | Profile growing features before splitting Lambda; candidate workers are not an instruction to create a function for every feature. |
| D009 | Latest user refinement | Petrol motorcycle/car fuel estimate with editable sourced/default mileage, plus applicable tolls/taxes/fees. |
| D010 | Latest user refinement | Defer e-bike/electric-car energy/range/charging modeling; supersedes earlier EV feasibility requirement. |
| D011 | Locked by user | SerpAPI + permitted Scrapling for parallel discovery/extraction; observed official-site price stays indicative. |
| D012 | Locked by user | External booking handoff only; supersedes PDF payment/Stripe/booking-execution/price-lock design. |
| D013 | Locked by user | India-first scope, 3.5s/2s/20s P95 targets with partial enrichment around 10s; not measured performance. |
| D014 | Engineering baseline | PostgreSQL jobs/outbox, leases/fencing/idempotency provide portable recovery without another mandatory broker. |
| D015 | Integration gate | Google Places display/storage rights with selected map renderer unresolved; adapters can proceed independently. |
| D016 | Engineering baseline | Async provider I/O, bounded blocking thread pool and browser isolation; no unbounded gather/thread fan-out. |
| D017 | Deferred | Vector retrieval, voice and checkout require later evidence/specification; no automatic scope expansion. |
| D018 | Historical Prototype | Prototype 50-scenario benchmark runner (`travel.benchmarks.runner`) created; evaluated via loop on synthetic trip. (Audited in D023). |
| D019 | Historical Prototype | Prototype Leaflet geospatial canvas created with CartoDB Dark Matter styling. (Audited in D023). |
| D020 | Historical Prototype | Web Speech API prototype and public shareable route `/trips/[id]` created. (Audited in D023). |
| D021 | Historical Prototype | SWYRA Auth BFF routes (`/api/auth/*`) created; token payload decoding added. (Audited in D023). |
| D022 | Historical Prototype | Initial Playwright test suite created in `frontend/e2e/app.spec.ts`. (Audited in D023). |
| D023 | Canonical Audit (2026-09-19) | **Reality Audit & Implementation State Corrections:** Codebase inspection at commit `8b7592f` identified that D018 looped a single synthetic case rather than asserting case invariants; D020 `/trips/[id]` rendered a static Karnataka fixture; D021 `/api/auth/me` decoded JWTs without RS256 signature verification, and FastAPI trip routes accepted client `owner_id`; D022 used synthetic cookie injection. All claims of "Phase 1–5 completed" are corrected to reflect verified status. Approved the canonical completion plan (Milestones C0–C8) in `docs/PRODUCTION-COMPLETION-PLAN.md` to guide production delivery. |

---

## Runtime Split ADR Standard
Future runtime split ADRs must include: workload, package/import/RSS profile, cold/warm latency, cost, alternatives considered, before/after result, rollback path, and affected contracts.
