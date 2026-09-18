# Technical requirements

Version 1.0 | Change when technical requirements change.

## Runtime and module contracts

Python 3.12 baseline with Pydantic v2; verify support of pinned dependency versions. FastAPI handles HTTP. LangGraph owns workflow sequencing, branching and HITL. OR-Tools solves constrained candidate itineraries. SQLAlchemy/async PostgreSQL driver and Alembic are proposed persistence implementation choices; LangGraph uses its supported PostgreSQL saver in a separate schema. Confirm checkpoint driver/pool compatibility in integration tests.

Domain models must run without cloud SDK initialization. Adapters implement typed ports: repositories, unit of work, providers, clock, object store, LLM, event publisher and job dispatcher. Use Decimal throughout monetary logic, decimal strings on the wire and explicit ISO 4217 currencies. OR-Tools integer costs use documented currency minor units and rounding. Geographic distance is not money and may use floating-point calculations.

## Bounded concurrency

Use async I/O for independent provider requests; reuse connection pools and enforce per-provider plus global admission limits. Only blocking I/O goes to a bounded ThreadPoolExecutor. Cancelling an await does not kill its underlying thread: configure socket timeouts and avoid overlapping retries. CPU-heavy Python parsing/optimization goes to an isolated worker/process where appropriate; threads are not an automatic CPU speedup.

Initial tunable experiment defaults: global provider tasks 8 per worker, per-origin scrape tasks 2, blocking pool 4 threads, browser contexts 1 per worker. These are not supplier entitlements; the stricter provider limit always wins. Production values require memory, timeout and load measurements. Cap queued work, response bytes, browser lifetime, candidate count and request fan-out. Do not share unsafe browser/session objects between threads.

## Search/extraction pipeline

SerpAPI discovery -> canonical URL validation -> registry authorization -> ordinary async fetch or permitted browser rendering -> site-specific extraction -> normalized evidence -> context matching -> deduplication -> comparison. Run independent permitted sites concurrently; fetching a discovered URL depends on discovery completion.

Extract route/date, passengers, room/occupancy, fare/room type, currency, inclusions, availability wording, source time and merchant link. If dates or occupancy cannot be established, retain a generic indicative observation or reject it from exact-trip comparison. Adaptive selectors cannot establish semantic correctness: validate labels, units and fixture expectations. A sudden implausible price shift or missing context quarantines a parser result. Never take the minimum across incompatible fare conditions.

Scrapling anti-bot/challenge evasion is outside the approved project policy even if the library offers it. Blocks return UNAVAILABLE/BLOCKED. Do not launch bulk crawls of named suppliers merely because their public homepage exists.

## Failure and retry ownership

Each request has a deadline propagated to tools. Provider-specific timeouts replace a universal 3.5s timeout. One layer owns provider retries; avoid retries multiplied by SDK, adapter, graph and worker. Retry transient idempotent operations with bounded exponential backoff/jitter and Retry-After; no blind retry of 401/403/challenges. Persistent job retries handle worker failure separately. Circuit breakers expose status and recover through limited probes.

## Road budget model

fuel_liters = route_distance_km / mileage_km_per_liter

fuel_cost = fuel_liters * fuel_price_per_liter

road_total = fuel_cost + applicable_tolls + parking + rental + applicable_fees_and_taxes

All inputs carry provenance, units, effective date, location and vehicle class. Mileage must be positive. User overrides remain distinct from sourced averages. Do not assume all motorcycles pay the car toll, or that every road is toll-free. Rate validity depends on vehicle class, plaza, direction, date and return/pass rules. Route providers may identify a toll road without providing its charge. Unknown entries propagate incomplete budget status. No live fuel/toll source is verified yet.

Normalize currencies using a sourced timestamped FX rate before aggregating; preserve original amounts and conversion/rounding evidence. Fixed costs count per vehicle/room when appropriate, not once per passenger. Estimate scenarios are not actual-trip spending guarantees.

## Jobs, state and streaming

Persist accepted commands and durable jobs transactionally. Workers claim jobs using PostgreSQL row locking/leases with heartbeat, fencing generation and bounded attempts. Job completion checks active lease ownership. Duplicate delivery is expected; domain commands are idempotent. Jobs that exhaust attempts enter failed state with a replay action. A recovery scan reclaims expired leases. Redis cannot be the only lease/job authority.

Persist progress events with monotonic per-run sequence. SSE supports Last-Event-ID and client deduplication; expired event cursors return a snapshot/reconnect instruction. Browser disconnect does not cancel a run; explicit cancellation does. Provider results bind to trip version and cannot silently overwrite a newer edit.

## Quality and performance

Trace trip/run/delta IDs, provider latency, evidence class, cache hit, retries, parser version, LLM usage, solver duration, queue delay and cold-start duration. Do not log raw personal data or scraped bodies. Profile initialization time, package size, peak RSS, warm/cold P95, provider overhead and per-run cost before splitting Lambda workloads. No fixed tokens-to-dollar assumption is accepted.
