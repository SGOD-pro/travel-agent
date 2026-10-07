# boundaries.md

## Shared Harness

### Can
- authenticate
- create executions
- decompose requests
- choose required capabilities
- dispatch tasks
- enforce limits
- resume workflows
- provide observability
- coordinate proactive suggestions

### Cannot
- automatically execute all three products
- replace product business logic
- invent destination facts

## Travel Planner

### Can
- understand trip intent
- build a destination-specific research plan
- research weather and seasonal conditions
- research holidays, festivals and events
- discover famous/must-visit places and buildings
- discover nearby places and day trips
- discover local food and restaurant experiences
- discover hidden/lesser-known candidates
- discover scenic, sunrise/sunset and photoshoot points
- research local culture, markets, nature and activities
- research practical access, opening hours, fees, permits and crowd/safety signals
- use verified places/facts
- create schedules
- request route optimization
- calculate budgets
- synthesize a memorable TripPlan

### Cannot
- invent evidence
- perform route mathematics itself when Route Optimizer owns it
- bypass verification
- claim a place is truly “hidden” solely because a model generated the label
- recommend restricted/private/unsafe access as ordinary tourism
- treat stale weather/event/closure information as current

## Media Location

### Can
- inspect public media
- extract metadata/audio/frames
- OCR/ASR/vision
- research candidates
- verify location
- return VerifiedPlace[]

### Cannot
- automatically create a trip
- automatically optimize a route
- bypass access controls

## Route Optimizer

### Can
- geocode
- build route matrices
- research road/festival/event restrictions
- incorporate current route conditions when provider data supports them
- model constraints
- solve with OR-Tools
- validate route
- return OptimizedRoute

### Cannot
- automatically create a complete trip plan
- silently ignore hard constraints
- use LLM as mathematical authority

## Destination Intelligence

### Can
- coordinate category-specific research tasks
- normalize destination facts into `DestinationInsight`
- preserve source, timestamp/freshness, confidence/status and applicability
- surface candidates from mainstream, local, community and social evidence
- mark lesser-known/hidden candidates with uncertainty when the evidence is insufficient for a strong label

### Cannot
- declare a destination fact verified without evidence
- manufacture current weather/events/closures/opening hours
- treat popularity or social frequency as proof of quality
- expose private or restricted locations as normal recommendations

## Evidence Collector

### Can
- search
- fetch permitted content
- extract observations
- preserve provenance
- collect destination-specific evidence

### Cannot
- decide final truth by itself
- bypass anti-bot/CAPTCHA/access controls

## Browser

### Can
- navigate approved targets
- extract DOM/metadata
- capture screenshots

### Cannot
- unrestricted autonomous browsing
- bypass authentication/CAPTCHA

## LAYA

### Can
- classify
- select workers/tools
- gate expensive operations
- route
- triage
- decide retry/repair/continue/abstain
- select relevant destination research categories

### Cannot
- replace authorization/security
- replace OR-Tools
- replace financial arithmetic
- turn unsupported claims into verified truth

## LLM

### Can
- interpret natural language
- decompose tasks
- generate research queries
- interpret evidence
- identify potentially relevant experiences
- synthesize explanations

### Cannot
- authorize users
- own route mathematics
- own monetary arithmetic
- declare external facts verified without evidence

## Policy / Deterministic Code

Owns:

```text
authorization
idempotency
leases
hard constraints
money
route validity
verification state transitions
security invariants
retry classification
freshness rules
recommendation safety rules
```
