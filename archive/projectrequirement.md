# projectrequirement.md

## Product model

Three independent capabilities:

```text
Travel Planner
Media Location Identifier
Route Optimizer
```

The Orchestrator chooses the minimum capability set required by the user's request.

# 1. Travel Planner

## 1.1 Input

Natural-language trip request, which may include:

```text
origin / destination
travel dates or flexible period
duration
traveler profile
budget
interests
food preferences
mobility/accessibility needs
transport preference
stay preference
must-visit places
places discovered from social media
route constraints
experience priorities
```

## 1.2 Travel Planner responsibility

Travel Planner must produce more than a sequence of places. It should create an evidence-backed **Destination + Experience Plan** and, when requested, a practical `TripPlan`.

The research plan should dynamically consider the following intelligence categories.

### A. Time, weather and season intelligence

Research when relevant:

- current weather
- forecast for the travel window
- seasonal weather patterns
- rainfall / heat / cold considerations
- weather-dependent experiences
- sunrise / sunset timing when useful
- monsoon / snow / storm / extreme-weather considerations
- seasonal closures or reduced availability

Weather is time-sensitive and must carry freshness/timestamp information.

### B. Holidays, festivals and events

Research:

- public holidays
- regional holidays
- religious/cultural festivals
- destination-specific festivals
- concerts/events/fairs when relevant
- school-holiday/peak-season effects where useful
- festival crowd impact
- festival road closures / no-entry / traffic restrictions
- temporary opening/closing changes

Event claims must be date-scoped and evidence-backed.

### C. Must-visit and famous places

Discover and verify:

- iconic landmarks
- famous buildings
- monuments
- heritage sites
- temples/churches/mosques/other culturally significant places
- famous beaches, waterfalls, lakes, hills and viewpoints
- museums/galleries
- signature attractions

The system should distinguish:

```text
must-see
popular
optional
skip-if-time-limited
```

rather than treating every attraction equally.

### D. Nearby places and geographic discovery

For a destination, research useful places around the primary location, including:

- nearby attractions
- nearby towns/areas
- day-trip opportunities
- clusters of attractions
- places naturally compatible with the planned route
- geographically close experiences that reduce unnecessary travel

Nearby does not mean merely “within a radius”; travel time and actual road access matter.

### E. Local food intelligence

Research:

- regional specialties
- must-try local dishes
- street food
- traditional food experiences
- famous food streets/markets
- local drinks/non-alcoholic specialties
- seasonal foods
- dietary-compatible local options

The system should explain **what to try and why it is locally meaningful**, not just return restaurant names.

### F. Restaurant intelligence

Research when relevant:

- famous restaurants
- respected local eateries
- traditional restaurants
- food streets
- seafood/meat/vegetarian specialists
- budget/mid-range/special-occasion options
- restaurants near planned activities
- opening hours and reservation requirements when available

Restaurant availability must not be invented. Real-time reservation searches are separate from general recommendation research.

### G. Hidden / lesser-known place discovery

The product should actively search for:

- hidden gems
- lesser-known viewpoints
- small beaches/coves
- local trails
- quiet cultural spots
- village experiences
- small local food places
- non-obvious scenic locations
- places surfaced by local/community/social sources but not prominent on mainstream booking platforms

Important rule:

> **“Hidden” is a discovery label, not a fact supplied by the LLM.**

A place may be labeled lesser-known only when the evidence supports the characterization or when the system clearly presents it as a lower-visibility candidate with uncertainty.

The system must not encourage trespassing, unsafe access, illegal access, or visiting restricted/private locations.

### H. Photography and memory-making points

Research:

- sunrise viewpoints
- sunset viewpoints
- scenic photo points
- architecture/photo backdrops
- beach/cliff/mountain viewpoints
- iconic framing locations
- photography-friendly areas
- creator/content-friendly spots
- time-of-day recommendations

The output should distinguish between:

```text
official/viewpoint location
publicly accessible scenic point
user/community-discovered candidate
restricted/private location
```

### I. Experience and local-culture intelligence

Where relevant:

- local markets
- handicrafts
- cultural performances
- heritage walks
- local neighborhoods
- workshops/classes
- local traditions
- nightlife
- nature experiences
- adventure activities
- family activities
- couple-oriented experiences
- solo-friendly experiences

### J. Practical destination intelligence

Research where relevant:

- local transport options
- parking
- walking feasibility
- road conditions
- current restrictions
- opening/closing times
- entry fees
- permits
- booking requirements
- accessibility
- safety advisories
- crowd/peak-time considerations
- connectivity/remote-area considerations
- cash/card/payment considerations when reliably sourced

