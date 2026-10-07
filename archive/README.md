# Travel Agent

> **We give you memories.**

This is a **travel intelligence and memory-making system**, not merely an itinerary generator.

Three independent products:

- ✈️ Travel Planner
- 📱 Social Media Location Identifier
- 🗺️ Route Optimizer

They can run independently, in pairs, or together depending on the user's request.

## What makes the Travel Planner different

The Travel Planner builds destination intelligence around the trip, when relevant:

```text
weather / season
holidays / festivals / events
must-visit landmarks / famous buildings
nearby places / day trips
local foods / food streets
famous + local restaurants
hidden / lesser-known discoveries
sunrise / sunset / scenic points
photoshoot / creator points
culture / markets / local experiences
nature / activities
opening hours / closures / fees / permits
crowds / practical access / safety signals
stay areas / transport
```

The system does not blindly research every category. It selects relevant work and verifies external claims.

## Core architecture

```text
User
→ API
→ Orchestrator
→ LLM + LAYA
→ Policy
→ required product(s)
→ Destination Intelligence / Evidence / Verification
→ result
```

The system does not automatically run all three products.

## Example

```text
"I saw this place on Instagram"
→ Media Location
→ identify + verify
→ return details
→ "Want me to plan a trip there?"
```

If accepted, Travel Planner starts.

For a Durga Puja route request:

```text
stops
→ Route Optimizer
→ route data + festival closure research
→ OR-Tools
→ optimized route
```

No Travel Planner is required.

## Rebuild

This repository is being refactored from its previous prototype toward the locked architecture.

Read:

```text
project.md
projectrequirement.md
architecture.md
boundaries.md
rules.md
decision.md
memory.md
phases.md
benchmark_methodology.md
core-diagrams.md
phase-diagrams.md
```

Start at Phase 0.
