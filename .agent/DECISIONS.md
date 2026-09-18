# Decision log

Append important decisions; never rewrite history to hide supersession. Date: 2026-09-12.

| ID | Status | Decision and reason |
| --- | --- | --- |
| D001 | Locked by user | One repo, one feature-modular frontend, shared modular-monolith backend; avoid fragmented implementations |
| D002 | Locked by user | LangGraph single workflow owner; FastAPI HTTP; no competing business orchestrator |
| D003 | Locked by user | Aiven PostgreSQL/PostGIS authoritative, Redis ephemeral, S3 objects; no Supabase/Qdrant/initial pgvector |
| D004 | Locked by user | SWYRA external identity candidate; separate MongoDB; production security acceptance required |
| D005 | Locked by user | LIVE_OFFER, INDICATIVE_SEARCH, EDITORIAL_DISCOVERY, UNAVAILABLE; no silent promotion |
| D006 | Locked by user | Registry and 50-case evaluation required; no assumed India inventory access |
| D007 | Locked by user | ECS core; shared code supports Docker/EC2 and bounded Lambda tasks |
| D008 | Latest user refinement | Profile growing features before splitting Lambda; candidate workers are not an instruction to create a function for every feature |
| D009 | Latest user refinement | Petrol motorcycle/car fuel estimate with editable sourced/default mileage, plus applicable tolls/taxes/fees |
| D010 | Latest user refinement | Defer e-bike/electric-car energy/range/charging modeling; supersedes earlier EV feasibility requirement |
| D011 | Locked by user | SerpAPI + permitted Scrapling for parallel discovery/extraction; observed official-site price stays indicative |
| D012 | Locked by user | External booking handoff only; supersedes PDF payment/Stripe/booking-execution/price-lock design |
| D013 | Locked by user | India-first scope, 3.5s/2s/20s P95 targets with partial enrichment around 10s; not measured performance |
| D014 | Engineering baseline | PostgreSQL jobs/outbox, leases/fencing/idempotency provide portable recovery without another mandatory broker |
| D015 | Integration gate | Google Places display/storage rights with selected map renderer unresolved; adapters can proceed independently |
| D016 | Engineering baseline | Async provider I/O, bounded blocking thread pool and browser isolation; no unbounded gather/thread fan-out |
| D017 | Deferred | Vector retrieval, voice and checkout require later evidence/specification; no automatic scope expansion |

Future runtime split ADRs must include workload, package/import/RSS profile, cold/warm latency, cost, alternative considered, before/after result, rollback and affected contracts.
