# Current project memory

Updated 2026-09-12. This is implementation status, not durable user profiling.

## Completed

- Documentation pack, provider registry, and 50 scenario definitions.
- Stage 1 Foundation: Scaffolded isolated `backend/` and `frontend/` folders.
- Initialized Python 3.12 virtual environment and dependencies using `uv` (`pyproject.toml`).
- Implemented pure domain contracts in `backend/src/travel/domain/` (`Money`, `BudgetLine`, `BudgetSummary` with zero-coercion and non-hallucination guarantees; `Trip`, `TripBrief`, `TripVersion`, `TripDelta`; `Job`, `OutboxEvent`).
- Implemented application ports: `CachePort`, `TripRepositoryPort`, `JobRepositoryPort`, `UnitOfWorkPort`.
- Built Redis adapters: `AsyncRedisAdapter` (local Docker) and `UpstashRedisAdapter` (production Upstash REST HTTP SDK).
- Built PostgreSQL persistence layer: SQLAlchemy 2.0 ORM models, Repositories with optimistic concurrency (409 Conflict) and SKIP LOCKED lease claiming, Unit of Work, and initial Alembic migration (`0001_initial_schema.py`).
- Configured `deployment/local/docker-compose.yml` (PostgreSQL 16 + PostGIS and Redis).
- Created deterministic unit test suite in `backend/tests/unit/` (16 passing tests, 100% database-free).
- Verified with Ruff (`All checks passed!`) and Mypy strict mode (`Success: no issues found in 18 source files`).

- Stage 2 Workflows & Adapters:
  - Road transit math and fuel calculations in `domain/routing.py` (`fuel_liters = distance / mileage`, `fuel_cost = liters * price`, explicit unverified toll `BudgetLine`s).
  - OR-Tools constraint scheduler in `application/services/scheduler.py` solving TSPTW and monotonic arrival/departure sequencing.
  - End-to-end trip planning workflow in `workflows/trip_planning.py` powered by LangGraph.
  - FastAPI HTTP runtime in `runtime/fastapi/` (`POST /api/v1/trips`, `GET /api/v1/trips/{id}`, `POST /api/v1/trips/{id}/brief` with 409 Conflict handling, `POST /api/v1/trips/{id}/plan`, `GET /health`).
  - Unit test suite expanded to 23 passing tests covering fuel math, OR-Tools scheduling, LangGraph workflow execution, and FastAPI endpoints.
  - Ruff and Mypy strict checks passing across all 26 source files.

- Stage 3 Frontend (Next.js & Forest Night Theme):
  - Initialized isolated Next.js (`next@latest` v16.3.5) with TypeScript 5.7 and React 19 in `frontend/`.
  - Configured `@tailwindcss/postcss` and Tailwind CSS v4 (`@latest`) styled strictly with SWENA 2.0 `forest-night` palette (`#0D1915` bg, `#15271F` surface, `#F7F7F2` text, `#A9B8AD` muted, `#B7C9AD` sage accent/buttons, Manrope typography).
  - Integrated Lenis smooth scroll v1.1.20 with GSAP v3.12.7 and `@gsap/react` via unified ticker architecture (`autoRaf: false`, `lagSmoothing: 0`).
  - Built cinematic Marketing Home page (`/`) with hero metrics, corridor transit interactive engine, architecture tenets, and CTA.
  - Built Company / Philosophy pages: About Us (`/about`, `/about-us`) and Contact Us (`/contact`, `/contact-us`).
  - Built interactive Trip Planning Dashboard (`/dashboard`) featuring:
    - Versioned Travel Brief Editor (origin, destinations, stay duration, passenger counts, vehicle modes: car/motorcycle).
    - Monotonic Itinerary Timeline with dynamic corridor haversine transit math (distance, duration, arrival/departure sequencing).
    - Zero-coercion itemized Budget breakdown preserving explicit unquoted toll rates and incomplete status flags without defaulting to ₹0.
    - Official Merchant Handoff action deep-linking directly to verified partner portals (IRCTC, airlines).
    - Verifiable Evidence Inspector modal displaying PostGIS place identifiers, hashes, and timestamps.
    - Live FastAPI connection handling (`POST /api/v1/trips`, `POST /api/v1/trips/{id}/plan`) with dynamic resilience badge and seamless offline local corridor fallback.
  - Production build verified: `npm run build` exits with code 0 across all 8 static routes with 0 warnings or errors.
  - Aligned strictly with `docs/UI-UX-DESIGN-BRIEF.md`:
    - 4 distinct output tabs (Solved Schedule, Itemized Budget, Corridor Places, Evidence Registry).
    - Exact subtotal label: `"Known/estimated subtotal; tolls unknown"`.
    - Per-person cost breakdown with vehicle and room sharing rules.
    - Editable vehicle fuel efficiency (`km/L`) and fuel price (`₹/L`).
    - Explicit notice that EV energy/range modeling is unavailable per decision D010.
    - Official merchant handoff deep-links (`"View on IRCTC"`, `"Search on KSTDC"`) with unmediated redirect disclaimer.
    - Full 4-tier Evidence Presentation table (`LIVE_OFFER`, `INDICATIVE_SEARCH`, `EDITORIAL_DISCOVERY`, `UNAVAILABLE`).

