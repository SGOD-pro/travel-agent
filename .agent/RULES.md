# Agent Rules & Core Invariants

**Version:** 2.0  
**Governing Standard:** Non-negotiable principles for all automated and human contributors.

1. **Evidence Before Assertions:** Read README, these rules, BOUNDARIES, MEMORY, and relevant specs before work. A component existing, a unit test passing, and an operational user journey are three different facts.
2. **Zero Hallucination Standard:** No fabricated prices, availability, routes, source quotes, IDs, or booking confirmations. Never promote indicative evidence to live offers.
3. **Deterministic Financial Math:** Money strictly uses Python `Decimal` / currency types. Unknown costs are never zero. Preserve hard user constraints; impossible budgets must be reported.
4. **Domain Independence:** Domain logic stays runtime/provider independent. No duplicate logic per Lambda, no micro-frontends, no agent-per-function.
5. **Authoritative Persistence:** PostgreSQL is the single authoritative source of truth. Redis is strictly ephemeral (cache loss must not erase trips). No Supabase, Qdrant, or initial pgvector.
6. **Scraping & Provider Compliance:** Do not bypass blocked scraping access, CAPTCHAs, authentication, paywalls, robots/rate/access rules, or anti-bot protections. Blocked providers return `UNAVAILABLE`.
7. **Zero Secret & Privacy Leakage:** Never commit secrets, raw private GPS coordinates, unrestricted provider payloads, or hidden model reasoning. Redact dietary and accessibility details from general telemetry.
8. **Substantiated Verification:** Tests and actual measurements substantiate claims. A passing fixture is not live supplier coverage, and a base64 string decode is not a security audit.
9. **Zero-Custody Boundary:** No internal checkout, payment capture, real bookings, or charges. All commercial handoffs are direct and unmediated to official provider portals.
10. **One Canonical Server Plan:** The backend server holds mathematical and scheduling authority; the frontend must consume server plans directly rather than simulating them via client-side approximations.
11. **Honest Status Bookkeeping:** Separate working code from demo facades. A mock UI or synthetic loop must be marked honestly as `DEMO_ONLY` or `PARTIAL`.
12. **Safe Continuity:** Do not pause with "continue?" when authorized work remains within the active vertical task. Continue execution until declared acceptance evidence is verified.
