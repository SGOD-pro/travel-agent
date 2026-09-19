# SWENA Platform — Gemini CLI Agent Entrypoint & Protocol

**Version:** 1.0  
**Repository:** `SGOD-pro/travel-agent`  
**Stack:** Next.js 16 (Turbopack, Tailwind v4) + FastAPI (Python 3.12, Uvicorn) + PostgreSQL/PostGIS (Aiven) + Upstash Redis  
**Governing Documents:** [`.agent/RULES.md`](.agent/RULES.md) | [`.agent/BOUNDARIES.md`](.agent/BOUNDARIES.md) | [`docs/IMPLEMENTATION-AUDIT.md`](docs/IMPLEMENTATION-AUDIT.md) | [`docs/PRODUCTION-COMPLETION-PLAN.md`](docs/PRODUCTION-COMPLETION-PLAN.md)

---

## 1. Prime Directives for Agent Execution

1. **Evidence Before Assertions:** A passing unit test, an existing visual component, and an operational user journey are three different facts. Never declare a feature "complete" without concrete runtime test evidence.
2. **Zero Hallucination:** Zero fabricated prices, availability, routes, source quotes, IDs, or booking confirmations. Unknown costs remain explicitly unknown; never coerce missing fees or tolls to ₹0.
3. **Canonical Authority:** PostgreSQL is the single source of truth for trips, versions, and jobs. The backend server owns mathematical and scheduling authority; frontend must consume server plans directly rather than simulating them via client-side math.
4. **Non-Custodial Handoff:** SWENA is an intelligence, comparison, and planning platform. No internal checkout, payment capture, or booking creation. Handoff to official suppliers (IRCTC, airlines, hotels) is direct and unmediated.
5. **Fail-Closed Security:** Derive user ownership exclusively from cryptographically verified tokens. Never trust client-supplied `owner_id` fields. Forged, expired, or unsigned tokens strictly return 401/404.

---

## 2. Standard Session Protocol

When starting or continuing any work in this repository, follow this sequence:

```
[1. Read GEMINI.md, .agent/RULES.md, and docs/IMPLEMENTATION-AUDIT.md]
  │
  ▼
[2. Identify Current Task from docs/PRODUCTION-COMPLETION-PLAN.md]
  │
  ▼
[3. Inspect the Actual Execution Path Before Changing Code]
  │
  ▼
[4. Implement Complete Vertical Slice (UI → API → Domain → Persistence)]
  │
  ▼
[5. Execute Task-Relevant Positive and Negative Tests]
  │
  ▼
[6. Update Task Status, docs/IMPLEMENTATION-AUDIT.md, and .agent/MEMORY.md]
```

### Protocol Details:
* **Task Selection:** Always pick the first incomplete task whose dependencies are satisfied in `docs/PRODUCTION-COMPLETION-PLAN.md` (e.g. `C1-T01`).
* **Surgical Edits:** Touch only the files required to satisfy the task. Preserve user edits and existing working code.
* **External Blockers:** If an external supplier API key is unconfigured, build the typed port, adapter, fixture validation, and `UNAVAILABLE` error state. Mark the live integration gate as `BLOCKED_EXTERNAL`. Never invent credentials or fake responses.
* **No Premature Endings:** Do not ask "continue?" if authorized work remains within the current task. Continue until the task's declared acceptance evidence is verified.
* **Context Preservation / Checkpointing:** If token context is running low, write a structured checkpoint in `.agent/MEMORY.md`: active task ID, changed files, current test outputs, next command, and any unresolved blockers.

---

## 3. Essential Verification Commands

```bash
# 1. Full Master Verification Pipeline
./verify.sh

# 2. Backend Linting & Strict Typecheck
cd backend && uv run ruff check . && uv run mypy src

# 3. Backend Unit Test Suite (100% Deterministic)
cd backend && uv run pytest tests/unit/ -v

# 4. Frontend Turbopack Build
cd frontend && npm run build

# 5. Playwright End-to-End Suite
cd frontend && npx playwright test

# 6. Run Both Servers Locally
./start.sh all
```

---

## 4. Current Workstation Status & Active Task

* **Current Baseline:** Commit `8b7592f` audit completed.
* **Active Milestone:** **Milestone C1 — Identity, Ownership & Data Integrity**
* **Next Ready Task:** **`C1-T01`** (Cryptographically Validated Identity & Fail-Closed Session Boundary in `AuthProvider.tsx` and `api/auth/me/route.ts`).
