# Architecture

Version 1.0 | Change when system structure changes.

## Canonical structure

One modular monolith with portable execution entrypoints. Source layout planned under src/frontend and src/backend; documentation creation does not scaffold application implementations.

```text
src/
  frontend/
    app/
    features/{trip-planning,itinerary,maps,transport,places,hotels,budget,recommendations,auth}/
    components/ui/
    generated/api/
  backend/travel/
    domain/{trips,itinerary,transport,places,hotels,budget,recommendations,routing,users}/
    application/{ports,commands,queries,services}/
    workflows/{trip_planning,rerouting,provider_enrichment,approval}/
    providers/{flights,trains,buses,hotels,maps,places,search}/
    validation/
    ranking/
    persistence/{repositories,migrations,checkpoints,jobs}/
    infrastructure/{postgres,redis,storage,identity,llm,telemetry}/
    events/
    runtime/{fastapi,lambda_handlers,worker,cli}/
    bootstrap/
deployment/{local,docker,ecs,lambda,ec2}/
tests/{unit,contracts,integration,runtime_parity,evaluations}/
```

Runtime calls workflow/application code; workflow uses application ports/services; application uses domains; infrastructure implements ports. Bootstrap wires implementations. Domain code must not import FastAPI, LangGraph, AWS APIs or database clients. Module internals are private; cross-module work uses explicit public contracts. Frontend API types are generated from OpenAPI.

## Request path

1. Next.js authenticates and sends a planning command with an idempotency key.
2. FastAPI authorizes trip ownership, validates the command, persists the run/job, returns 202.
3. A worker claims the job and invokes the shared LangGraph definition.
4. Independent transport, hotel, POI and route work runs with bounded concurrency. Dependencies remain ordered: candidate discovery precedes detour matrices; validated times/costs precede final synthesis.
5. Successful sections produce persisted progress events. The client streams or polls them.
6. Validation produces an itinerary proposal. User-required approval binds its exact version.
7. A transactional commit advances canonical state; exports/handoff use that version.

## Execution profiles

| Profile | API/workflows | Bounded tasks | Durable authority |
| --- | --- | --- | --- |
| Local | Container API and worker commands | Local workers | PostgreSQL |
| ECS | Core production container API/worker | Container or Lambda adapters | Aiven PostgreSQL |
| EC2 | Same container commands/image | Container or Lambda adapters | Aiven PostgreSQL |
| Lambda | Selected bounded shared services/subworkflows | Thin handlers | Aiven PostgreSQL |

The full system is not promised as a Lambda-only deployment. Approximately three candidate workloads are exports, notifications and enrichment; creating separate functions requires measured justification. No agent-per-function design. Ordinary Lambda workers do not own a second business state machine. An AWS dispatcher invokes job IDs, not new domain implementations.

## Durable execution

Use PostgreSQL jobs and transactional outbox initially, avoiding an obligatory extra broker. Local/ECS/EC2 workers poll/claim jobs. The AWS dispatcher can invoke Lambda for eligible jobs; failed dispatch is retried from durable state. At-least-once delivery is handled by idempotent commands and lease fencing. Side effects such as notification sends use provider idempotency where supported; exactly-once external delivery is not promised.

LangGraph checkpoints live separately from domain records. A replay checks command IDs and existing commits. A process-local graph state is not authoritative. A pending human approval frees compute and resumes from persisted state later. Approval edits create new proposals rather than mutating historical evidence.

## Spatial and provider constraints

PostGIS stores permitted spatial records and uses GiST indexes for shortlist queries. Routing adapters compute network detours, restrictions and travel times. Route geometry, place centroid and entrance/access point are distinct.

Mapbox is primary routing; OSRM is a dated non-live fallback with explicit dataset version. Google Places is a POI adapter but does not grant unrestricted storage or non-Google map display. Renderer choice for Google-derived POIs remains gated on permitted usage; mapcn is only a presentation component, not route/traffic data. Do not silently add another provider or assume attribution alone resolves licensing.

## Ownership

Trips owns brief/deltas; itinerary owns stop/leg versions; transport/hotels owns normalized options; budget owns calculated breakdowns; users owns travel profiles/consent; identity remains external; LangGraph owns workflow checkpoints; infrastructure owns jobs/outbox; exports owns S3 metadata. Only authorized module repositories write their tables. Cross-module transactional changes use application services and a unit of work.

## Data rights through the graph

Persist references and permitted normalized fields, not unlimited raw provider payloads. The same rules apply to logs, Redis, LangGraph checkpoints and PDFs. Disallowed content is fetched transiently when needed. Data-use rules accompany evidence records.

## Identity & Authentication (SWYRA Auth — OAuth 2.1 / OIDC)

Identity is strictly externalized to [SWYRA Auth](https://github.com/SGOD-pro/OAuth2.1), a sovereign self-hosted OAuth 2.1 / OIDC provider:

1. **Protocol Standards**: Strict OAuth 2.1 and RFC 8252 compliance with PKCE (`code_challenge_method=S256`), exact redirect URI matching, and single-use authorization codes.
2. **Next.js BFF Integration**: Next.js App Router serves as the confidential client using the BFF pattern:
   - `/api/auth/login`: Initiates OAuth 2.1 authorization with PKCE challenge and state.
   - `/api/auth/callback`: Exchanges authorization code with SWYRA Auth token endpoint (`/api/auth/oauth2/token`) using client credentials, sets secure HttpOnly session cookie, and redirects to dashboard.
   - `/api/auth/me`: Decodes and provides current authenticated user session context.
   - `/api/auth/logout`: Clears session cookies.
3. **Resource Server Offline Validation (FastAPI)**:
   - FastAPI inspects incoming `Authorization: Bearer <token>`.
   - Validates RS256 signatures offline against SWYRA Auth JWKS endpoint (`/.well-known/jwks.json`) with caching.
   - Enforces `client_id` and `aud` binding to prevent cross-application token replay.
   - No user passwords or credentials are ever stored in the PostgreSQL travel database; users are identified purely via `sub` claims.

## Geospatial Canvas & Voice Architecture

1. **Geospatial Corridor Engine**:
   - Leaflet client-side engine with CartoDB Dark Matter tiles matching SWENA 2.0 `forest-night`.
   - PostGIS queries provide waypoints, bounding boxes, and detour spatial geometries.
   - Rendered using Next.js dynamic client-side imports (`ssr: false`) to avoid SSR window pollution.
2. **Client-Native Voice Processing**:
   - Web Speech API (`SpeechRecognition`) converts spoken briefs to structured destination/mode parameters on-device.
   - Web Speech Synthesis (`SpeechSynthesisUtterance`) reads solved itineraries aloud in natural voice.
   - Strict Privacy Boundary: Audio waveforms never leave the client browser; zero speech audio is transmitted or logged to backend servers.