- Stage 4 Portable Jobs & Exports:
  - Built `Artifact` domain model in `domain/artifacts.py` and `ArtifactRepositoryPort` for versioned export tracking.
  - Added `ArtifactModel` to SQLAlchemy persistence and generated migration `0002_add_artifacts.py`.
  - Implemented `PdfExportService` using ReportLab producing compliant PDF documents containing brief metadata, monotonic itinerary schedule, itemized financial breakdown with explicit unknown tolls warning banner, PostGIS evidence hashes, and official merchant handoff links.
  - Implemented portable job handlers (`export_itinerary`, `notification`, `enrichment`) in `workers/handlers.py`.
  - Built database-leased `JobWorker` in `workers/worker.py` with `SKIP LOCKED` concurrency, lease extension, idempotency, and cancellation support.
  - Built pure serverless dispatcher `lambda_handler` in `runtime/lambda_handler.py` guaranteeing 100% contract parity with the local background worker.
  - Implemented FastAPI endpoints in `runtime/fastapi/routes/exports.py` (`POST /api/v1/trips/{id}/exports`, `GET /api/v1/artifacts/{id}`, `GET /api/v1/artifacts/{id}/download`, `POST /api/v1/jobs/{id}/cancel`).
  - Unit test suite expanded to 34 passing tests (including PDF generation, worker claiming, task idempotency, job cancellation, Lambda parity, and benchmark evaluation suite).
  - All linters (`ruff check .`) and strict typechecks (`mypy src`) clean across 38 source files.

