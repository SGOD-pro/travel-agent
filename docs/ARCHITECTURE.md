# SWENA Architecture & System Design

**Version:** 2.0  
**Status:** Canonical Architectural Baseline  
**Pattern:** Hexagonal / Clean Architecture (Ports and Adapters) with Next.js BFF and PostgreSQL Outbox.

---

## 1. Current Baseline vs. Target Production Architecture

### 1.1 Current Implementation State (Commit `8b7592f`)
```
[Browser Client]
   │
   ├── (1. Direct fetch to FastAPI: bypasses auth header) ────────┐
   │                                                             │
   └── (2. Session Cookie) ──► [Next.js BFF / proxy.ts]          │
                                  │                              │
                                  └── (Base64 decode only)       │
                                                                 ▼
[FastAPI Runtime: routes/trips.py] ◄─────────────────────────────┘
   │ (Trusts client-supplied owner_id; no auth dependency)
   ├── Enqueues Job in DB (never claimed by worker)
   └── Synchronously calls trip_planning_workflow.ainvoke() inline
          │
          └── [Linear LangGraph Graph] (No checkpoints, static hotel estimate)
```

### 1.2 Target Production Architecture (Milestone C1–C8)
```
[Browser Client: Next.js 16 + GSAP]
       │
       ▼ (HttpOnly, Secure, SameSite=Lax Session Cookie)
[Next.js BFF Gateway: App Router]
       │
       ├── Auth: Validates RS256 JWT Signature against SWYRA Auth JWKS (/.well-known/jwks.json)
       └── API Proxy: Forwards Authorization: Bearer <validated_jwt> to Backend
              │
              ▼
[FastAPI HTTP Resource Server]
       │
       ├── Auth Dependency: Validates Token & Extracts user_id (Rejects client owner_id)
       ├── Unit of Work: Validates Ownership & Commits TripBrief Version N
       └── Job Enqueue: Inserts Job into PostgreSQL & returns 202 Accepted { run_id }
              │
              ▼
[Authoritative PostgreSQL 16 + PostGIS (Aiven)]
   ├── trips, trip_versions, itineraries, budget_lines, public_shares
   └── jobs (claimed via SELECT ... FOR UPDATE SKIP LOCKED)
              │
              ▼
[Asynchronous Background Worker Pool (ECS Tasks)]
       │
       ├── LangGraph Execution Engine (PostgreSQL Checkpoints)
       ├── Bounded Provider Adapters (Mapbox, OSRM, Places, SerpAPI)
       └── Constraint Solver: Google OR-Tools (Time Windows & Depot Return Legs)
              │
              ▼
[Server-Sent Events (SSE) / Polling] ──► [Browser Planning Workspace]
```

---

## 2. Domain Modules, Ports, and Adapters

```text
backend/src/travel/
  ├── domain/                     # Pure business logic, zero external framework imports
  │     ├── money.py              # Decimal arithmetic, BudgetLine, BudgetSummary, Currency
  │     ├── trips.py              # Trip, TripBrief, TripVersion, TripState, TransportMode
  │     ├── routing.py            # VehicleFuelProfile, RoadSegment, RoadBudgetCalculator
  │     ├── jobs.py               # Job, JobStatus, OutboxEvent
  │     └── artifacts.py          # Artifact, ArtifactStatus, ArtifactFormat
  ├── application/
  │     ├── ports/                # Abstract interfaces (Clean Architecture Boundaries)
  │     │     ├── repository.py   # TripRepositoryPort, JobRepositoryPort, ArtifactRepositoryPort
  │     │     ├── unit_of_work.py # UnitOfWorkPort
  │     │     ├── cache.py        # CachePort
  │     │     ├── providers.py    # RoutingProviderPort, PlacesProviderPort, LodgingProviderPort
  │     │     └── llm.py          # LLMPort (Structured inference interface)
  │     └── services/
  │           ├── scheduler.py    # OR-Tools TSPTW itinerary solver
  │           └── pdf_export.py   # ReportLab publication generator
  ├── workflows/
  │     └── trip_planning.py      # LangGraph state graph definition & checkpoints
  ├── persistence/                # Implementations of application repository ports
  │     ├── models.py             # SQLAlchemy 2.0 ORM entity definitions
  │     ├── repositories.py       # SqlAlchemyTripRepository, SqlAlchemyJobRepository
  │     └── unit_of_work.py       # SqlAlchemyUnitOfWork
  └── runtime/
        ├── fastapi/              # HTTP delivery adapter
        │     ├── app.py          # FastAPI application factory & CORS configuration
        │     ├── auth.py         # OAuth 2.1 / OIDC Bearer token validation dependency
        │     └── routes/         # trips.py, exports.py, health.py, benchmarks.py
        └── workers/
              └── worker.py       # JobWorker executing SKIP LOCKED claiming loop
```

---

## 3. Identity & Authentication Architecture (SWYRA Auth Integration)

1. **Sovereignty & Separation of Concerns:**
   * SWYRA Auth ([SGOD-pro/OAuth2.1](https://github.com/SGOD-pro/OAuth2.1)) is the external OpenID Connect identity provider.
   * Zero passwords, user credentials, or password hashes are ever stored in the `travel-agent` PostgreSQL database.
   * Users are identified internally purely by the immutable subject claim (`sub`) mapped to an internal user UUID.
2. **Next.js BFF Authentication Flow:**
   * Browser requests `/api/auth/login` $\to$ BFF generates PKCE $S256$ code challenge and cryptographically secure state cookie, redirecting to SWYRA Auth.
   * User authenticates at SWYRA Auth $\to$ redirects back to `/api/auth/callback?code=...&state=...`.
   * BFF validates state, exchanges authorization code for tokens via backchannel POST with client credentials.
   * BFF sets an encrypted, HttpOnly, Secure, SameSite=Lax session cookie (`swena_session`).
3. **FastAPI Resource Server Verification:**
   * Next.js forwards requests with `Authorization: Bearer <token>`.
   * FastAPI dependency `get_current_user` validates token offline against cached JWKS (`/.well-known/jwks.json`).
   * Validates RS256 signature, expiry (`exp`), not-before (`nbf`), issuer (`iss`), and audience (`aud` / `azp`).
   * Extracts user UUID and injects into route handlers. All trip mutations check `trip.owner_id == user.id`.

---

## 4. Geospatial Canvas & Voice Architecture

### 4.1 Geospatial Corridor Engine
* **Leaflet & CartoDB Dark Matter:** Renders high-contrast dark vector tiles matching SWENA 2.0 Forest Night (`#0D1915`).
* **PostGIS Authority:** Spatial points are indexed using GiST on `geography(Point, 4326)`. Spatial queries extract POIs within calculated corridor bounding boxes.
* **Network vs. Straight Line:** True driving paths use polyline vectors returned by routing adapters (Mapbox / OSRM). If live routing is unavailable, fallback lines are explicitly labeled "Illustrative Straight Line".

### 4.2 Voice Briefing & Guide Architecture
* **Web Speech API:** Leverages client-native `SpeechRecognition` (speech-to-text) and `SpeechSynthesis` (text-to-speech).
* **Transparent Privacy Boundary:**
  * **Browser Reality:** In Chromium-based browsers (Google Chrome, Microsoft Edge), speech audio waveforms are processed remotely by browser vendor cloud infrastructure. The UI explicitly notifies travelers: *"Speech recognition is processed by your browser engine. Audio is not recorded or stored on SWENA servers."*
  * **Backend Authority:** Zero raw audio is transmitted to or stored on SWENA backend servers. SWENA receives only the finalized text transcript submitted by the user.
