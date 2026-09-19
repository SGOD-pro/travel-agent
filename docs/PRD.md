# Product requirements

Version 1.0 | Owner: product owner | Change when product requirements change.

## Purpose and audience

Serve India-first domestic travelers planning family, solo, group, food, heritage, nature and road trips. Unite routes, stops, accommodation, transport comparisons, budgets and editable itineraries. Reliability means transparent uncertainty and recoverable failures; neither pixel-perfect UI nor a successful HTTP response guarantees a third-party checkout.

## Release scope

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P01 | Create/edit a trip with dates, origin, destinations and party | Ambiguous cities/dates request clarification; children have ages and room occupancy |
| P02 | Separate hard constraints from preferences | Fixed hotel/vendor/mode cannot be silently substituted |
| P03 | Compare flight/train/bus/hotel options | Show evidence state, exact context, merchant and checked-at time |
| P04 | Car and motorcycle road budgets | Fuel, tolls, parking, taxes/fees and rentals are separate sourced/assumed lines |
| P05 | Distinct car, motorcycle, bicycle, walking modes | No motorcycle routed using bicycle profile; unsupported mode is explicit |
| P06 | Food, viewpoints, stops, must-visit categories and hidden gems | Identity, geographic fit, hours/access and source confidence checked |
| P07 | Personalized daily itinerary | Travel, dwell, meal/rest, opening hours and connection buffers included |
| P08 | Budget control | Unknown costs never become zero; impossible budgets are reported |
| P09 | Partial results and replanning | Keep successful sections; show failures and offer retry without losing edits |
| P10 | Official booking handoff | Destination merchant visible; no internal payment or booking confirmation |
| P11 | Map pins and routes | Correct place/access point; provider display rights respected |
| P12 | Preferences and memory | Long-term preferences require opt-in and can be edited/deleted |
| P13 | Export itinerary | Includes version, source timestamps, estimates, warnings and permitted content |
| P14 | Best months/dates suggestions | Seasonal guidance distinguished from short-term forecasts and real-time closures |
| P15 | Voice brief dictation & tour narration | Client-side Web Speech API with explicit permission; audio not recorded server-side |
| P16 | Corridor weather & ghat hazard advisory | Regional weather, monsoon risks, and Western Ghats visibility/safety advisories |
| P17 | Public shareable itinerary & mobile QR | Deep-link sharing at `/trips/[id]` with client-side QR generation for road travelers |
| P18 | Sovereign OAuth 2.1 identity integration | Delegated auth to SWYRA Auth (RFC 8252 + PKCE), zero stored credentials, offline JWKS validation |

Not every domain requires an LLM agent. Not every domain is supported by a live supplier on launch. UNAVAILABLE is a valid section state.

## Evidence UX contract

LIVE_OFFER means a qualifying authorized supplier response, not a guaranteed held price. INDICATIVE_SEARCH means search/scraped observations. EDITORIAL_DISCOVERY means reference/discovery information and other non-offer records with an explicit data_kind. UNAVAILABLE requires a reason. Never turn indicative search into a live offer because several sites agree.

## Road estimate behavior

Travelers can enter actual mileage and fuel price. Otherwise use a versioned, sourced regional/vehicle-class default labeled as an assumption. Do not label a guessed number “Indian average”. Display low/base/high scenarios when uncertainty is material. A missing toll or fee rate makes that component unknown. Do not apply generic taxes already embedded in fuel/merchant prices a second time.

## Failure and approval behavior

If a rail provider fails and the user requires rail, keep that requirement unresolved. Suggest alternatives only with permission to relax it. Human approval is required for proposed hard-constraint changes, material plan acceptance and memory consent. No financial authorization UI exists in this release.

## Non-goals and deferred features

No checkout, payment capture, booking creation, cancellations/refunds, universal inventory coverage, live crowd-density promise, autonomous emergency response, guaranteed safe route, EV battery/charging planning, vector store or independent frontend deployments. Voice brief dictation and audio narration are implemented purely client-side via native Web Speech API with zero server-side recording. No inferred approval for checkout exists merely because external booking handoffs exist. Competitor research is outstanding; no unverified superiority claims enter product copy.

## Success criteria

Approved SLO targets: first useful map/route P95 <=3.5s; cached reroute P95 <=2s; full enriched plan initial P95 <=20s; useful partial enrichment visible around 10s. Measure client and server timings separately and publish cache/workload/provider conditions. No latency results have been measured.

Release requires passing the 50-case deterministic evaluation pack, provider contract tests, ownership isolation, evidence integrity, recoverable job execution, auth security acceptance and permitted map/provider display. Passing fixtures alone does not prove live provider health.