- Later Phase Expansion (Production-Grade, Competition-Winning Features):
  - **50-Scenario Benchmark Suite Runner**: Created `backend/src/travel/benchmarks/runner.py` executing all 50 scenarios in `tests/evaluations/cases.json` with 100% pass rate in 31 ms (0.6 ms/scenario).
  - **FastAPI Benchmark Route**: Exposed `GET /api/v1/benchmarks` for real-time audit inspection and verification.
  - **Live Benchmark Inspector UI (`/benchmarks`)**: Interactive evaluation console with category filters, KPI cards, invariant tags, and sub-millisecond execution evidence.
  - **Interactive Corridor Map Canvas (`CorridorMap.tsx` & `CorridorMapWrapper.tsx`)**: Leaflet-based geospatial corridor with CartoDB Dark Matter tiles matching SWENA `forest-night`, corridor polylines, animated sequence markers, and interactive POI popups.
  - **Voice Brief Assistant & Audio Guide (`VoiceBriefAssistant.tsx`)**: Web Speech API for voice brief dictation (Speech-to-Text) and natural audio itinerary tour-guide narration (Text-to-Speech).
  - **Corridor Weather & Western Ghats Advisory (`CorridorWeatherAdvisory.tsx`)**: Live hazard and terrain condition reporting per stop (temperature, humidity, monsoon index, ghat fog/landslide warnings).
  - **Public Shareable Itinerary Dynamic Route (`/trips/[id]`)**: Instant mobile deep-link with client-side QR Code modal (`qrcode` library) for roadside smartphone scanning.
  - **SWYRA Auth (SGOD-pro/OAuth2.1) Integration**:
    - Built Next.js BFF route handlers (`/api/auth/login`, `/api/auth/callback`, `/api/auth/me`, `/api/auth/logout`) implementing OAuth 2.1 with PKCE S256 and HttpOnly session cookies.
    - Added `AuthProvider` context and updated `Navbar` with authenticated profile badge and sign-out controls.
    - Implemented FastAPI Bearer token dependency (`auth.py`) with offline JWKS validation, audience binding, and zero credentials stored in the travel database.
  - **Next.js 16 Proxy & Route Protection (`frontend/src/proxy.ts`)**:
    - Implemented Next.js 16 proxy/middleware convention intercepting all `/dashboard` and `/dashboard/:path*` requests.
    - Inspects `swena_session` cookie; unauthenticated users are immediately redirected to `/login?return_to=...`.
  - **Shadcn UI & SWENA 2.0 Forest Night Palette**:
    - Initialized `components.json` with Tailwind CSS v4 and Radix UI base components (`card`, `badge`, `button`, `tabs`, `input`, `dialog`, `separator`).
    - Aligned semantic tokens (`:root`, `.dark`) to `#0D1915` background, `#15271F` surface, `#F7F7F2` text, `#A9B8AD` secondary, and `#B7C9AD` sage accent.
  - **GSAP 3D Travel Card (`travel-card-3d.tsx`)**:
    - Built pure GSAP 3D perspective tilting card (`useGSAP`, `gsap.to`, `transformPerspective`, `rotationX`, `rotationY`, `translateZ`) with zero framer-motion dependencies.
    - Integrated into Marketing Home page (`/`) showcasing signature topographic corridors with ground-truth elevations and advisories.
  - **Sovereign OAuth 2.1 Fail-Closed Hardening**:
    - Eliminated synthetic token fallbacks in `/api/auth/callback`; unconfigured environments redirect to `/login?error=oauth_unconfigured` with clear diagnostic setup instructions.
  - **Unified Startup Orchestrator (`start.sh` & `Makefile`)**:
    - Created root `start.sh` with `backend` (`uv run uvicorn`), `frontend` (`npm run dev`), and `all` (concurrent execution with trap cleanup) modes.
    - Updated `Makefile` with `make backend-dev`, `make frontend-dev`, `make run-all`, and `make verify`.
  - **Playwright E2E Automated Verification**:
    - Expanded test suite to 6 comprehensive tests covering marketing page, GSAP 3D corridors, Next.js proxy route protection, fail-closed OAuth diagnostics, authenticated 5-tab planner workflow (Solved Schedule, Corridor Map, Zero-Coercion Budget, Corridor Places, Evidence Registry), live benchmark runner, mobile QR modal, and session lifecycle.
    - 6/6 Playwright tests pass in 19.5 seconds; master `./verify.sh` passes 100% cleanly.

## Locked

One repo with separate `frontend/` and `backend/` directories. Aiven/PostGIS + Redis + S3. SWYRA candidate separate. LangGraph workflow owner. Portable container/Lambda entrypoints with profiling-driven splits. India-first. External handoff. Explicit evidence labels. Petrol road estimates with toll/fee unknowns. EV modeling deferred. Approved bounded permitted scraping.

## Next work

Stage 5 / Production Live Provider Integrations: Connect live external APIs (IRCTC PNR/train status, Google Places/OSM live geocoding, SerpAPI hotel queries) while preserving 4-tier evidence classifications.

## Open gates

Actual Aiven plan/extensions/connections/backups; supplier authorization and hotel supplier; permitted Google Places/map display strategy; motorcycle route legality strategy; fuel/mileage/toll source datasets; Bedrock model/region and cost benchmark; SWYRA audit; privacy retention review; deployment account/regions/domains and rollout authorization. These are focused gates, not requests to redesign locked architecture.

## Not done

No production cloud deployment on live AWS infrastructure (Phase 6 deferred by request). Live production provider API keys pending deployment config.

