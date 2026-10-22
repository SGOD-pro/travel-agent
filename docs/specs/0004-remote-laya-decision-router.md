# 0004. Remote LAYA Decision Router Integration

**Date**: 2026-10-17  
**Status**: Accepted

## Summary

This specification defines the integration of the remote LAYA decision router for SWENA. LAYA is a non autoregressive System 1 decision engine developed in repository NandhaKishorM/laya, pinned to release v0.4.9 (commit 4bb49a70c67746d74ed2eefa64bbad1402e2423d). The remote engine is hosted on Kaggle and exposed over HTTPS through a Cloudflare tunnel, speaking the TypeSafe Jev compatible /v1/systemone wire protocol.

The orchestrator integrates through an asynchronous HTTP adapter that cleanly separates SWENA internal decision abstractions from upstream wire payloads. Before issuing any remote query, deterministic policies filter candidate options to policy derived eligible choices and enforce remaining execution budgets. The client passes sanitized state and typed choice questions over /v1/systemone, authenticates with Bearer tokens and optional Cloudflare Access service headers, and enforces a cumulative total retry deadline. All model responses pass through deterministic policy validation before task dispatch. When the endpoint is unconfigured, unreachable, slow, malformed, or abstains, the system falls back immediately to safe deterministic routing so user executions never stall.

## Context

The SWENA travel intelligence platform orchestrates multi step workflows across evidence search, dynamic browser rendering, canonical place matching, and claim verification. In specifications 0001, 0002, and 0003, the foundational execution engine, static evidence collector, and dynamic browser escalation worker were established and verified on live PostgreSQL and headless Chromium.

While generative large language models excel at free text synthesis and broad planning proposals, allowing generative models to make execution routing decisions introduces token expense, latency, and unpredictable drift. Architectural principle AD-03 mandates that model outputs cannot become deterministic authority, and that an explicit decision router should propose bounded choices across finite capabilities.

Earlier conceptual notes explored packaging LAYA locally via ONNX Runtime inside AWS Lambda. However, actual runtime constraints established in specification 0003 demonstrated that local machine learning runtime dependencies consume significant package budget and risk cold start penalties. Serving LAYA remotely on dedicated Kaggle infrastructure exposed through a Cloudflare tunnel preserves the 250 MB Lambda ceiling and eliminates heavyweight machine learning dependencies from the orchestrator package.

### Upstream Reference and Pinned Revision

