# API specification

Version 1.0 | Proposed /api/v1 contract | Change when contracts change.

## Common protocol

JSON camelCase on wire; UUID IDs; ISO 8601 timestamps; ISO dates; money amount as decimal string and currency code. Authenticate via configured OIDC access token or validated BFF session. Never accept ownerId as authorization. All trip subresources authorize ownership. Cookie-authenticated mutations need CSRF protection. Errors include code, message, details, requestId and retryable; do not expose provider secrets.

POST commands require Idempotency-Key scoped to actor + operation. Reuse with identical canonical payload returns the original response; a changed payload returns 409 IDEMPOTENCY_CONFLICT. Mutations based on a snapshot require baseVersion. Version conflict returns 409 VERSION_CONFLICT and currentVersion. Schema errors return 422. Rate limit returns 429 plus Retry-After. Unknown optional fields are rejected in command schemas to catch typos.

## Endpoints

| Method/path | Input | Success | Important failures |
| --- | --- | --- | --- |
| POST /trips | TripBriefInput | 201 tripId, version=1, brief | 422 ambiguous/invalid |
| GET /trips | cursor, limit <=100 | 200 owned trips, nextCursor | 401 |
| GET /trips/{id} | none | 200 currentVersion, brief, latestPlan | 404 absent/not visible |
| POST /trips/{id}/deltas | baseVersion, changes, evidenceRefs | 201 deltaId, status, validation | 409/422 |
| GET /trips/{id}/deltas/{deltaId} | none | 200 proposal and outcomes | 404 |
| POST /trips/{id}/plans | baseVersion | 202 runId, eventsUrl, statusUrl | 409 |
| GET /runs/{runId} | none | 200 status, sectionStates, latestSequence | 404 |
| GET /runs/{runId}/events | Last-Event-ID header | 200 text/event-stream | 410 cursor expired |
| POST /runs/{runId}/cancel | reason | 202 cancellationRequested | 409 terminal |
| POST /trips/{id}/approvals | deltaId, baseVersion, proposalHash, decision | 200 committedVersion or rejection | 409 stale; 422 infeasible |
| POST /trips/{id}/reroutes | baseVersion, currentLocation, remainingStopIds | 202 runId | 409/422 |
| GET /trips/{id}/itineraries/{planId} | none | 200 versioned itinerary and budget | 404 |
| POST /trips/{id}/sections/{section}/retry | baseVersion, previousRunId | 202 runId | 409/429 |
| POST /trips/{id}/handoffs | tripVersion, optionId | 201 intentId, merchant, url, evidence, contextWarnings | 422 unsafe/unmatched link |
| POST /trips/{id}/exports | tripVersion, itineraryId, format=pdf | 202 artifactId, jobId | 409 |
| GET /artifacts/{artifactId} | none | 200 state and short-lived downloadUrl if ready | 404/410 |
| GET /benchmarks | none | 200 50-case invariant report, passRate, durationMs | 500 |
| GET /api/auth/login | none | 302 redirect to SWYRA Auth gateway with PKCE | 500 |
| GET /api/auth/callback | code, state | 302 redirect to /dashboard with HttpOnly session cookie | 400 invalid code |
| GET /api/auth/me | session cookie | 200 authenticated user profile (sub, email) | 401 unauthenticated |
| POST /api/auth/logout | session cookie | 200 cleared session cookie | 200 |
| GET /me/preferences | none | 200 preferences and consent | 401 |
| PUT /me/preferences | values, consentDecision, policyVersion | 200 updated preferences | 422 |
| DELETE /user/me | explicit deletion scope | 202 deletionJobId | 401 |
| GET /deletions/{id} | none | 200 per-store completion status | 404 |

Endpoints describe application contracts, not existing routes. OpenAPI and generated client types are produced during implementation.

## Representative money/evidence record

```json
{
  "amount": "1200.00",
  "currency": "INR",
  "evidenceClass": "INDICATIVE_SEARCH",
  "dataKind": "BUS_PRICE",
  "provider": "example-approved-source",
  "sourceUrl": "https://example.com/route",
  "observedAt": "2026-09-12T10:00:00Z",
  "expiresAt": null,
  "queryContext": {"departureDate": "2026-11-10", "adults": 1},
  "inclusions": [],
  "unknowns": ["taxes", "seat availability"]
}
```

This is synthetic contract data, not a fare observation. Unknown expiry is null, never an invented 15-minute lock. Numeric estimates include assumption IDs. Non-price unavailable records do not require an amount.

## TripBriefInput details

origin and destinations: name, provider refs, optional resolved coordinates, timezone and resolution status. dates: start/end or flexible-window intent; planning requires resolved dates. travelers: adults >=1, children as age list, room allocations. budget: amount/currency and hardLimit. modes enum: CAR_PETROL, MOTORCYCLE_PETROL, BICYCLE, WALK, TRAIN, BUS, FLIGHT. Unknown fuel type requests clarification. EV modes are rejected as unsupported in this release, not mapped to petrol. fixedSelections and preferences are separate objects; coordinate ranges and date order are validated.

## Progress contract

SSE event types: run.started, section.updated, proposal.ready, approval.required, run.completed, run.failed, run.cancelled. Envelope: schemaVersion, runId, tripId, baseVersion, sequence, occurredAt, type, data. IDs monotonically increase per run; delivery may repeat, client deduplicates. Heartbeats are transport keepalives without fake business progress. A 410 cursor response supplies a status snapshot endpoint. Polling returns the same section state model.

## Handoff contract

Validate merchant allowlist and each redirect; display context match as EXACT, PARTIAL or GENERIC. Revalidate only through the provider capable of checking that offer. Any changed amount is shown; a configurable material-change threshold controls prominence, not whether change is disclosed. HEAD 200 alone never certifies a booking link. No API endpoint creates a booking, charges a card or declares merchant confirmation.
