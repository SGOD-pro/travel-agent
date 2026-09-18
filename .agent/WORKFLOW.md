# Agent development workflow

Version 1.0 | Update when the workflow improves.

1. Read current memory and the owning specs; identify the smallest authorized stage/task.
2. Inspect workspace/git state and existing instructions; preserve unrelated work.
3. Define observable acceptance before coding. Use deterministic fixtures for provider-dependent work.
4. Implement domains/ports first, adapters and thin entrypoints second. Maintain generated contracts from one source.
5. Test meaningful invariants: money, hard constraints, version conflicts, evidence, ownership, retries and parity. Do not substitute mock success for live access verification.
6. For concurrent scraping, measure provider admission, queue length, memory and timeout behavior. Add a thread only for blocking I/O, not as a default for every function.
7. Measure package/init/cold/warm cost before proposing runtime splits. Record changes in DECISIONS with rollback path.
8. Run the relevant test subset and contract/doc consistency checks. Broaden only for concrete risk or release gates.
9. Update affected documents, append decisions and refresh MEMORY with completed/remaining work and verified results.
10. Report changed behavior, evidence and limitations. Ask only for genuinely missing authority or decisions that cannot be safely inferred.

## Documentation ownership

PRD: product requirements. TRD: technical requirements. ARCHITECTURE: structure. UI-UX-DESIGN-BRIEF: journeys/visual behavior. DATABASE-SCHEMA: data model. API-SPEC: contracts. AI-LLM-SPEC: model/graph behavior. SECURITY: threats/controls. DEPLOYMENT: runtime/release operations. PHASES: implementation sequence. DECISIONS: append material choices. MEMORY: current state. RULES/BOUNDARIES: stable constraints. WORKFLOW: agent process improvements.

## Release progression

Fixture validation -> local integration -> staging provider probes -> auth/security/deletion gates -> measured load/cost/SLO -> authorized rollout. Never label a deployment production-ready solely from the original PDF, a README, or a successful local test.
