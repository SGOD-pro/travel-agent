# benchmark_methodology.md

## Benchmark principle

Benchmark the system as both:

1. three independent products; and
2. a composable travel intelligence system.

Do not benchmark only final latency. Measure correctness, evidence quality, freshness, decision quality, operational reliability and memory-making usefulness.

## Travel Planner metrics

### Requirement satisfaction

Measure whether the result correctly covers applicable:

```text
weather/season
holidays/festivals/events
must-visit landmarks/buildings
nearby places/day trips
local foods
restaurants/food streets
hidden/lesser-known candidates
scenic/sunrise/sunset points
photoshoot points
culture/markets
nature/activities
practical access/opening/fees/permits
crowd/safety conditions
stay/transport
budget
route feasibility
```

### Quality metrics

- task success
- requirement satisfaction rate
- factual accuracy
- evidence correctness
- citation/evidence coverage
- freshness correctness for time-sensitive claims
- false verification rate
- stale-fact rate
- destination diversity/coverage
- hidden-place candidate precision
- restaurant recommendation relevance
- food/dish identification accuracy
- schedule validity
- route feasibility
- budget correctness
- user preference fit
- experience diversity
- latency p50/p90/p95/p99
- cost/tokens

“Memory value” should be evaluated with explicit rubrics/user studies rather than pretending it is an objective ground-truth scalar.

## Media metrics

- location top-1 accuracy
- top-k recall
- false-positive rate
- verification precision
- OCR/ASR/vision contribution
- evidence coverage
- latency/cost

## Route metrics

- route feasibility
- total distance/time
- hard constraint violations
- closure/restriction handling accuracy
- matrix/provider correctness
- solver success/timeouts
- cost calculation correctness

## Shared reliability metrics

- retry count
- repair/replan count
- duplicate task rate
- lease expiry
- worker crash recovery
- queue delay
- provider failure rate
- workflow resume correctness
- idempotency correctness
- evidence conflict handling
- abstention rate

## Failure injection

Inject:

```text
weather provider timeout
weather stale response
holiday/event source conflict
road closure provider failure
429
5xx
malformed provider response
duplicate task
lease expiry
worker crash
S3/DB failure
LLM failure
LAYA low confidence
conflicting evidence
solver timeout
browser failure
media extraction failure
```

## Golden scenarios

Include at least:

```text
Goa: food + hidden places + photo points + weather-aware planning
Durga Puja Kolkata: pandals + festival restrictions + food + route optimization
Hill destination: weather + sunrise + scenic trail + local food
Instagram discovery: media location → optional travel
Route-only: multiple stops under current restrictions
Travel + Route: destination intelligence → route solver → final trip
```

Do not benchmark hidden chain-of-thought.
