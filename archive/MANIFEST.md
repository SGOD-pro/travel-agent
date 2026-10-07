# Truth Document Manifest

These documents form the locked project foundation for the Multi-agent Travel Assistant.

```text
project.md
projectrequirement.md
architecture.md
boundaries.md
design.md
rules.md
decision.md
memory.md
phases.md
benchmark_methodology.md
core-diagrams.md
phase-diagrams.md
README.md
```

## Product scope

The system contains three independent products:

```text
Travel Planner
Social Media Location Identifier
Route Optimizer
```

The Travel Planner contains a **Destination Intelligence / Experience Discovery** capability covering, when relevant:

```text
weather / season
holidays / festivals / events
must-visit landmarks / famous buildings
nearby places / day trips
local foods / food streets
famous + local restaurants
hidden / lesser-known discoveries
scenic / sunrise / sunset / photoshoot points
culture / markets / local experiences
nature / activities
access / opening hours / fees / permits
crowds / safety / current conditions
stay / transport
```

These are not optional marketing bullets. They are part of the product requirements and architecture and must be reflected in implementation, contracts, evidence, tests and benchmarks.

`AGENTS.md` is intentionally not included because the repository already owns that file and it must not be overwritten by this package.