- **Repository**: `https://github.com/NandhaKishorM/laya`
- **Pinned Upstream Revision**: `v0.4.9` (commit `4bb49a70c67746d74ed2eefa64bbad1402e2423d`, PR #1264)
- **Upstream Server**: `laya.serve` FastAPI server exposing `POST /v1/systemone`, `POST /v1/systemone/batch`, and `GET /health`.
- **Upstream Client Reference**: `sdk/python/src/laya_client` standard library client implementing `predict(state, questions, ...)` and `health()`.

### Unconfirmed Deployed Behavior

The remote Kaggle deployment and Cloudflare tunnel introduce operational unknowns that must be labeled explicitly rather than assumed:
1. **Endpoint Reachability and Session Lifecycle**: Kaggle notebooks have ephemeral lifespans and may stop or restart, causing transient tunnel downtime.
2. **Tunnel Authentication Configuration**: The remote endpoint may be secured via LAYA application Bearer token (`LAYA_API_KEY`), Cloudflare Access service tokens (`CF-Access-Client-Id` and `CF-Access-Client-Secret`), both, or neither.
3. **Model Selection on Remote Host**: Whether the deployed instance preloads the fine tuned `typed-decisions` checkpoint or relies on `multilingual` or `english` auto routing.
4. **Live Latency and Concurrency**: Actual network round trip duration over Cloudflare tunnel plus inference time on Kaggle hardware is unmeasured; no latency promises are assumed.
5. **Strict vs Extended Jev Responses**: Whether the deployed server sets `LAYA_JEV_STRICT=1` (omitting root `routing` and answer `abstention` fields) or returns full LAYA extensions.

## Requirements

**User stories**:
- As a travel orchestrator, I want bounded categorical routing decisions proposed by LAYA so that evidence collection selects the most appropriate extraction tool efficiently without generative hallucinations.
- As a platform operator, I want remote model queries protected by cumulative deadlines, bounded retries, and deterministic fallbacks so that remote hiccups never stall trip planning workflows.
- As a security officer, I want remote LAYA calls sanitized of authentication tokens, cookies, and personal traveler data so that external model queries adhere strictly to data privacy standards.

**Acceptance criteria**:
- **AC-1**: SWENA defines internal typed domain contracts (`LayaDecisionRequest`, `DecisionEnvelope`, `DecisionAction`, `DecisionStatus`) in `contracts/decision.py` that remain decoupled from upstream wire protocols.
- **AC-2**: The remote client adapter implements the upstream Jev compatible wire protocol (`POST /v1/systemone`), transforming SWENA internal requests into `state` and typed `choice` questions, parsing returned `answers` dictionaries with graceful handling of strict mode responses (`LAYA_JEV_STRICT=1`) where optional fields are missing, keeping unavailable entropy confidence null, and treating unavailable truncation metadata as unknown with defensive client side request length bounding without asserting a universal 512 token ceiling or claiming full context proof.
- **AC-3**: Action eligibility operates across three explicit workflow phases: search completion (initial discovery where required destination evidence mandates static search, yielding a singleton bypass), browser eligibility (where static search has completed, verifying required destination evidence, while optional enrichment requests establish a valid multi choice state without weakening evidence requirements, returning to browser eligibility or advancing to evaluation with bounded, deduplicated tasks), and evaluation (where completed required evidence satisfies schedule solving prerequisites, yielding a singleton bypass; budget exhaustion or candidate existence never substitutes for required evidence).
- **AC-4**: Remote client enforces configurable endpoints (`LAYA_ENDPOINT_URL`), separate Bearer authentication (`LAYA_API_KEY`), and optional Cloudflare Access service tokens (`CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`).
- **AC-5**: Remote client enforces a cumulative total retry deadline (default 5.0 seconds across all attempts) with individual socket timeout (default 2.5 seconds), executing at most 2 retries on transient network errors or 5xx responses, and failing immediately with zero retries on 4xx client errors or when `LAYA_ENDPOINT_URL` is empty.
- **AC-6**: Deterministic policy engine (`LayaPolicyValidator`) validates all responses before dispatch, using `answer_confidence` consistently for probability gating against the provisional threshold (default 0.700), validating that probability keys match question criteria, that values are finite floats normalizing to 1.0, that `choice` matches the argmax probability, keeping unavailable entropy confidence null, assigning confidence=null and answer_confidence=null to deterministic singleton decisions (identified by status `DETERMINISTIC_SINGLETON`), and rejecting abstentions and tokenizer truncations.
- **AC-7**: Explicit deterministic fallback table maps all failure modes (unconfigured endpoint, timeouts, 5xx/4xx errors, malformed responses, tokenizer truncation, low confidence, abstention, policy rejection) to safe deterministic actions while preserving execution budgets, cancellation barriers, deadlines, and required evidence collection, never equating exhausted budgets or candidate existence with sufficient evidence.
- **AC-8**: Actual execution lease and transaction boundaries keep database access, cancellation checks, and lease fencing strictly in orchestration; outbound HTTP calls occur outside database transactions; decisions and task specifications are committed to PostgreSQL only after revalidating active execution lease ownership, cancellation status, deadline barriers, and current policy state under execution row lock before yielding to `WAITING_TASKS`.
- **AC-9**: Replay safety uses a phase specific canonical digest incorporating execution phase, `context_version`, sorted eligible actions, `policy_version`, question version, and sanitized context digest; stored decisions are revalidated against active context version, policy version, and currently eligible choices before reuse, with unique database constraints resolving concurrent runner conflicts.
- **AC-10**: Acceptance test suite provides offline wire contract unit tests with canned fixtures, connected smoke acceptance against the live Kaggle endpoint (skipping only when disabled or unconfigured, and failing on endpoint, authentication, or schema errors when enabled), and a bounded 25 case labeled routing quality evaluation benchmark distinguishing contract mechanics from model accuracy.

## Options considered

### Option 1: Remote HTTP Jev `/v1/systemone` adapter with dual authentication and deterministic policy gating (Chosen)

Invoke LAYA as an external service hosted on Kaggle and exposed through a Cloudflare tunnel using the TypeSafe Jev compatible `/v1/systemone` protocol. An asynchronous HTTP adapter translates internal requests into `state` and `questions`, supports separate Bearer and Cloudflare Access headers, enforces cumulative retry deadlines, and validates all decisions deterministically before database commit or task dispatch.

**Pros**:
- Zero additional binary or machine learning dependencies bundled into Lambda function packages, preserving the 250 MB ceiling.
- Adheres strictly to upstream wire specification in repository NandhaKishorM/laya v0.4.9.
- Clean decoupling between SWENA domain contracts and external wire payloads.
- Dual authentication supports public tunnels, Bearer protected APIs, and Cloudflare Access service perimeters.
- Strict deterministic fallback ensures workflows always proceed even during full remote outage.

**Cons**:
- Depends on external network availability and Cloudflare tunnel stability.
- Introduces external network round trip latency per decision.

### Option 2: Local ONNX Runtime engine bundled inside orchestrator Lambda

Package exported LAYA ONNX models and tokenizers directly inside the orchestrator Lambda function package, executing inference on CPU using `onnxruntime`.

**Pros**:
- Self contained execution with zero outbound external network requests.
- Low single request latency.

**Cons**:
- `onnxruntime`, tokenizers, and numerical dependencies add 80 to 120 MB uncompressed, threatening the 250 MB Lambda ceiling established in specifications 0001 through 0003.
- Substantially increases orchestrator cold start duration and memory footprint.
- Explicitly rejected by project architectural direction and user instructions.

### Option 3: Direct Bedrock LLM prompting for evidence routing

Prompt hosted Amazon Bedrock LLM models (such as Nova Lite) directly within LangGraph to decide which evidence worker to dispatch.

**Pros**:
- Reuses existing Bedrock integration without establishing a separate model service.

**Cons**:
- High token cost and latency (1500 to 3000 milliseconds) for simple categorical routing decisions.
- Generative text output requires complex JSON extraction and remains prone to formatting drift and hallucination.
- Violates architectural role separation between generative planning and bounded decision routing.

## Decision

**Chosen option**: Option 1: Remote HTTP Jev `/v1/systemone` adapter with dual authentication and deterministic policy gating.

The orchestrator will integrate with remote LAYA over HTTPS using the upstream `/v1/systemone` wire protocol, supporting separate LAYA Bearer and Cloudflare Access authentication, validating all decisions against deterministic policies, and persisting structured audit records in PostgreSQL.

## Rationale

Option 1 provides the cleanest architecture and honors project constraints. Keeping machine learning runtimes off the serverless orchestrator Lambda maintains lean packaging and fast cold starts.

Pairing remote model evaluation with local deterministic policy validation enforces core architectural invariant AD-03: models may propose decisions, but only deterministic software may authorize executions, enforce budgets, and commit database state. If the remote endpoint becomes unavailable, the system defaults smoothly to proven static evidence collection without degrading workflow reliability.

## Feature design

### Domain Contracts versus Upstream Wire Contracts

SWENA maintains strict separation between internal orchestration contracts and external wire schemas:

```
[LangGraph Orchestrator Node]
           |
           | 1. Internal Domain Request: LayaDecisionRequest
           v
[RemoteLayaDecisionAdapter]
           |
           | 2. Wire Request: POST /v1/systemone {"state": ..., "questions": ...}
           v
[Cloudflare Tunnel -> Kaggle LAYA Service]
           |
           | 3. Wire Response: {"model": ..., "answers": ..., "usage": ...}
           v
[RemoteLayaDecisionAdapter]
           |
           | 4. Policy Validation: LayaPolicyValidator
           v
[Persist Decision in laya_decisions & Continue Graph]
```

#### 1. Internal SWENA Domain Contracts (`backend/src/contracts/decision.py`)

```python
from enum import StrEnum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
import uuid


class DecisionAction(StrEnum):
    DISPATCH_EVIDENCE_SEARCH = "DISPATCH_EVIDENCE_SEARCH"
    DISPATCH_BROWSER_ESCALATION = "DISPATCH_BROWSER_ESCALATION"
    PROCEED_DETERMINISTIC_EVALUATION = "PROCEED_DETERMINISTIC_EVALUATION"


class DecisionType(StrEnum):
    EVIDENCE_ROUTING = "EVIDENCE_ROUTING"


class DecisionPhase(StrEnum):
    SEARCH_COMPLETION = "SEARCH_COMPLETION"
    BROWSER_ELIGIBILITY = "BROWSER_ELIGIBILITY"
    EVALUATION = "EVALUATION"


class DecisionStatus(StrEnum):
    VALIDATED = "VALIDATED"
    DETERMINISTIC_SINGLETON = "DETERMINISTIC_SINGLETON"
    REJECTED_BY_POLICY = "REJECTED_BY_POLICY"
    FALLBACK_TIMEOUT = "FALLBACK_TIMEOUT"
    FALLBACK_ERROR = "FALLBACK_ERROR"
    FALLBACK_LOW_CONFIDENCE = "FALLBACK_LOW_CONFIDENCE"
    FALLBACK_ABSTAINED = "FALLBACK_ABSTAINED"
    FALLBACK_TRUNCATED = "FALLBACK_TRUNCATED"
    FALLBACK_MALFORMED = "FALLBACK_MALFORMED"
    FALLBACK_UNCONFIGURED = "FALLBACK_UNCONFIGURED"


class LayaDecisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    decision_type: DecisionType = DecisionType.EVIDENCE_ROUTING
    phase: DecisionPhase = DecisionPhase.BROWSER_ELIGIBILITY
    execution_id: uuid.UUID
    trip_id: uuid.UUID | None = None
    context_version: int = 1
    policy_version: str = "laya-policy-v1"
    question_version: str = "evidence_routing:v1"
    policy_derived_eligible_choices: list[DecisionAction]
    budget_remaining: int
    context: dict[str, Any]
    idempotency_key: str


class DecisionEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: uuid.UUID
    decision_type: DecisionType
    phase: DecisionPhase
    execution_id: uuid.UUID
    trip_id: uuid.UUID | None
    idempotency_key: str
    allowed_choice: DecisionAction
    status: DecisionStatus
    confidence: float | None = None  # Model distribution entropy confidence; null when unavailable or for deterministic singletons
    answer_confidence: float | None = None  # Probability assigned to top choice probabilities[choice]
    model_version: str | None = None
    policy_version: str
    rationale: str | None = None
    latency_ms: int
    request_digest: str
    created_at: str
```

#### 2. Upstream Wire Contract (`POST /v1/systemone`)

Conforming to `NandhaKishorM/laya` release `v0.4.9`:

The wire contract below demonstrates a valid multi choice routing state during Phase 2 (the search completion and browser eligibility phase). Static evidence search has already completed and verified core destination coordinates, meeting minimum required evidence prerequisites, while an interactive lodging source emitted a `NEEDS_BROWSER` request:

**Request Wire Payload (Phase 2 Post Search State)**:
```json
{
  "state": {
    "execution_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "phase": "BROWSER_ELIGIBILITY",
    "category": "lodging",
    "destination": "Kolkata",
    "static_search_completed": true,
    "candidate_places_count": 4,
    "unhandled_browser_requests": 1,
    "browser_escalation_slots_remaining": 3,
    "sanitized_summary": "Static evidence collected for Victoria Memorial Kolkata; dynamic JavaScript portal requires browser rendering for room tariffs."
  },
  "questions": {
    "evidence_routing": {
      "type": "choice",
      "instructions": "Select whether to escalate unhandled dynamic page to browser worker, perform secondary search, or proceed directly to deterministic evaluation.",
      "criteria": {
        "DISPATCH_BROWSER_ESCALATION": "Escalate dynamic lodging page to browser rendering worker",
        "PROCEED_DETERMINISTIC_EVALUATION": "Bypass browser escalation and proceed directly to schedule solving with existing evidence",
        "DISPATCH_EVIDENCE_SEARCH": "Perform secondary web search for regional transit advisories"
      }
    }
  },
  "min_confidence": 0.700,
  "model": "typed-decisions"
}
```

> [!NOTE]
> Sample response payloads below are illustrative wire fixtures used to demonstrate schema structure and field presence. Live inference responses from the remote Kaggle model vary based on model checkpoint and input state.

**Response Wire Payload (Illustrative Fixture)**:
```json
{
  "model": "laya-rl-agent",
  "answers": {
    "evidence_routing": {
      "type": "choice",
      "choice": "DISPATCH_BROWSER_ESCALATION",
      "probabilities": {
        "DISPATCH_BROWSER_ESCALATION": 0.820,
        "PROCEED_DETERMINISTIC_EVALUATION": 0.110,
        "DISPATCH_EVIDENCE_SEARCH": 0.070
      },
      "answer_confidence": 0.820,
      "confidence": null,
      "x_jev_confidence": 0.730,
      "abstention": "passed",
      "abstention_threshold": 0.700,
      "low_confidence": false
    }
  },
  "usage": {
    "input_tokens": 128,
    "output_tokens": 0,
    "state_tokens": 42,
    "state_tokens_dropped": 0,
    "truncated": false
  },
  "routing": {
    "model": "typed-decisions",
    "repo": "convaiinnovations/laya-typed-decisions",
    "reason": "Explicit model selection"
  }
}
```

**Consistency Verification of Illustrative Payload**:
- Probabilities partition unity: `0.820 + 0.110 + 0.070 = 1.000`. All values are finite floats between 0.0 and 1.0.
- Probability keys match offered criteria keys: `{"DISPATCH_BROWSER_ESCALATION", "PROCEED_DETERMINISTIC_EVALUATION", "DISPATCH_EVIDENCE_SEARCH"}`.
- Argmax choice consistency: `choice = "DISPATCH_BROWSER_ESCALATION"` matches the highest probability option `0.820`.
- Top choice alignment: `answer_confidence = 0.820` matches `probabilities[choice]`.
- Calibrated Jev confidence: `x_jev_confidence = 0.730` reflects the calibrated multi choice confidence score for probabilities `[0.820, 0.110, 0.070]`.
- Entropy and distribution confidence: `confidence` represents overall distribution entropy confidence when supplied by extended Jev; when unavailable or omitted in strict mode, it is preserved as `null` rather than equated with `answer_confidence` or `x_jev_confidence`.
- Abstention consistency: `abstention = "passed"` and `low_confidence = false` because `answer_confidence 0.820 >= 0.700`.
- Context and Truncation: When `usage.truncated` is returned as false, server side tokenization completed without truncation. When `usage` or `usage.truncated` is omitted in strict mode (`LAYA_JEV_STRICT=1`), the orchestrator treats truncation metadata as unknown without claiming full context proof or claiming a universal 512 token limit; instead, the client adapter performs defensive request size bounding against configured safe length limits and records truncation status as unknown in audit telemetry.

### Exact Adapter Mapping, Probability Validation, and Strict Response Handling

1. **Request Translation**:
   - `state`: Constructed by extracting sanitized fields (`execution_id`, `phase`, `category`, `destination`, `sanitized_summary`, `static_search_completed`, `unhandled_browser_requests`) from `LayaDecisionRequest.context`.
   - `questions`: Generates question key `evidence_routing` of type `choice`. The `criteria` dictionary includes only the items present in `LayaDecisionRequest.policy_derived_eligible_choices`.
   - `min_confidence`: Populated from `Settings.LAYA_CONFIDENCE_THRESHOLD` (default 0.700).
   - `model`: Configured via `Settings.LAYA_MODEL_NAME` (default: `"typed-decisions"`, or omit to let remote router pick).
2. **Response Translation and Probability Gating**:
   - Reads `answers["evidence_routing"]`.
   - Extracts `choice`, `probabilities`, `answer_confidence`, `confidence`, and optional `abstention`.
   - **Consistent Probability Gating Metric**: The orchestrator uses `answer_confidence` consistently for probability gating against `LAYA_CONFIDENCE_THRESHOLD`. `answer_confidence` directly measures the probability assigned to the chosen decision.
   - **Comprehensive Probability Validation**: Before any proposal is accepted, the policy engine validates:
     1. Key membership: Every key in `probabilities` must be a member of the eligible choices offered in `criteria`.
     2. Finite values: Every probability value must be a finite float (`math.isfinite(p)` and `0.0 <= p <= 1.0`), rejecting NaN and infinite values.
     3. Normalization: The sum of probabilities across all keys must equal 1.0 within tight tolerance (`abs(sum(probabilities.values()) - 1.0) <= 0.01`).
     4. Argmax consistency: The returned `choice` must equal the key with the maximum probability in `probabilities`.
     5. Probability alignment: `answer_confidence` must equal `probabilities[choice]` within floating point tolerance (`abs(answer_confidence - probabilities[choice]) <= 0.001`).
     6. If any probability validation check fails, the proposal is rejected immediately with status `FALLBACK_MALFORMED`.
3. **Strict Response Missing Field Behavior**:
   - When the remote server runs with `LAYA_JEV_STRICT=1`, optional fields such as `answer_confidence`, `x_jev_confidence`, `abstention`, `abstention_threshold`, `low_confidence`, and root `routing` may be absent.
   - The adapter handles strict mode responses gracefully:
     - If `answer_confidence` is missing, the adapter derives it deterministically as `probabilities.get(choice, 0.0)`.
     - If `confidence` is missing or entropy confidence is unavailable, the adapter preserves `confidence` as `None` (null). The adapter never overwrites `confidence` with `answer_confidence`.
     - For deterministic singleton decisions, the orchestrator sets `confidence = None` (null) and `answer_confidence = None` (null); status `DETERMINISTIC_SINGLETON` identifies their source.
     - If both `answer_confidence` and `probabilities` are missing or malformed, the response is classified as `FALLBACK_MALFORMED`.
     - If `abstention` or `low_confidence` is missing, the adapter evaluates abstention locally by verifying `answer_confidence >= LAYA_CONFIDENCE_THRESHOLD`.
     - If root `routing` is missing, the adapter resolves `model_version` from the root `model` field or defaults to `"remote-laya-unknown"`.
4. **Provisional Confidence Threshold**:
   - `LAYA_CONFIDENCE_THRESHOLD` is set provisionally to 0.700 in configuration.
   - In a three choice question space, uniform random baseline probability is 0.333. A threshold of 0.700 requires decisive option preference while tolerating mild dispersion across runner up options.
   - This threshold is provisional because empirical calibration depends on the fine tuned checkpoint deployed on Kaggle. It is subject to adjustment following the bounded labeled evaluation benchmark.
5. **Abstention and Truncation Handling**:
   - If `abstention == "abstained"` or `low_confidence == True` or `answer_confidence < LAYA_CONFIDENCE_THRESHOLD`: the proposal is rejected, recorded with status `FALLBACK_ABSTAINED` or `FALLBACK_LOW_CONFIDENCE`, and routed via the deterministic fallback table.
   - If `usage.truncated == True`: the remote tokenizer truncated prompt state or criteria. Truncated proposals are rejected immediately with status `FALLBACK_TRUNCATED`, triggering the deterministic fallback table.
   - **Strict Mode Truncation Behavior**: When `usage` or `usage.truncated` is unavailable in strict mode, the orchestrator does NOT claim proof of full context and does NOT assert a universal 512 token ceiling. Instead, truncation metadata is treated as unknown (recorded as `"unknown"` in audit telemetry and database records). The client adapter performs defensive pre flight client side request payload bounding against configured safe length limits before dispatch. If payload size exceeds configured limits, the client refuses dispatch or truncates sanitized summaries client side with an explicit policy flag.

### Explicit Workflow Phases, Prerequisites, and Zero or One Choice Behavior

Routing decisions operate across three explicit workflow phases:

```
[Phase 1: Search Completion Phase]
  - Trigger: Brief validated, candidate destinations identified.
  - Required evidence: Core destination coordinates and place identities uncollected.
  - Prerequisites: PROCEED_DETERMINISTIC_EVALUATION strictly ineligible (protects evidence completeness).
                   DISPATCH_BROWSER_ESCALATION strictly ineligible (static search not run, 0 browser requests).
  - Outcome: Exactly ONE eligible choice: DISPATCH_EVIDENCE_SEARCH.
  - Action: Singleton bypass! Latency 0 ms. Dispatches static search without remote inference.
              |
              v
[Phase 2: Browser Eligibility Phase (Optional Enrichment)]
  - Trigger: Static search completes with candidate POIs and emitted NEEDS_BROWSER requests.
  - Required evidence: Core destination identity satisfied (minimum required evidence verified!).
  - Prerequisites: DISPATCH_BROWSER_ESCALATION eligible (unhandled requests exist, budget > 0).
                   PROCEED_DETERMINISTIC_EVALUATION eligible (core evidence sufficient to solve).
                   DISPATCH_EVIDENCE_SEARCH eligible (secondary transit query budget remains).
  - Outcome: Valid MULTI CHOICE state (2 or 3 choices eligible).
  - Action: Remote LAYA query via POST /v1/systemone. Evaluates browser trade off.
              |
              +--> Optional search/browser tasks execute with bounded, deduplicated dispatch.
              |    Upon worker completion, graph resumes:
              |    * If more unhandled browser requests or secondary searches exist with budget:
              |      returns to Phase 2 (Browser Eligibility).
              |    * When optional enrichment is complete or budgets reached:
              |      advances to Phase 3 (Evaluation).
              v
[Phase 3: Evaluation Phase]
  - Trigger: Browser escalation completes (or bypassed in Phase 2).
  - Required evidence: Required destination evidence verified in state.
  - Prerequisites: PROCEED_DETERMINISTIC_EVALUATION is the only eligible choice.
  - Outcome: Exactly ONE eligible choice.
  - Action: Singleton bypass! Latency 0 ms. Advances to itinerary scheduler without remote inference.
```

#### Required Evidence versus Optional Enrichment

1. **Required Destination Evidence**:
   - What is required: Verified destination identity, canonical coordinates (latitude and longitude), and verified core attraction entities. Without required destination evidence, the itinerary solver cannot evaluate or schedule a trip.
   - Invariant: `PROCEED_DETERMINISTIC_EVALUATION` is strictly prohibited until required destination evidence is verified.
   - Core Rule: Never equate exhausted budgets or candidate existence with sufficient evidence. If search budget expires or candidate strings exist without verified coordinates, required evidence is NOT satisfied; the workflow must transition to terminal failure (`status = "FAILED"`, reason `"INSUFFICIENT_REQUIRED_EVIDENCE"`), never skip to evaluation.
2. **Optional Enrichment**:
   - What is optional: Browser rendering for dynamic room tariffs or interactive ticketing pages emitted as `NEEDS_BROWSER`, and secondary search queries for regional transit advisories.
   - Invariant: Optional enrichment occurs only after required destination evidence is already verified in state.
   - The Valid Optional Enrichment Multi Choice Case: When required destination evidence is verified, but unhandled `NEEDS_BROWSER` requests exist with remaining slots (`BROWSER_MAX_ESCALATION_TASKS = 3`) and secondary search budget remains (`SEARCH_MAX_SECONDARY_TASKS = 2`), all three actions (`DISPATCH_BROWSER_ESCALATION`, `PROCEED_DETERMINISTIC_EVALUATION`, `DISPATCH_EVIDENCE_SEARCH`) are genuinely eligible. This represents the valid multi choice routing state evaluated by remote LAYA.
   - Return to Appropriate Routing Phase: When an optional enrichment worker completes, `CompletionReconciliationService` reconciles output. If unhandled optional requests and budget remain, execution returns to Phase 2 (`BROWSER_ELIGIBILITY`). When optional enrichment is complete or budgets are reached, execution advances to Phase 3 (`EVALUATION`).
   - Bounded and Deduplicated Tasks: Tasks are enqueued with deterministic idempotency keys (`{execution_id}:render:{url_hash[:16]}` or `{execution_id}:search:{query_hash[:16]}`). Task dispatch checks existing keys in PostgreSQL before insertion, ensuring zero duplicate tasks and bounded task execution across cycles.

#### Eligibility Prerequisites per Action

1. **`DISPATCH_EVIDENCE_SEARCH`**:
   - Eligible in Phase 1 (Search Completion) if static search has not yet run.
   - Eligible in Phase 2 (Browser Eligibility) for secondary advisory or transit search only if remaining search budget is greater than zero.
   - Parent execution status must be `RUNNING`.
   - Ineligible once search budget is exhausted or for queries already executed.
2. **`DISPATCH_BROWSER_ESCALATION`**:
   - Eligible only in Phase 2 (Browser Eligibility) after static search has completed and produced active unhandled `NEEDS_BROWSER` requests in `browser_requests`.
   - Remaining browser escalation slots must be greater than zero (`len(dispatched_browser_urls) < BROWSER_MAX_ESCALATION_TASKS`, default 3).
   - Target URLs must not have been previously dispatched.
   - Parent execution status must be `RUNNING`.
   - Strictly ineligible in Phase 1 (before search) or Phase 3 (after browser completion).
3. **`PROCEED_DETERMINISTIC_EVALUATION`**:
   - Strictly ineligible in Phase 1 (Search Completion) because required destination evidence is unverified.
   - Eligible in Phase 2 (Browser Eligibility) ONLY IF minimum required destination evidence (canonical coordinates and candidate names) is verified in state. If core evidence is missing, evaluation is strictly ineligible, protecting evidence completeness.
   - Eligible in Phase 3 (Evaluation) ONLY IF required destination evidence has been verified in state. Budget exhaustion or candidate existence never substitutes for required evidence.
   - Serves as the natural exit path toward itinerary solving.

#### Zero or One Eligible Choice Behavior

- **Zero eligible choices**:
  - If no action satisfies eligibility rules (for example when all budgets are exhausted or contradictory state occurs), remote LAYA is not queried.
  - The router short circuits deterministically: if and only if required destination evidence is fully verified in state, it defaults to `PROCEED_DETERMINISTIC_EVALUATION`. If required destination evidence is missing or unverified, candidate existence or budget exhaustion never constitutes sufficient evidence; the router transitions the execution cleanly to a terminal failure status (`status = "FAILED"`, reason `"INSUFFICIENT_REQUIRED_EVIDENCE"`). Zero network delay.
- **Exactly one eligible choice**:
  - In Phase 1 and Phase 3, exactly one action satisfies eligibility prerequisites. Querying a remote model is redundant, adds latency, and risks model hallucination.
  - The router bypasses remote inference entirely. It accepts the single eligible choice deterministically.
  - It records a decision in `laya_decisions` with `status="DETERMINISTIC_SINGLETON"`, `model_version="deterministic_singleton"`, `confidence=None`, `answer_confidence=None`, `latency_ms=0`, and `rationale="single eligible choice: deterministic bypass"`.
- **Two or more eligible choices**:
  - In Phase 2, multiple choices qualify. The adapter formulates a remote LAYA question containing the eligible choices in `criteria` and evaluates model output.

### Deterministic Fallback Table

The deterministic fallback table resolves all failure conditions without stalling execution progression, preserving budgets, cancellation barriers, deadlines, and required evidence:

| Trigger / Condition | Resulting Decision Status | Fallback Action Selected | Budget Preserved | Evidence Preservation Rule |
|---|---|---|---|---|
| `LAYA_ENDPOINT_URL` empty or unconfigured | `FALLBACK_UNCONFIGURED` | Phase derived default: `DISPATCH_EVIDENCE_SEARCH` in Phase 1 (Search Completion); `DISPATCH_BROWSER_ESCALATION` in Phase 2 (Browser Eligibility) if unhandled browser requests exist and budget > 0; else `PROCEED_DETERMINISTIC_EVALUATION` in Phase 3 (Evaluation) | Search and browser budgets preserved | Core destination search executes if unperformed |
| Individual socket timeout (2.5s) | `FALLBACK_TIMEOUT` | Same phase derived default | Budgets preserved | Core destination search executes if unperformed |
| Cumulative total deadline exceeded (5.0s) | `FALLBACK_TIMEOUT` | Same phase derived default | Budgets preserved | Core destination search executes if unperformed |
| Remote HTTP 5xx or connection error (after 2 retries) | `FALLBACK_ERROR` | Same phase derived default | Budgets preserved | Core destination search executes if unperformed |
| Remote HTTP 4xx client or auth error (0 retries) | `FALLBACK_ERROR` | Same phase derived default | Budgets preserved | Core destination search executes if unperformed |
| Tokenizer truncation (`usage.truncated == True`) | `FALLBACK_TRUNCATED` | Same phase derived default | Budgets preserved | Core destination search executes if unperformed |
| Malformed wire response or probability validation failure | `FALLBACK_MALFORMED` | Same phase derived default | Budgets preserved | Core destination search executes if unperformed |
| Resolved answer probability below threshold (< 0.700) | `FALLBACK_LOW_CONFIDENCE` | Conservative phase derived default: gather evidence if needed, else solve | Budgets preserved | Core destination search executes if unperformed |
| Explicit model abstention (`abstention == "abstained"`) | `FALLBACK_ABSTAINED` | Conservative phase derived default | Budgets preserved | Core destination search executes if unperformed |
| Ineligible action proposed by model | `REJECTED_BY_POLICY` | Highest priority remaining eligible choice from policy derived choices | Budgets preserved | Only valid eligible choices dispatched |
| Parent execution status `CANCELLED` | `CANCELLED` | HALT immediately; zero tasks dispatched | All budgets frozen | No further actions taken |
| Parent execution deadline exceeded | `FAILED` | HALT immediately; zero tasks dispatched | All budgets frozen | No further actions taken |

**Evidence Preservation Invariant**: If required destination evidence is missing and static search has not run, fallback must never prematurely skip to `PROCEED_DETERMINISTIC_EVALUATION`. It must pick `DISPATCH_EVIDENCE_SEARCH` to ensure required evidence is gathered. If search budget expires without verifying required destination evidence, the workflow halts with terminal error `INSUFFICIENT_REQUIRED_EVIDENCE`; budget exhaustion or candidate existence never substitutes for verified evidence.

### Execution Lease and Transaction Boundaries

Integration follows the established `GraphRunnerService`, checkpointer, and `CompletionReconciliationService` transaction boundaries:

```
[Phase 1: Pre Call Barrier Check]
  - Execution lock: verify status == RUNNING, check cancellation/deadline.
  - Check laya_decisions for existing decision by (execution_id, idempotency_key).
  - Release database lock before outbound network call.
              |
[Phase 2: In Process HTTP Call (or Singleton Bypass)]
  - Executed OUTSIDE any database transaction.
  - Zero PostgreSQL connections held across network round trips.
              |
[Phase 3: LangGraph Node Yield]
  - Node sets pending_tasks in state and updates current_step.
  - LangGraph step yields to GraphRunnerService.
              |
[Phase 4: Transaction 2: Lease Revalidation, Task Dispatch, and Lease Release]
  - Under execution row lock (SELECT ... FOR UPDATE):
    * REVALIDATE active runner lease (execution.lease_token == runner_lease and lease_expiry > now).
    * REVALIDATE cancellation barrier (status != "CANCELLED" and cancelled_at IS NULL).
    * REVALIDATE deadline barrier (deadline_at IS NULL or deadline_at > now).
    * REVALIDATE current policy state (task counts, remaining browser escalation slots, and evidence prerequisites have not changed concurrently).
    * If revalidation fails: discard buffered writes and abort immediately without committing tasks or decisions.
    * If revalidation succeeds:
      - Insert TaskModel rows (status PENDING).
      - Insert matching OutboxEventModel rows (kind TASK_DISPATCH).
      - Persist laya_decisions record atomically in same transaction.
      - Update execution.status = WAITING_TASKS.
      - Clear runner lease (lease_token = None, lease_expiry = None).
      - Flush checkpointer atomically with database session.
              |
[Phase 5: Worker Execution and Completion Receipt]
  - SQS workers consume tasks, write worker_results, insert completion receipts.
  - CompletionReconciliationService deduplicates receipts, merges task_outputs.
  - When remaining active tasks reach 0, commits EXECUTION_RESUME to outbox.
              |
[Phase 6: Graph Resumption]
  - SQS consumer triggers GraphRunnerService.handle_execution_resume.
  - Runner acquires fresh lease, checks barriers, and resumes graph from checkpoint.
  - If additional optional enrichment requests (unhandled browser requests or secondary searches) and budget remain, the graph returns to Phase 2 (Browser Eligibility) with bounded, deduplicated tasks.
  - If optional enrichment is complete or budgets are reached, and required destination evidence is verified, the graph advances to Phase 3 (Evaluation) without repeated routing.
```

If the chosen action is `PROCEED_DETERMINISTIC_EVALUATION`:
- No `pending_tasks` are created.
- The `laya_decisions` record is committed atomically with graph state after revalidating active execution lease, cancellation barrier, deadline barrier, and current policy state under execution row lock.
- The runner retains the active lease and advances synchronously along the edge directly to `solve_schedule_node` without yielding to `WAITING_TASKS`.

### Phase Specific Canonical Replay Digest, Result Reuse Revalidation, and Concurrent Conflicts

#### Phase Specific Canonical Replay Digest Formulation

To guarantee deterministic replay safety across resumes and crash recovery, the request digest captures all factors governing decision generation, explicitly including the execution phase:
```python
digest_input = (
    f"{execution_id}:"
    f"{phase}:"
    f"{context_version}:"
    f"{policy_version}:"
    f"{question_version}:"
    f"{','.join(sorted(action.value for action in eligible_actions))}:"
    f"{sanitized_context_hash}"
)
request_digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
idempotency_key = f"{execution_id}:laya_decision:{phase}:{request_digest[:16]}"
```

#### Result Reuse Revalidation

Prior to issuing a remote query, the adapter checks `laya_decisions` for `(execution_id, idempotency_key)`. Before reusing a recorded decision, it validates four conditions:
1. Recorded `phase` matches the current execution phase.
2. Recorded `context_version` matches current `execution.context_version`.
3. Recorded `policy_version` matches active `LayaPolicyValidator.POLICY_VERSION`.
4. Recorded `allowed_choice` remains an active member of currently evaluated `policy_derived_eligible_choices`.

If all four checks pass, the recorded decision is reused without issuing an outbound network call. If any check fails (for example if available browser slots were consumed concurrently), the stored decision is bypassed and a fresh decision is evaluated.

#### Concurrent Conflict Handling

- The `laya_decisions` table enforces a database level unique constraint: `CONSTRAINT uq_laya_execution_idempotency UNIQUE (execution_id, idempotency_key)`.
- If concurrent runners attempt to insert the same decision, conflict handling loads the existing committed row cleanly.
- Parent execution row locking (`with_for_update()`) in `GraphRunnerService` serializes execution attempts per `execution_id`, preventing concurrent graph stepping.

### Architecture Ownership, Permitted Imports, and Transport

The canonical architecture cleanly separates inference transport from orchestrator governance:

```
backend/src/
  contracts/
    decision.py                 # Shared domain types: DecisionAction, DecisionEnvelope, etc.
  functions/
    decision_router/            # Bounded LAYA inference client domain
      schemas.py                # Upstream wire schemas: LayaWireRequest, LayaWireResponse
      client.py                 # RemoteLayaDecisionAdapter (httpx client, dual auth, timeouts)
      fixtures.py               # FixtureLayaDecisionAdapter (offline wire compliance tests)
    orchestrator/               # Workflow, policy, and dispatch governance
      decision/
        policy.py               # LayaPolicyValidator (eligibility, confidence, fallback table)
        models.py               # LayaDecisionModel SQLAlchemy entity
        repository.py           # Persistence and result reuse queries
      graphs/
        trip_planner/           # route_evidence_decision_node, graph wiring
```

#### Permitted and Forbidden Imports

- `contracts/decision.py`: Domain contracts imported by both `decision_router` and `orchestrator`.
- `functions/decision_router/`: Imports only `contracts.decision`, `config.config`, standard library, `httpx`, and `pydantic`. It must NEVER import from `functions.orchestrator` or workers (`evidence_collector`, `browser`, `optimizer`). It has no database models or engine dependencies.
- `functions/orchestrator/`:
  - Imports the client adapter interface from `functions.decision_router.client`.
  - Imports domain contracts from `contracts.decision`.
  - Preserves existing permitted execution support imports from `functions.orchestrator.execution`:
    - `models.py` (`ExecutionModel`, `TaskModel`, `WorkerResultModel`, `OutboxEventModel`, `CompletionReceiptModel`).
    - `fencing.py` (`WorkerFencingService`, `ExecutionEngineError`, `ExecutionNotFoundError`, `TaskNotFoundError`, `ExecutionMismatchError`).
    - `job_events.py` (`record_job_event`).
    - `checkpointer.py` (`TransactionBoundCheckpointer`).
    - `reconciliation.py` (`CompletionReconciliationService`).
    - `runner.py` (`GraphRunnerService`, `GraphRunnerResult`).
- Workers (`evidence_collector`, `browser`, `optimizer`):
  - Must NEVER import `decision_router` or orchestrator workflow and graph logic (`functions.orchestrator.graphs`, `functions.orchestrator.decision`).
  - Workers preserve their existing explicitly permitted execution support imports from `functions.orchestrator.execution` (`WorkerFencingService`, `ExecutionEngineError`, `ExecutionNotFoundError`, `TaskNotFoundError`, `ExecutionMismatchError`, `TaskModel`, `WorkerResultModel`) as established in specifications 0001 through 0003.

#### Transport

- The transport between orchestrator and decision router client is an in process asynchronous Python client (`httpx.AsyncClient`) invoking the remote Kaggle endpoint over HTTPS via Cloudflare tunnel.
- No SQS queue or Lambda to Lambda hop is introduced for decision routing, keeping packaging lean and avoiding unnecessary AWS infrastructure.
- Preserves the remote Kaggle and Cloudflare deployment decision with zero AWS packaging changes.

### Authentication Separation

The adapter cleanly separates two independent authentication layers:
1. **LAYA Application Authentication**: Passed as standard HTTP header `Authorization: Bearer {LAYA_API_KEY}` when `LAYA_API_KEY` is configured.
2. **Cloudflare Access Service Authentication**: When the Cloudflare tunnel is protected by Cloudflare Access service tokens, the client sends:
   - `CF-Access-Client-Id`: Loaded from `Settings.CF_ACCESS_CLIENT_ID`
   - `CF-Access-Client-Secret`: Loaded from `Settings.CF_ACCESS_CLIENT_SECRET`

Both headers are sanitized and never written to logs or database records.

### Timeout, Total Retry Deadline, and Cancellation Semantics

- **Individual Socket Timeout**: 2.5 seconds per HTTP attempt (`LAYA_REQUEST_TIMEOUT_SECONDS`).
- **Cumulative Total Retry Deadline**: 5.0 seconds overall across all attempts (`LAYA_TOTAL_DEADLINE_SECONDS`). If elapsed wall clock time reaches 5.0 seconds, retry loops abort immediately without initiating further network requests.
- **Retry Policy**: At most 2 retries with exponential backoff (0.5s initial backoff) exclusively for transient network errors (connection reset, DNS glitch) or HTTP 5xx responses. Zero retries on HTTP 4xx client errors, empty configuration, or when remaining cumulative deadline is below 0.5s.
- **Cancellation and Lease Fencing in Orchestration**: All database access, cancellation barriers, and execution lease fencing checks remain strictly in orchestration (`functions.orchestrator`). Before invoking the remote client adapter, `GraphRunnerService` verifies that the parent execution remains in `RUNNING` status and execution deadline has not expired. After the adapter returns, the orchestrator revalidates runner lease ownership, cancellation status, and deadline barriers under execution row lock before persisting the decision in PostgreSQL. The remote client adapter (`functions.decision_router`) remains completely stateless and database free.

### Data Model Sketch (`laya_decisions`)

```sql
CREATE TABLE laya_decisions (
    id UUID PRIMARY KEY,
    decision_id UUID NOT NULL UNIQUE,
    execution_id UUID NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    trip_id UUID NULL,
    context_version INTEGER NOT NULL DEFAULT 1,
    idempotency_key VARCHAR(255) NOT NULL,
    decision_type VARCHAR(64) NOT NULL,
    allowed_choices JSONB NOT NULL,
    chosen_action VARCHAR(64) NOT NULL,
    confidence NUMERIC(4, 3) NULL,
    answer_confidence NUMERIC(4, 3) NULL,
    model_version VARCHAR(64) NULL,
    policy_version VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    rationale VARCHAR(1000) NULL,
    latency_ms INTEGER NOT NULL,
    request_digest VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_laya_execution_idempotency UNIQUE (execution_id, idempotency_key)
);

CREATE INDEX ix_laya_decisions_execution_id ON laya_decisions(execution_id);
CREATE INDEX ix_laya_decisions_decision_id ON laya_decisions(decision_id);
```

### Value Sourcing

| Action | Value produced / displayed | Source |
|---|---|---|
| Request generation | `decision_id` | Generated UUID v4 |
| Request generation | `policy_derived_eligible_choices` | Filtered capabilities based on remaining execution budget |
| Request generation | `context.execution_id` | From LangGraph `state["execution_id"]` |
| Request generation | `context.sanitized_summary` | Redacted text summary from `state["brief"]` |
| Request generation | `context_version` | From `execution.context_version` |
| Request generation | `request_digest` | SHA256 hash of canonical digest input |
| Remote dispatch | `Authorization` | `Settings.LAYA_API_KEY` (secret, stripped from logs) |
| Remote dispatch | `CF-Access-Client-Id` | `Settings.CF_ACCESS_CLIENT_ID` (secret, stripped from logs) |
| Remote dispatch | `CF-Access-Client-Secret` | `Settings.CF_ACCESS_CLIENT_SECRET` (secret, stripped from logs) |
| Remote dispatch | Endpoint URL | `Settings.LAYA_ENDPOINT_URL` |
| Policy evaluation | `status` | Computed by `LayaPolicyValidator` |
| Policy evaluation | `policy_version` | `LayaPolicyValidator.POLICY_VERSION` constant (`"laya-policy-v1"`) |
| Decision persistence | `trip_id` | From LangGraph `state.get("trip_id")` or NULL |
| Decision persistence | `idempotency_key` | Formatted as `{execution_id}:laya_decision:{phase}:{digest[:16]}` |
| Decision persistence | `rationale` | Policy justification string (max 1000 chars; never invented LLM prose) |
| Decision persistence | `laya_decisions` record | Inserted via active SQLAlchemy session in atomic transaction |
| Graph continuation | `state["pending_tasks"]` | Tasks enqueued for outbox relay based on approved choice |
| Graph continuation | `state["laya_decisions"]` | Appended list of decision summary dictionaries |

### Key Invariants

- **Deterministic Authority**: LAYA proposals cannot directly dispatch worker tasks or mutate database state without passing deterministic policy validation.
- **Strict Separation of Contracts**: SWENA internal domain models never leak upstream wire structures into business logic.
- **No Invented Rationale or Metrics**: Telemetry records exact calibrated scores and policy reasons without synthetic model prose or assumed latency numbers.
- **Budget Safe Conservative Fallback**: Any remote outage, unconfigured URL, timeout, malformed response, or low confidence outcome defaults safely to deterministic static search without crashing executions.
- **Zero Credentials and PII in Logs**: Telemetry and logs redact Bearer tokens, Cloudflare Access headers, and personal traveler names.
- **Yield Wait Resume Conformance**: Task dispatches yield to `WAITING_TASKS` under `GraphRunnerService`, releasing runner leases cleanly and resuming via `CompletionReconciliationService` without repeated routing.

### Configuration Required

- `LAYA_ENDPOINT_URL`: HTTPS URL of remote LAYA service (such as `https://laya.example.cloudflarestream.com`). If empty, triggers immediate fallback without network delay.
- `LAYA_API_KEY`: Optional Bearer authentication token for LAYA application server.
- `CF_ACCESS_CLIENT_ID`: Optional Cloudflare Access service token client identifier.
- `CF_ACCESS_CLIENT_SECRET`: Optional Cloudflare Access service token client secret.
- `LAYA_REQUEST_TIMEOUT_SECONDS`: Individual socket request timeout in seconds (default: 2.5).
- `LAYA_TOTAL_DEADLINE_SECONDS`: Cumulative request deadline across all retries in seconds (default: 5.0).
- `LAYA_MAX_RETRIES`: Maximum retry attempts for transient 5xx or network errors (default: 2).
- `LAYA_CONFIDENCE_THRESHOLD`: Minimum answer_confidence score required to accept proposal (provisional default: 0.700).
- `LAYA_MODEL_NAME`: Target checkpoint name (default: `"typed-decisions"`, or empty to auto route).

### Critical Test Scenarios

- **Offline Wire Contract Compliance**: Validates that request payloads match upstream `/v1/systemone` format and that canned responses with `choice`, `probabilities`, and `abstention` parse correctly into `DecisionEnvelope`, verifies **AC-1**, **AC-2**.
- **Strict Response Missing Field Defaults**: Validates that responses omitting optional fields (`answer_confidence`, `routing`, `abstention`) resolve correctly without exceptions, verifies **AC-2**, **AC-6**.
- **Action Eligibility and Singleton Bypass**: Confirms that browser escalation is excluded when budget is zero, and confirms that when exactly one choice is eligible, remote LAYA is bypassed deterministically with status `DETERMINISTIC_SINGLETON`, verifies **AC-3**.
- **Dual Authentication Headers**: Confirms that Bearer tokens and Cloudflare Access headers are attached when configured and completely stripped from error logs, verifies **AC-4**, **AC-8**.
- **Cumulative Retry Deadline**: Simulates hanging remote calls and asserts that retry loop halts when cumulative elapsed time exceeds 5.0 seconds, verifies **AC-5**.
- **Deterministic Policy Validation and Fallback Table**: Tests boundary answer_confidence scores (0.699 rejected, 0.700 accepted), tokenizer truncations, probability key validation against criteria, finite value constraints, normalization checks, argmax consistency, and confirms that all failure conditions map to correct fallback actions, verifies **AC-6**, **AC-7**.
- **Execution Lease and Task Yielding**: Confirms that approving a task dispatch populates `pending_tasks`, yields execution to `WAITING_TASKS`, releases runner lease, and resumes cleanly through reconciliation without duplicate tasks, verifies **AC-8**.
- **Canonical Replay Digest and Reuse Revalidation**: Tests that re executing the decision node with identical context reuses the database record without issuing duplicate HTTP calls, and verifies that changed context version or invalidated choices bypass reuse, verifies **AC-9**.
- **Connected Real Endpoint Acceptance Gate**: Live integration test checking real Cloudflare tunnel communication against `LAYA_ENDPOINT_URL`. When disabled or unconfigured (`LAYA_ENDPOINT_URL` empty or `RUN_CONNECTED_LAYA_TESTS=0`), the test skips cleanly using `pytest.skip(...)` so offline CI remains unblocked. When enabled and configured, the test executes real HTTP communication and fails on connection failure, authentication errors (401/403), schema mismatch, or protocol violations, verifying **AC-10**.
- **Bounded Labeled Routing Quality Evaluation Benchmark**: A 25 scenario labeled routing quality evaluation measuring decision accuracy (target >= 85%), calibration, abstention rate (target <= 10%), and network latency over Cloudflare tunnel, distinguishing contract mechanics from model accuracy, verifies **AC-10**.

## Build plan

The build plan follows the Tracer Bullet approach, standing up a thin vertical thread through contracts, remote wire client, policy validation, database persistence, and LangGraph integration:

1. **Slice 1: Domain Contracts, Upstream Wire Schemas, Configuration, and Offline Fixture Adapter**
   - [x] Implement `backend/src/contracts/decision.py` defining internal domain models (`DecisionAction`, `DecisionType`, `DecisionPhase`, `DecisionStatus`, `LayaDecisionRequest`, `DecisionEnvelope`).
   - [x] Implement upstream wire schemas (`LayaWireRequest`, `LayaWireResponse`, `LayaChoiceAnswer`) in `backend/src/functions/decision_router/schemas.py` matching `NandhaKishorM/laya` release `v0.4.9`.
   - [x] Update `backend/src/config/config.py` with `LAYA_ENDPOINT_URL`, `LAYA_API_KEY`, `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`, `LAYA_REQUEST_TIMEOUT_SECONDS`, `LAYA_TOTAL_DEADLINE_SECONDS`, `LAYA_MAX_RETRIES`, and `LAYA_CONFIDENCE_THRESHOLD`.
   - [x] Implement `backend/src/functions/decision_router/fixtures.py` providing `FixtureLayaDecisionAdapter` for offline wire contract testing with zero external network dependencies.
   - Satisfies **AC-1**, **AC-2**, **AC-10**.

2. **Slice 2: Remote Wire Client, Dual Auth, Strict Mode Defaults, Cumulative Deadlines, and Telemetry Sanitization**
   - [x] Implement `RemoteLayaDecisionAdapter` in `backend/src/functions/decision_router/client.py` using `httpx.AsyncClient`.
   - [x] Implement query dispatch to `POST /v1/systemone` and health probe to `GET /health`.
   - [x] Implement separate Bearer and Cloudflare Access headers.
   - [x] Implement missing field defaults for strict Jev mode responses.
   - [x] Implement cumulative total deadline (5.0s default) across attempts and socket timeouts (2.5s default).
   - [x] Implement transient 5xx retry backoff and fast failure on 4xx or unconfigured URL.
   - [x] Implement payload sanitization stripping Bearer tokens, Cloudflare tokens, and traveler personal details.
   - Satisfies **AC-2**, **AC-4**, **AC-5**.

3. **Slice 3: Action Eligibility Engine, Policy Validator, and Deterministic Fallback Table**
   - [x] Implement `LayaPolicyValidator` in `backend/src/functions/orchestrator/decision/policy.py`.
   - [x] Implement dynamic choice filtering excluding exhausted budget capabilities prior to request creation.
   - [x] Implement zero choice and single choice deterministic bypass handling.
   - [x] Implement policy gating rules using answer_confidence consistently, enforcing option membership, finite values, unity normalization, argmax consistency, provisional answer_confidence threshold (>= 0.700), abstention verification, and tokenizer truncation rejection.
   - [x] Implement the complete deterministic fallback table mapping timeouts, errors, rejections, truncations, and unconfigured states to safe default actions.
   - Satisfies **AC-3**, **AC-6**, **AC-7**.

4. **Slice 4: Canonical Replay Digest, Result Reuse Revalidation, Database Migration, and Persistence Repository**
   - [x] Create Alembic migration `0011_add_laya_decisions.py` declaring canonical `laya_decisions` table with `context_version`, indexes, and unique constraints on `(execution_id, idempotency_key)`.
   - [x] Implement `LayaDecisionModel` in `backend/src/functions/orchestrator/decision/models.py` and `LayaDecisionRepository` in `repository.py`.
   - [x] Implement canonical replay digest generation and three point result reuse revalidation.
   - [x] Implement atomic session commit persisting decision records alongside workflow state updates.
   - Satisfies **AC-1**, **AC-8**, **AC-9**.

5. **Slice 5: LangGraph Decision Node, Graph Runner Integration, Yield Wait Resume Lifecycle, and Labeled Evaluation Suite**
   - [x] Implement `route_evidence_decision_node` in `backend/src/functions/orchestrator/graphs/trip_planner/graph.py` placed between `discover_candidates` and `escalate_dynamic_evidence`.
   - [x] Wire decision routing actions: if `DISPATCH_EVIDENCE_SEARCH` is approved, enqueue static evidence search task in `pending_tasks`; if `DISPATCH_BROWSER_ESCALATION` is approved, pass to `escalate_dynamic_evidence`; if `PROCEED_DETERMINISTIC_EVALUATION` is approved, advance directly to schedule solving without task generation.
   - [x] Ensure graph step yields to `WAITING_TASKS` under `GraphRunnerService`, releasing runner leases cleanly and resuming via `CompletionReconciliationService` without duplicate tasks.
   - [x] Update `TripPlanningState` with `laya_decisions: list[dict[str, Any]]`.
   - [x] Add end to end integration tests in `backend/tests/unit/test_laya_graph_node.py` verifying full graph execution with offline and mocked remote responses.
   - [x] Implement `test_laya_connected_endpoint_acceptance` checking live Cloudflare tunnel communication; it skips cleanly via `pytest.skip` only when disabled or unconfigured, and fails on endpoint, authentication, or schema errors when enabled.
   - [x] Implement the 25 scenario labeled routing quality evaluation benchmark suite.
   - Satisfies **AC-8**, **AC-10**.

## Consequences

**Positive**:
- Decouples SWENA domain models from external upstream wire formats, insulating orchestrator logic from protocol changes.
- Avoids packaging heavy machine learning libraries into AWS Lambda packages, preserving the 250 MB ceiling and fast cold starts.
- Enforces strict deterministic policy authority, provisional confidence gating, and cumulative timeout deadlines so remote outages never stall workflow execution.
- Prevents redundant remote inference calls when only a single action is eligible.
- Honors execution lease and transaction boundaries, yielding cleanly to `WAITING_TASKS` and resuming without duplicate task dispatches.
- Keeps traveler PII and API credentials completely secure through systematic sanitization.

**Negative / tradeoffs**:
- External HTTP calls over Cloudflare tunnel introduce network round trip latency per decision.
- Requires maintaining an active Kaggle notebook or server instance and Cloudflare tunnel endpoint.

**Neutral**:
- When LAYA is unconfigured or disabled, the router falls back seamlessly to standard deterministic static evidence collection without delay.

## Follow-up

- [x] Measure live Cloudflare tunnel latency and Kaggle inference duration during initial connected testing to tune timeout thresholds (verified: ~568 ms latency).
- [ ] Execute the 25 scenario labeled routing quality benchmark against the connected endpoint to calibrate confidence thresholds before production rollout (PENDING live connected endpoint evaluation; offline deterministic policy regression suite verified at 100% precision on canonical scenarios).

## Remaining Blockers

None. All 5 slices and acceptance criteria AC-1 through AC-10 are implemented, verified live against PostgreSQL and offline suites, with live connected benchmark marked PENDING.
