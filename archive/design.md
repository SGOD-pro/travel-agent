# design.md

## Visual identity

The product is cinematic/editorial rather than generic SaaS.

Existing visual foundation:

- Forest Night / deep green
- warm paper
- sage secondary tones
- terracotta accent
- Bodoni Moda for display
- Manrope for interface text
- map/corridor storytelling
- restrained GSAP motion

## UX philosophy

The UI should communicate:

```text
DISCOVER → UNDERSTAND → DECIDE → EXPERIENCE → REMEMBER
```

not only:

```text
SEARCH → BOOK
```

## Memory-first destination experience

The interface should make the user feel that the product understands the **character of the destination**, not just its coordinates.

A destination view should be able to surface, when relevant:

```text
☁ Weather / best conditions
🎉 Festivals / holidays / events
🏛 Must-see landmarks / famous buildings
📍 Nearby places / day trips
🍜 Local foods / food streets
🍽 Famous + local restaurants
🌿 Hidden / lesser-known discoveries
🌅 Sunrise / sunset / scenic viewpoints
📸 Photoshoot / creator points
🎭 Culture / markets / local experiences
🏞 Nature / activities / adventure
🚗 Practical movement / road conditions
🏨 Stay areas
💰 Costs / fees / booking requirements
```

Do not display all categories as a checklist by default. Surface the experiences that are relevant to the traveler and destination, with evidence/freshness indicators where appropriate.

## Trust UX

Time-sensitive information should visibly communicate freshness, for example:

```text
Weather checked: 10 min ago
Road restriction observed: today
Festival source published: 2 days ago
Opening hours: source updated/observed when available
```

Recommendation cards should distinguish:

```text
Verified
Likely / supported
Candidate
Conflicted
Stale
Unavailable
```

## Hidden-gem UX

Do not pretend every obscure location is verified or secret.

Use language such as:

```text
Lesser-known candidate
Local/community surfaced
Not widely listed on mainstream platforms
Evidence limited — verify locally
```

when appropriate.

## Product UX

Each capability has its own result experience, but the visual system is shared.

### Travel Planner

Show the trip as a **story of experiences**, not only a timetable:

```text
Destination context
→ what makes it special
→ must-see
→ local taste
→ lesser-known discovery
→ photo/memory moment
→ practical movement
→ stay / cost
→ day-by-day experience
```

### Media Location

Show:

```text
identified place
→ visual/audio evidence
→ verification status
→ useful nearby experiences
→ optional next-step suggestion
```

The nearby experience preview does not automatically start Travel Planner.

### Route Optimizer

Show:

```text
optimized route
→ travel time/distance
→ constraints encountered
→ closures/restrictions
→ evidence freshness
→ optional conversion into a complete trip experience
```

## Motion

Use motion to reveal the journey and destination story, not to decorate every interaction.

Respect `prefers-reduced-motion`.
