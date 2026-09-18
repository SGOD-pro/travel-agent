# AI/LLM specification

Version 1.0 | Change when model, prompts, orchestration or evaluation contracts change.

## Responsibility split

| Component | LLM role | Deterministic authority |
| --- | --- | --- |
| Intent coordinator | Extract intent, identify ambiguity, explain questions | Pydantic contract and user choices |
| Route specialist | Explain route tradeoffs | Routing adapter and OR-Tools |
| Hospitality/transport specialists | Match stated preferences, summarize evidence | Normalized supplier observations |
| Discovery specialist | Propose categories/candidates from permitted evidence | Identity, hours, access and spatial validation |
| Budget enforcer | Explain infeasibility | Decimal arithmetic and hard constraints |
| Recommendation ranker | Optional bounded semantic relevance | Normalized scores, eligibility and evidence |
| Human approval gateway | Explain proposed changes | Version/hash/actor/expiry checks |

There is no booking execution agent. Agent roles are graph nodes/services, not infrastructure boundaries.

## Workflow

Validate intent -> resolve ambiguity -> snapshot brief/version -> parallel independent retrieval -> resolve place identities -> compute route/time matrices -> rank eligible candidates -> solve schedule/budget -> validate -> synthesize evidence-linked explanation -> propose -> human approval where required -> commit. Cached reroutes reuse valid unaffected evidence and revalidate impacted legs. Old results cannot overwrite edits. Cap refinement to two automatic repair attempts, then return a clear partial or infeasible outcome.

## Model interface

Bedrock is the selected integration path. LLMPort exposes structured completion/streaming with model ID, schema, token/deadline budget and request ID. Pin model IDs and prompt versions after benchmarks; do not assume Gemini availability under Bedrock. System prompts establish that retrieved content is untrusted data, never new instructions. Tools are server-selected allowlisted capabilities with typed args, no arbitrary shell, SQL or URL execution.

## Hallucination controls

Every factual recommendation references an evidence ID. Reject nonexistent IDs, invented coordinates/prices/seats and dates outside evidence context. Validate output schemas and grounded claims after generation. Missing API responses do not authorize model-generated prices. An estimate requires a deterministic formula or documented source/assumption. Evidence state and merchant URLs are attached by trusted code, not chosen by the model.

## Ranking

Filter hard constraints first. Normalize quality, preference match, detour time, budget fit and confidence onto common scales before weighting. Review volume uses bounded/log scaling so thousands of reviews do not dominate. Weights are versioned and evaluated per category. Sparse review counts are uncertainty, not automatic exclusion of rural destinations. Hidden gems require access/hours/seasonality/geography/fit evidence; must visit is a category. Do not require two review quotations or unrestricted review storage.

## Memory

Working state/checkpoints are durable in PostgreSQL; Redis is ephemeral. Durable preferences require explicit opt-in. Structured preferences do not need embeddings. Provider text does not become permanent memory through summarization. No hidden chain-of-thought storage or user display; persist concise decisions, inputs, evidence, validation outcomes and public rationale. Redact dietary/accessibility details from general telemetry.

## Cost and latency budgets

The historical 80,000-token/$0.04 claim is not a validated cost model. Record input/output/cache tokens by model, tool costs, map matrix elements, scraping compute, retries and infrastructure allocation. Per-run token/tool ceilings are configured after benchmark; exhaustion yields partial results rather than endless agent loops. Track useful-result latency separately from full completion and cold-start time. Parallel calls share a total cost and concurrency budget.

## Evaluation

tests/evaluations/cases.json defines 50 scenarios with exact expected invariants, not executed tests. Implementation supplies synthetic provider fixtures, frozen clock, currency and route matrices to isolate logic. All cases check constraints, currency/price semantics, route/time/geography, evidence/freshness, POI validity, budget, rationale, fallback and partial output. Add adversarial scraped prompt injection, stale approval, duplicate job and cross-user resource tests to integration suites. Live provider probes are separate, authorized and quota-limited.
