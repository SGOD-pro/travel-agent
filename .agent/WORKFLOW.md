# Agent Development Workflow

**Version:** 2.0  
**Update Policy:** Update when the development or verification workflow improves.

1. **Task Identification:** Read current memory (`.agent/MEMORY.md`), `GEMINI.md`, and the owning specifications. Identify the smallest incomplete task in `docs/PRODUCTION-COMPLETION-PLAN.md` whose dependencies are satisfied.
2. **Context Inspection:** Inspect the active code path before writing code. Preserve unrelated work and existing working code.
3. **Deterministic First:** Define observable acceptance criteria before coding. Use deterministic fixtures for provider-dependent work.
4. **Clean Architecture Order:** Implement domains/ports first, persistence and adapters second, and HTTP/runtime entrypoints last.
5. **Invariant Testing:** Test meaningful invariants: Decimal precision, hard constraint preservation, optimistic version conflicts, ownership isolation, and lease recovery. Counterexamples/negative controls are mandatory.
6. **No Mock Facades:** Do not substitute mock successes or synthetic client loops for missing server logic. Mark incomplete integrations honestly.
7. **Performance Measurement:** Profile package size, initialization, and memory before proposing serverless Lambda splits. Verified AWS limits apply.
8. **Automated Verification:** Execute `./verify.sh` or the task-relevant test commands before declaring work complete.
9. **Documentation Sync:** Update affected documents, append material architectural choices to `DECISIONS.md`, and update `MEMORY.md` with verifiable evidence.
10. **Report with Evidence:** Report changed behavior, command outputs, and limitations honestly. Avoid asking "continue?" when authorized work remains within the active task.

---

## Documentation Directory Ownership

* **PRD:** Product requirements and launch acceptance (`docs/PRD.md`).
* **TRD:** Technical requirements, performance limits, and state transitions (`docs/TRD.md`).
* **ARCHITECTURE:** Hexagonal design, BFF paths, and runtime profiles (`docs/ARCHITECTURE.md`).
* **IMPLEMENTATION-AUDIT:** Implementation reality, defect tracking, and traceability matrix (`docs/IMPLEMENTATION-AUDIT.md`).
* **END-TO-END-JOURNEYS:** Screen-to-server interaction contracts for J01–J15 (`docs/END-TO-END-JOURNEYS.md`).
* **PRODUCTION-COMPLETION-PLAN:** Dependency-ordered implementation tasks C0–C8 (`docs/PRODUCTION-COMPLETION-PLAN.md`).
* **VISUALIZATION-AND-MOTION-SPEC:** Motion design, GSAP rules, and accessibility matrix (`docs/VISUALIZATION-AND-MOTION-SPEC.md`).
* **TEST-AND-RELEASE-PLAN:** Test layers, 50-case benchmark invariants, and release gates (`docs/TEST-AND-RELEASE-PLAN.md`).
* **OPERATIONS-RUNBOOK:** Incident response, support triage, and recovery protocols (`docs/OPERATIONS-RUNBOOK.md`).
* **DATABASE-SCHEMA:** Relational entities, migrations, and retention policies (`docs/DATABASE-SCHEMA.md`).
* **API-SPEC:** Wire contracts, casing strategy, and OpenAPI endpoint definitions (`docs/API-SPEC.md`).
* **AI-LLM-SPEC:** LangGraph workflow, prompt injection defense, and inference port (`docs/AI-LLM-SPEC.md`).
* **SECURITY:** Threat model, token validation, SSRF controls, and privacy (`docs/SECURITY.md`).
* **DEPLOYMENT:** Packaging, ECS orchestration, and rollback policies (`docs/DEPLOYMENT.md`).
* **PHASES:** Historical engineering phases and canonical completion roadmap (`docs/PHASES.md`).
* **SOURCES:** Verified references, Web Speech API standards, and superseded assumptions (`docs/SOURCES.md`).
* **PROVIDER-REGISTRY:** Provider capabilities and verified access status (`docs/PROVIDER-REGISTRY.json`).
* **GEMINI.md:** Entrypoint protocol and instructions for automated CLI agents (`GEMINI.md`).
