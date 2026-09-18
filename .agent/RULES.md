# Agent rules

Version 1.0 | Rarely changes.

1. Read README, these rules, BOUNDARIES, MEMORY and relevant specs before work. Latest explicit user decisions supersede earlier docs; record resulting changes.
2. No fabricated prices, availability, routes, source quotes, IDs or booking confirmations. Never promote indicative evidence to live offers.
3. Money uses Decimal/currency; unknown cost is not zero. Preserve hard user constraints.
4. Domain logic stays runtime/provider independent. No duplicate logic per Lambda, no micro-frontends, no agent-per-function.
5. PostgreSQL is authoritative. Redis loss must not erase trips. No Supabase/Qdrant/pgvector initially.
6. Do not bypass blocked scraping access, CAPTCHAs, auth, paywalls, robots/rate/access rules or anti-bot protections. Permission to use Scrapling is not permission to use every feature it offers.
7. Never commit secrets, raw private GPS, unrestricted provider payloads or hidden model reasoning.
8. Tests and actual measurements substantiate claims. A fixture pass is not live supplier coverage or a security audit.
9. No deployment, external messages, real bookings, charges or destructive migrations without authorization appropriate to the action.
10. Preserve user edits. Keep changes scoped and reviewable. Update relevant specs and current memory after material work.
11. Do not repeatedly reopen locked decisions. Track genuine missing access/rights/configuration as specific gates with an owner and next action.
