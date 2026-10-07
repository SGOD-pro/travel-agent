# project.md

## Identity

This is a **travel intelligence, discovery and memory-making product**, not simply an itinerary generator.

> **We give you memories.**

The product should help a traveler discover not only **where to go**, but **what is worth experiencing there, when to experience it, how to move between experiences, what local things to eat, where to stay, what is special or hidden, and what real-world conditions may change the experience**.

A trip plan is therefore only one output. The deeper product is **destination understanding + experience discovery + practical execution + memory creation**.

## Three independent products

1. **Travel Planner** — creates evidence-backed trips and destination experiences.
2. **Social Media Location Identifier** — identifies places seen in public social-media content.
3. **Route Optimizer** — computes practical routes under real-world constraints.

They can run:

```text
ONE product
TWO products
ALL THREE
```

according to the user's actual request.

They do **not** run all three every time.

## What “Travel Planner” actually means

Travel Planner is not limited to:

```text
hotel + transport + itinerary
```

It should build a **Destination Intelligence Layer** containing, when relevant:

```text
Weather / season / current conditions
Holiday / festival / event calendar
Opening hours / closures / temporary restrictions
Must-visit landmarks and famous buildings
Nearby places worth visiting
Local attractions and experiences
Local food and regional specialties
Famous restaurants / food streets / local eateries
Hidden gems / lesser-known places
Scenic viewpoints / sunset / sunrise locations
Photography and photoshoot points
Nature / beaches / waterfalls / trails / viewpoints
Culture / heritage / markets / nightlife / local activities
Family / couple / solo / accessibility suitability
Crowd / peak-time / practical visit considerations
Distance / travel time / route feasibility
Stay options and area-level lodging suitability
Costs, fees and availability where evidence exists
```

The system should not force every category into every trip. It selects the categories that are relevant to the destination, traveler, season, request and available evidence.

### Memory-first principle

The system should optimize for **experiences that are likely to make the trip memorable**, not merely for the maximum number of attractions.

Examples:

```text
Goa
→ beach + sunset viewpoint + local seafood + lesser-known coastal spot + photo point

Kolkata during Durga Puja
→ famous pandals + current crowd/road restrictions + food + cultural experience + optimized route

A hill destination
→ weather + sunrise point + scenic trail + local food + hidden viewpoint + practical road conditions
```

A “hidden gem” is never treated as true merely because a model calls it hidden. It must have attributable evidence, an appropriate confidence/status, and freshness information.

## Core experience

The product should understand what the traveler actually needs and use the minimum necessary capabilities.

After completing a request, the system may proactively suggest a useful next step, but suggestions require user acceptance.

Example:

```text
User: "Where is this Instagram place?"

Media Location
→ VerifiedPlace
→ answer

Assistant:
"Would you like me to plan a trip there this week or this month?"
```

If the user says yes:

```text
→ Travel Planner starts
```

If no:

```text
→ finish
```

## Target users

Travelers, road trippers, festival visitors, families, solo travelers, couples, culture/food explorers, photographers/content creators, and people discovering destinations through social media.

## Product philosophy

Optimize for:

- discovery
- destination intelligence
- local character
- real-world conditions
- useful experiences
- practical movement
- trust and evidence
- memorable journeys
- evidence instead of confident guesses

The product should answer the deeper question:

> **“What can make this trip worth remembering?”**
