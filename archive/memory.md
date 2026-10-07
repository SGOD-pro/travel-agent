# memory.md

## Product memory

The product's central promise is:

> **We give you memories.**

The system should remember and reason over the user's active trip context during a workflow, including relevant preferences and accepted discoveries, without turning memory into an excuse to run unnecessary products.

## Destination intelligence memory

A destination research result should preserve:

```text
destination_id
category
claim
source
evidence
evidence_timestamp
observed_at / published_at when available
freshness_status
verification_status
confidence
applicability
season/time window
geographic scope
user relevance
```

## Categories

```text
weather
season
holiday
festival
event
landmark
famous_building
nearby_place
day_trip
local_food
restaurant
food_street
hidden_place_candidate
scenic_point
sunrise_point
sunset_point
photo_point
cultural_experience
nature_experience
activity
practical_access
opening_hours
fee
permit
crowd_signal
safety_advisory
stay_area
transport
```

## Evidence statuses

```text
VERIFIED
PARTIALLY_VERIFIED
CONFLICTED
STALE
UNVERIFIED
UNAVAILABLE
```

## Important distinction

A memory/context record is not automatically truth.

The system must distinguish:

```text
user preference
model inference
candidate discovery
external observation
evidence-backed claim
verified fact
```

## Privacy

Do not retain unnecessary sensitive personal data. User-controlled preferences and trip context should have explicit lifecycle/retention rules.
