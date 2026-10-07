# architecture.md

## Canonical architecture

```text
USER
→ API
→ ORCHESTRATOR / LANGGRAPH
→ LLM + LAYA
→ POLICY
→ DISPATCHER
→ SQS
→ ONLY REQUIRED PRODUCT(S)
→ DESTINATION INTELLIGENCE / EVIDENCE / VERIFICATION
→ WORKFLOW RESUME
→ RESULT
```

## Important architectural rule

The three products are **not a single pipeline**.

They are independent capabilities connected by contracts.

Shared infrastructure is reusable; product execution is demand-driven.

## Product capability map

```text
                    ┌──────────────────────────┐
                    │     SHARED HARNESS        │
                    │ API / LangGraph / LLM     │
                    │ LAYA / Policy / SQS       │
                    │ Evidence / Verification   │
                    └────────────┬─────────────┘
                                 │
          ┌──────────────────────┼──────────────────────┐
          │                      │                      │
          ▼                      ▼                      ▼
   Travel Planner        Media Location        Route Optimizer
          │                      │                      │
          │                      │                      │
          ▼                      ▼                      ▼
 Destination            Media evidence         Routing + live/
 Intelligence            + verification         event constraints
```

## Destination Intelligence is a first-class Travel capability

Travel Planner should not be modeled as only:

```text
places → route → schedule → budget
```

It includes a destination research/experience layer:

```text
Trip Brief
   ↓
Research Plan
   ↓
┌─────────────────────────────────────────────────────────────┐
│ Destination Intelligence                                    │
│                                                             │
│ Weather / Season       Holidays / Festivals / Events        │
│ Famous / Must Visit    Nearby Places / Day Trips            │
│ Local Food             Restaurants / Food Streets            │
│ Hidden / Lesser-known  Scenic / Photo / Sunset / Sunrise    │
│ Culture / Markets      Nature / Adventure / Activities      │
│ Practical Access       Opening Hours / Fees / Permits       │
│ Crowds / Safety        Stay Areas / Transport                │
└──────────────────────────────┬──────────────────────────────┘
                               ↓
                    Evidence + Verification
                               ↓
                  Experience Candidate Set
                               ↓
                 LAYA / Policy-controlled ranking
                               ↓
                 Route / Schedule / Budget
                               ↓
                         TripPlan
```

Each category is **conditional**. The system does not call every provider or research every category for every trip. LAYA + Policy select relevant work based on the user's request, destination, season, time window and risk/cost.

## Destination Intelligence data classes

The system should model at least:

```text
WeatherCondition
SeasonalCondition
Holiday
Festival
Event
OpeningHours
TemporaryClosure
Landmark
FamousBuilding
NearbyPlace
DayTrip
LocalFood
Restaurant
FoodStreet
HiddenPlaceCandidate
ScenicPoint
PhotoPoint
SunrisePoint
SunsetPoint
CulturalExperience
NatureExperience
Activity
PracticalAccessFact
CrowdSignal
SafetyAdvisory
StayArea
TransportOption
CostFact
```

Time-sensitive records require freshness/observed-at information.

## Target backend

```text
backend/
├── src/
│   ├── contracts/
│   ├── domain/
│   │   ├── destination/
│   │   ├── experience/
│   │   └── travel/
│   ├── application/
│   │   ├── ports/
│   │   ├── execution/
│   │   ├── evidence/
│   │   ├── verification/
│   │   └── decisions/
│   ├── workflows/
│   │   ├── trip_planner/
│   │   ├── media_location/
│   │   └── route_optimizer/
│   ├── functions/
│   │   ├── api/
│   │   ├── orchestrator/
│   │   ├── decision_router/
│   │   ├── evidence_collector/
│   │   ├── browser/
│   │   ├── media_extractor/
│   │   ├── media_analyzer/
│   │   ├── destination_intelligence/
│   │   └── optimizer/
│   ├── infrastructure/
│   │   ├── aws/
│   │   ├── database/
│   │   ├── providers/
│   │   └── models/
│   ├── observability/
│   ├── config/
│   └── utils/
├── tests/
└── infrastructure/
```

## Product contracts

```text
VerifiedPlace[]
DestinationInsight[]
RouteRequest
OptimizedRoute
TripPlan
```

`DestinationInsight[]` is a reusable evidence-backed contract for experience intelligence. It is not a fourth product.

## Shared infrastructure

```text
API
Orchestrator
LLM Gateway
Decision Gateway / LAYA
Policy
Execution Core
SQS
Evidence Collector
Verification
Observability
PostgreSQL
S3
```

## Product internals

### Media Location
```text
media_extractor
→ media_analyzer
→ evidence_collector
→ verification
```

### Route Optimizer
```text
geocoder
→ routing provider
→ live/current conditions
→ festival/event/road evidence
→ constraints
→ OR-Tools
→ validation
```

### Travel Planner
```text
LLM intent
→ research plan
→ Destination Intelligence tasks
→ Evidence Collector
→ Verification
→ DestinationInsight[] / VerifiedPlace[]
→ LAYA selection
→ Route Optimizer when needed
→ schedule
→ budget
→ memory/experience synthesis
→ TripPlan
```

## Runtime

Use Lambda for bounded workloads only after package/startup/memory/timeout measurements.

Browser/media/solver workloads may require container runtime.

The runtime choice is an engineering measurement, not a branding requirement.

## Persistence

```text
PostgreSQL = authoritative state
S3 = large artifacts
Redis = ephemeral cache only
```

## Async execution

```text
TaskEnvelope
→ SQS
→ Worker
→ WorkerResult
→ CompletionEvent
→ LangGraph resume
```

No arbitrary direct worker-to-worker coupling.