## 1.3 Experience ranking

Candidate experiences should be ranked using deterministic/policy-controlled signals such as:

```text
user interest match
geographic usefulness
time-window fit
weather/event compatibility
source quality
freshness
accessibility
cost
crowd/practicality
uniqueness / local character
memory value signals
```

“Memory value” must not become an unsupported psychological score. It should be an explainable product heuristic derived from observable factors such as uniqueness, scenic value, cultural significance, local character, user preference and experience diversity.

## 1.4 Must do

- understand intent, dates, origin/destination, travelers and constraints
- create a destination-specific research plan
- research relevant weather/season conditions
- research relevant holidays/festivals/events
- discover famous/must-visit places
- discover nearby places and day-trip opportunities
- discover local foods and food experiences
- discover restaurants/local eateries when useful
- actively search for lesser-known/hidden candidates
- discover scenic and photoshoot points
- research practical access/opening/fee/permit information where relevant
- collect and verify external evidence
- create schedules
- use real route calculations where route order/time matters
- calculate costs deterministically
- clearly preserve unknown costs
- produce a final `TripPlan`

## 1.5 Internal modules

```text
LLM
LAYA
Destination Intelligence
Evidence Collector
Verification
Route Optimizer
Schedule Engine
Budget Engine
Experience Ranker
PostgreSQL
S3
```

Destination Intelligence may be implemented as application/domain logic rather than one monolithic worker. Its category-specific research tasks should remain separately observable.

## 1.6 APIs/tools

```text
LLM provider
weather provider
holiday/event sources
SerpAPI
Scrapling
Playwright escalation
routing provider
OR-Tools through Route Optimizer
maps/geocoder sources
PostgreSQL
S3
```

Provider capabilities and terms must be verified before enabling them in production.

# 2. Social Media Location Identifier

## Input

Public social-media URL.

## Pipeline

```text
URL
→ fetch media
→ metadata
→ audio
→ frames/scenes
→ LAYA gating
→ OCR / ASR / Vision
→ candidate locations
→ evidence search
→ verification
→ VerifiedPlace[]
```

## Internal modules

```text
media_extractor/
media_analyzer/
evidence_collector/
decision_router/
```

## APIs/tools

```text
media source
OCR
ASR
Vision
SerpAPI
Scrapling
Playwright
S3
PostgreSQL
LAYA
```

## Requirement

A model guess alone cannot become a verified location.

A verified place can later feed the Travel Planner or Route Optimizer only when the user requests/accepts that composition.

# 3. Route Optimizer

## Input

Locations + route constraints.

## Pipeline

```text
locations
→ geocode
→ routing matrix
→ current/festival road research
→ constraints
→ OR-Tools
→ cost
→ validation
→ OptimizedRoute
```

## Internal modules

```text
routing.py
constraints.py
solver.py
costs.py
repository.py
```

## APIs/tools

```text
geocoder
routing API
SerpAPI
Scrapling / Playwright when needed
OR-Tools
LAYA
PostgreSQL
```

## Festival requirement

For Durga Puja and similar events, research verified road closures/restrictions and use them as route constraints. Do not merely mention that a route "may be blocked" without evidence/status.

# 4. Composition

Supported examples:

```text
Media only:
URL → Media Location → VerifiedPlace

Route only:
Stops → Route Optimizer → OptimizedRoute

Travel only:
Brief → Travel Planner → TripPlan

Media + Route:
URL → VerifiedPlace[] → Route Optimizer

Media + Travel:
URL → VerifiedPlace[] → Travel Planner

Travel + Route:
Trip requirements → Travel Planner → RouteRequest → Route Optimizer

All:
Media → VerifiedPlace[] → Travel Planner → RouteRequest → Route Optimizer → Travel Planner
```

## Important

The presence of Destination Intelligence does **not** turn Travel Planner into a mandatory dependency for Media or Route.

# 5. Proactive suggestions

After a successful result, the system can suggest one clearly relevant next capability.

Examples:

```text
Media result
→ "Plan a trip there?"

Route result
→ "Turn these stops into a complete experience plan?"

Travel result
→ "Optimize the route for your trip?"
```

Never execute the suggested capability without user acceptance.

# 6. Shared requirements

Every task has:

```text
task_id
execution_id
parent_task_id
attempt
idempotency_key
deadline
```

Every workflow has bounded loops:

```text
iterations
replans
repairs
tool calls
wall time
cost
tokens
same-action repeats
```

All meaningful external claims must have evidence/provenance.

Time-sensitive facts such as weather, events, closures, opening hours and availability must include freshness/observed-time metadata.
