# Boundaries

Version 1.0 | Rarely changes.

## Domain ownership

Trips owns canonical brief/versions/deltas. Itinerary owns schedules/stops/legs. Transport and hotels own provider-normalized options. Budget owns arithmetic and assumptions. Places owns identity/spatial records subject to rights. Recommendations owns ranking. Users owns travel profiles/consents. SWYRA Auth alone owns identity infrastructure (users, sessions, credentials, MFA/TOTP, OAuth 2.1 authorization server, and JWKS). Travel-agent only consumes offline-verified JWT tokens with `sub` claims; never stores passwords or session secrets.

Application services coordinate modules through public interfaces and a unit of work. Runtime controllers/handlers validate transport concerns only. LangGraph chooses workflow paths but imports ports/services rather than supplier SDKs. Infrastructure adapters implement ports. No repository imports into domain; no frontend direct DB/secret access.

## Operational scope

One Next.js application and shared backend source. ECS is primary core runtime; local/EC2 share container image. Lambda is a legitimate bounded execution target with boundaries based on profiling. Browser-heavy code and solver dependencies must not load in unrelated handlers. Threads are bounded I/O helpers, not uncontrolled parallelism.

## Product boundary

India-first planning/comparison/discovery/export/handoff. No internal checkout, payment, booking confirmation or price locks. Petrol cars and motorcycles plus ordinary bicycles/walking; EV energy/range/charging deferred. Motorcycle legality cannot be inferred from bicycle routing. Client-native voice dictation and narration operate strictly on-device with zero server-side recording. No inferred approval for checkout exists merely because external booking handoffs exist.

## Rights and uncertainty

Provider access is gated per registry environment/market/capability. Brand names in candidate lists do not authorize execution. Google-derived data cannot automatically populate a non-Google map or permanent store. Unknown fees/hours/access remain unknown; do not relax fixed constraints silently.
