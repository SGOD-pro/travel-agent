# decision.md

## Locked architectural decisions

### D1 — Three independent products

Travel Planner, Media Location Identifier and Route Optimizer are independent capabilities.

They may compose through contracts, but they do not form one mandatory pipeline.

### D2 — Minimum capability execution

The Orchestrator runs only the capability set required by the request.

### D3 — Memory-first Travel Planner

Travel Planner is a destination-intelligence and experience-planning system, not only an itinerary generator.

It must be able to research and reason over relevant:

```text
weather
season
holidays
festivals/events
famous/must-visit places
nearby places/day trips
local foods
restaurants/food streets
hidden/lesser-known places
scenic/sunrise/sunset points
photoshoot/creator points
culture/markets
nature/activities
practical access/opening/fees/permits
crowd/safety conditions
stay areas
transport
```

### D4 — Conditional research

The system must not blindly call every data source for every destination. LAYA + Policy select relevant research categories and gate expensive/current-data operations.

### D5 — Evidence-backed discovery

Destination recommendations are claims. They require attributable evidence and freshness appropriate to the claim.

### D6 — Hidden places are candidates unless proven

“Hidden”, “secret” or “lesser-known” is not an LLM fact. The system must preserve evidence and uncertainty.

### D7 — Time-sensitive truth

Weather, holidays/events, road restrictions, closures, opening hours and availability are freshness-sensitive. Their records carry observed/published timestamps and freshness status.

### D8 — Deterministic authority

LLM/LAYA cannot replace deterministic authority for authorization, money, geospatial math, hard constraints, verification state transitions or security invariants.

### D9 — Shared DestinationInsight contract

Destination Intelligence is a Travel capability and shared evidence contract, not a fourth public product.

### D10 — Composition

Canonical composition contracts remain:

```text
VerifiedPlace[]
DestinationInsight[]
RouteRequest
OptimizedRoute
TripPlan
```
