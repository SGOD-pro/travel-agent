# Project Agent Guidelines & Instructions

This repository defines strict development and architectural rules. Whenever working on, building, or modifying this project, all agents MUST read, adhere to, and enforce the guidelines located in the [`.agent/`](.agent/) directory and the project specifications in [`docs/`](docs/).

---

## 1. Core Rule & Context Files in `.agent/`

Before any work or code implementation:
1. **[.agent/RULES.md](.agent/RULES.md)**:
   - **No Hallucinations**: Zero fabricated prices, availability, routes, source quotes, IDs, or booking confirmations.
   - **Precision**: Money must always use `Decimal`/currency types; unknown cost is never zero.
   - **Persistence**: PostgreSQL/PostGIS is authoritative; Redis is ephemeral (loss must not lose trips).
   - **Boundaries**: Domain logic remains provider/runtime-independent. Never commit secrets, raw private GPS, or hidden model reasoning. No bypassing scraping protections or rate limits.
2. **[.agent/BOUNDARIES.md](.agent/BOUNDARIES.md)**:
   - **Domain Ownership**: Trips owns brief/versions/deltas; Itinerary owns schedules/stops/legs; Transport & Hotels own provider-normalized options; Budget owns arithmetic/assumptions; Places owns spatial records; Recommendations owns ranking; Users owns profiles/consents; SWYRA owns identity.
   - **Architecture**: Clean architecture with ports and adapters, unit of work. LangGraph orchestrates workflow; controllers validate transport only. No direct database or secret access from frontend.
   - **Product Boundaries**: India-first planning, comparison, discovery, and external handoff. No internal checkout, payment, or price locks. Petrol cars & motorcycles + walking/bicycles; EV energy/range modeling deferred.
3. **[.agent/DECISIONS.md](.agent/DECISIONS.md)**:
   - Respect locked architectural decisions (D001 through D017). Do not reopen locked decisions without explicit user instruction.
   - Single repository, feature-modular Next.js frontend, shared modular-monolith FastAPI/Python backend.
4. **[.agent/AGENT.MD](.agent/AGENT.MD)**:
   - **Think Before Coding**: Clarify assumptions and surface tradeoffs before implementing.
   - **Simplicity First**: Write minimal code solving the immediate problem; no speculative abstractions.
   - **Surgical Changes**: Touch only what is required. Preserve existing style and unrelated code.
   - **Goal-Driven Execution**: Formulate verifiable criteria and test loops before writing code.
5. **[.agent/WORKFLOW.md](.agent/WORKFLOW.md)**:
   - Scope to the smallest authorized task.
   - Implement domains/ports first, adapters second.
   - Test invariants (money, constraints, versioning, retries, parity).
   - Refresh [.agent/MEMORY.md](.agent/MEMORY.md) and record material choices in [.agent/DECISIONS.md](.agent/DECISIONS.md).
6. **[.agent/MEMORY.md](.agent/MEMORY.md)**:
   - Check current implementation status and open gates before starting tasks.

---

## 2. Project Specifications in `docs/`

Follow the official project specifications in sequence:
- **[docs/PRD.md](docs/PRD.md)**: Product requirements and scope.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** & **[docs/TRD.md](docs/TRD.md)**: System design and technical requirements.
- **[docs/PHASES.md](docs/PHASES.md)**: Staged implementation roadmap (Stage 0 through Stage 6).
- **[docs/DATABASE-SCHEMA.md](docs/DATABASE-SCHEMA.md)**, **[docs/API-SPEC.md](docs/API-SPEC.md)**, **[docs/AI-LLM-SPEC.md](docs/AI-LLM-SPEC.md)**: Data, API, and agent workflow specifications.
- **[docs/PROVIDER-REGISTRY.json](docs/PROVIDER-REGISTRY.json)** & **[tests/evaluations/cases.json](tests/evaluations/cases.json)**: Provider capabilities and 50 benchmark evaluation scenarios.
