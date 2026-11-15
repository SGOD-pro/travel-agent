# Backend audit

You have a substantial durable execution foundation, but this inspection does not establish production readiness. This is a source review of the trip planner, execution cancellation and fencing, browser and evidence workers, and model wiring. It is not a load test or a full backend security review.

## Findings

1. High, Lambda deadline handling is incomplete. `backend/src/functions/browser/consumer.py:85` and `backend/src/functions/evidence_collector/consumer.py:85` check remaining time only while building the batch. They then await all records without an overall time limit. A slow operation can run until Lambda kills the process, losing the partial batch response. You could apply a remaining invocation deadline to each operation, reserve cleanup time, and return unfinished message identifiers for retry.

2. High, crash recovery does not recover evidence budget reservations. `backend/src/functions/evidence_collector/service.py:154` commits aggregate reservation counters before provider calls. Reconciliation happens in a finally block, but process termination cannot run that block. No reservation recovery was found in the execution recovery modules. You could associate reservations with task attempts and reconcile abandoned attempts conservatively, preserving unknown provider spend rather than assuming a failed call was free.

3. Medium, cancellation can change an already superseded execution. `backend/src/functions/orchestrator/execution/cancellation.py:43` excludes SUPERSEDED from its terminal guard, although the runner treats it as terminal. A cancellation request can replace SUPERSEDED with CANCELLED and emit another lifecycle event. You could align terminal handling and add a regression case for this exact transition.

4. Medium, a model timeout stops awaiting, not necessarily the provider operation. `backend/src/functions/orchestrator/llm/adapters/bedrock.py:178` wraps boto3 work in asyncio.to_thread and wait_for. Socket limits, SDK retries, and inference profile fallback can outlive the coroutine deadline. You could bound outstanding provider work and align the SDK retry budget with the task deadline. This is a resource and billing risk, not evidence of stale writes bypassing database fencing.

5. Medium, evidence collection combines sequential latency with transaction lifetime concerns. `backend/src/functions/evidence_collector/service.py:558` runs searches sequentially, then up to three page extractions sequentially. In the extraction loop, the execution SELECT opens a transaction and the usual search plus extraction path does not commit it before network work, because should_reserve_budget is false. This contradicts the documented transaction boundary rule. You could close database transactions before network waits, then use bounded parallel calls with separate database sessions for reservations and persistence. You should not share an AsyncSession across concurrent operations.

6. Medium, model roles are not separated. `backend/src/config/config.py:227` defines one BEDROCK_MODEL_ID, defaulting to Nova Lite. Discovery and ranking construct clients from that same setting. There is no configured main Haiku versus small extraction model split. Query generation uses an injected client when present and otherwise deterministic templates. LAYA remains a separate routing decision adapter.

7. Maintainability, `backend/src/functions/orchestrator/graphs/trip_planner/graph.py` has 5,078 lines. It combines graph construction, evidence validation, route result consumption, food and lodging research, advisory policy, budgets, and publication. You could move cohesive responsibilities into modules while preserving graph node interfaces, replay semantics, and transaction boundaries. File length alone does not prove a runtime performance problem.

## Model recommendation

You could use your chosen Haiku model for planning and synthesis, Nova Lite for bounded structured extraction and classification, and LAYA for eligible tool selection. Coordinates proposed by any model still need source corroboration. Weather, money, scheduling, ownership, evidence eligibility, and cancellation should remain deterministic. The existing provider port and fixture adapters give you a useful starting point. Actual model availability, quality, latency, and cost need deployment specific evaluation before selecting exact model identifiers.

The Bedrock converse request currently has no explicit inferenceConfig output token limit. You could add per operation maxTokens limits and record model ID, input tokens, output tokens, duration, retries, and fallback reason without logging private prompts.

## Verification

Ruff passed for backend/src. Browser consumer, query planner, and trip planner model wiring tests passed, 26 tests total. A combined run including the Bedrock adapter stopped progressing after five reported passes and was interrupted. Its cause was not diagnosed, so Bedrock adapter validation remains incomplete. No connected cloud tests, database integration tests, or load tests were completed as part of this review. No application code or existing context files were changed.

## Context proposal

You could add this sentence under Conventions in backend/AGENTS.md:

Worker deadline checks must cover running operations and leave time for cleanup and partial batch responses. Aggregate evidence budget reservations currently need recovery after process termination, and model roles currently share BEDROCK_MODEL_ID.

This is a proposal only. Existing context is preserved under the audit skill rules.
