# 0004. Remote LAYA Decision Router Integration

**Date**: 2026-10-17  
**Status**: Proposed

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
- **AC-1**: SWENA defines internal typed domain contracts (`LayaDecisionRequest`, `DecisionEnvelope`, `DecisionAction`, `DecisionStatus`) that remain decoupled from upstream wire protocols.
- **AC-2**: The remote client adapter implements the upstream Jev compatible wire protocol (`POST /v1/systemone`), transforming SWENA internal requests into `state` and typed `choice` questions, and parsing returned `answers` dictionaries.
- **AC-3**: Allowed choices are dynamically restricted to policy derived eligible choices before remote query generation; capabilities whose execution budgets are exhausted (such as browser escalation slots) are excluded from the question criteria.
- **AC-4**: Remote client enforces configurable endpoints (`LAYA_ENDPOINT_URL`), separate Bearer authentication (`LAYA_API_KEY`), and optional Cloudflare Access service tokens (`CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`).
- **AC-5**: Remote client enforces a cumulative total retry deadline (default 5.0 seconds across all attempts) with individual socket timeout (default 2.5 seconds), executing at most 2 retries on transient network errors or 5xx responses, and failing immediately with zero retries on 4xx client errors or when `LAYA_ENDPOINT_URL` is empty.
- **AC-6**: Deterministic policy engine (`LayaPolicyValidator`) validates all responses before dispatch, verifying that proposed actions exist in policy derived eligible choices, that remaining budgets permit execution, that the parent execution remains in running status, and that calibrated confidence meets or exceeds 0.700, rejecting low confidence or abstained responses.
- **AC-7**: Explicit deterministic fallback activates whenever the endpoint is unconfigured, unreachable, exceeds cumulative deadlines, returns 5xx or 4xx errors, provides malformed payloads, abstains, or proposes rejected actions, routing safely to default static search or schedule solving without interrupting graph progression.
- **AC-8**: Decision telemetry records execution identifiers, idempotency keys, decision types, chosen actions, policy status, request payload SHA256 digests, and round trip latency in a dedicated `laya_decisions` table with replay reuse and PII redaction.
- **AC-9**: The test suite provides offline wire contract unit tests with canned fixtures, connected smoke tests for optional live endpoint validation, and an end to end LangGraph orchestration scenario, explicitly distinguishing fixture contract compliance from model decision quality.

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


class DecisionStatus(StrEnum):
    VALIDATED = "VALIDATED"
    REJECTED_BY_POLICY = "REJECTED_BY_POLICY"
    FALLBACK_TIMEOUT = "FALLBACK_TIMEOUT"
    FALLBACK_ERROR = "FALLBACK_ERROR"
    FALLBACK_LOW_CONFIDENCE = "FALLBACK_LOW_CONFIDENCE"
    FALLBACK_ABSTAINED = "FALLBACK_ABSTAINED"
    FALLBACK_UNCONFIGURED = "FALLBACK_UNCONFIGURED"


class LayaDecisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    decision_type: DecisionType = DecisionType.EVIDENCE_ROUTING
    execution_id: uuid.UUID
    trip_id: uuid.UUID | None = None
    policy_derived_eligible_choices: list[DecisionAction]
    budget_remaining: int
    context: dict[str, Any]
    idempotency_key: str


class DecisionEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: uuid.UUID
    decision_type: DecisionType
    execution_id: uuid.UUID
    trip_id: uuid.UUID | None
    idempotency_key: str
    allowed_choice: DecisionAction
    status: DecisionStatus
    confidence: float | None = None
    answer_confidence: float | None = None
    model_version: str | None = None
    policy_version: str
    rationale: str | None = None
    latency_ms: int
    request_digest: str
    created_at: str
```

#### 2. Upstream Wire Contract (`POST /v1/systemone`)

Conforming to `NandhaKishorM/laya` release `v0.4.9`:

**Request Wire Payload**:
```json
{
  "state": {
    "execution_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "category": "lodging",
    "query": "hotels near Victoria Memorial Kolkata",
    "sanitized_summary": "Lodging research for Kolkata destination"
  },
  "questions": {
    "evidence_routing": {
      "type": "choice",
      "instructions": "Select the appropriate evidence collection capability for this travel planning state.",
      "criteria": {
        "DISPATCH_EVIDENCE_SEARCH": "Perform static web search and permitted page extraction",
        "DISPATCH_BROWSER_ESCALATION": "Escalate to dynamic browser rendering worker",
        "PROCEED_DETERMINISTIC_EVALUATION": "Bypass evidence collection and proceed directly to deterministic evaluation"
      }
    }
  },
  "min_confidence": 0.700,
  "model": "typed-decisions"
}
```

**Response Wire Payload**:
```json
{
  "model": "laya-rl-agent",
  "answers": {
    "evidence_routing": {
      "type": "choice",
      "choice": "DISPATCH_EVIDENCE_SEARCH",
      "probabilities": {
        "DISPATCH_EVIDENCE_SEARCH": 0.880,
        "DISPATCH_BROWSER_ESCALATION": 0.080,
        "PROCEED_DETERMINISTIC_EVALUATION": 0.040
      },
      "confidence": 0.797,
      "answer_confidence": 0.880,
      "x_jev_confidence": 0.850,
      "abstention": "passed",
      "abstention_threshold": 0.700,
      "low_confidence": false
    }
  },
  "usage": {
    "input_tokens": 83,
    "output_tokens": 0,
    "state_tokens": 12,
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

### Exact Adapter Mapping

1. **Request Translation**:
   - `state`: Constructed by extracting sanitized fields (`execution_id`, `category`, `query`, `sanitized_summary`) from `LayaDecisionRequest.context`.
   - `questions`: Generates question key `evidence_routing` of type `choice`. The `criteria` dictionary includes only the items present in `LayaDecisionRequest.policy_derived_eligible_choices`. If browser budget is 0, `DISPATCH_BROWSER_ESCALATION` is omitted from `criteria`.
   - `min_confidence`: Populated from `Settings.LAYA_CONFIDENCE_THRESHOLD` (default 0.700).
   - `model`: Configured via `Settings.LAYA_MODEL_NAME` (default: `"typed-decisions"`, or omit to let remote router pick).
2. **Response Translation**:
   - Reads `answers["evidence_routing"]`.
   - Extracts `choice`, `confidence` (normalized entropy), `answer_confidence` (top option probability), and optional `abstention`.
   - Reads `model` string from root payload or `routing.model`.
   - Does NOT invent model rationale: LAYA returns calibrated probabilities without natural language explanations. Internal `rationale` field records deterministic policy explanations (e.g. `"policy: verified confidence 0.880 >= 0.700"` or `"fallback: remote socket timeout after 2.5s"`).

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
- **Cancellation Check**: Before dispatching the remote request and before persisting the decision in PostgreSQL, the adapter validates that the parent execution remains in `RUNNING` status and worker lease has not expired. If cancelled, the operation halts immediately without scheduling fallback tasks.

### Replay Safety, Result Reuse, and Crash Window Semantics

- **Deterministic Idempotency Key**: Formatted as `{execution_id}:laya_decision:{decision_type}:{context_digest[:16]}`.
- **Result Reuse**: Prior to remote dispatch, the adapter queries `laya_decisions` for `(execution_id, idempotency_key)`. If a committed decision already exists (such as after LangGraph node recovery from checkpoint), the adapter reuses the recorded decision and advances without invoking the remote endpoint.
- **Crash Window Tolerance**:
  - Crash during remote HTTP call: PostgreSQL state is unmodified; graph recovery reruns node, finds no recorded decision, and re evaluates or executes fallback.
  - Crash after remote call but before database commit: Decision was not committed; graph recovery reruns node with identical idempotency key.
  - Transactional Consistency: Persistence of the `laya_decisions` row is committed in the same database transaction that enqueues outbox tasks and updates graph state.

### Data Model Sketch (`laya_decisions`)

```sql
CREATE TABLE laya_decisions (
    id UUID PRIMARY KEY,
    decision_id UUID NOT NULL UNIQUE,
    execution_id UUID NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    trip_id UUID NULL,
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
| Request generation | `request_digest` | SHA256 hash of sanitized wire request JSON |
| Remote dispatch | `Authorization` | `Settings.LAYA_API_KEY` (secret, stripped from logs) |
| Remote dispatch | `CF-Access-Client-Id` | `Settings.CF_ACCESS_CLIENT_ID` (secret, stripped from logs) |
| Remote dispatch | `CF-Access-Client-Secret` | `Settings.CF_ACCESS_CLIENT_SECRET` (secret, stripped from logs) |
| Remote dispatch | Endpoint URL | `Settings.LAYA_ENDPOINT_URL` |
| Policy evaluation | `status` | Computed by `LayaPolicyValidator` |
| Policy evaluation | `policy_version` | `LayaPolicyValidator.POLICY_VERSION` constant (`"laya-policy-v1"`) |
| Decision persistence | `trip_id` | From LangGraph `state.get("trip_id")` or NULL |
| Decision persistence | `idempotency_key` | Formatted as `{execution_id}:laya_decision:{decision_type}:{digest[:16]}` |
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

### Configuration Required

- `LAYA_ENDPOINT_URL`: HTTPS URL of remote LAYA service (e.g. `https://laya.example.cloudflarestream.com`). If empty, triggers immediate fallback without network delay.
- `LAYA_API_KEY`: Optional Bearer authentication token for LAYA application server.
- `CF_ACCESS_CLIENT_ID`: Optional Cloudflare Access service token client identifier.
- `CF_ACCESS_CLIENT_SECRET`: Optional Cloudflare Access service token client secret.
- `LAYA_REQUEST_TIMEOUT_SECONDS`: Individual socket request timeout in seconds (default: 2.5).
- `LAYA_TOTAL_DEADLINE_SECONDS`: Cumulative request deadline across all retries in seconds (default: 5.0).
- `LAYA_MAX_RETRIES`: Maximum retry attempts for transient 5xx or network errors (default: 2).
- `LAYA_CONFIDENCE_THRESHOLD`: Minimum confidence score required to accept proposal (default: 0.700).
- `LAYA_MODEL_NAME`: Target checkpoint name (default: `"typed-decisions"`, or empty to auto route).

### Critical Test Scenarios

- **Offline Wire Contract Compliance**: Validates that request payloads match upstream `/v1/systemone` format and that canned responses with `choice`, `probabilities`, and `abstention` parse correctly into `DecisionEnvelope`, verifies **AC-1**, **AC-2**.
- **Eligible Choice Restriction**: Confirms that when browser escalation budget is exhausted, `DISPATCH_BROWSER_ESCALATION` is omitted from question criteria, verifies **AC-3**.
- **Dual Authentication Headers**: Confirms that Bearer tokens and Cloudflare Access headers are attached when configured and completely stripped from error logs, verifies **AC-4**, **AC-8**.
- **Cumulative Retry Deadline**: Simulates hanging remote calls and asserts that retry loop halts when cumulative elapsed time exceeds 5.0 seconds, verifies **AC-5**.
- **Deterministic Policy Validation**: Tests boundary scores (0.699 rejected, 0.700 accepted) and confirms that low confidence proposals or abstentions trigger fallback, verifies **AC-6**, **AC-7**.
- **Unconfigured Endpoint Fast Path**: Tests that empty `LAYA_ENDPOINT_URL` falls back immediately with zero socket creation and zero network delay, verifies **AC-5**, **AC-7**.
- **Replay Safety and Idempotency**: Tests that executing the decision node twice with the same idempotency key reuses the database record without issuing a second HTTP request, verifies **AC-8**.
- **Parent Cancellation Handling**: Confirms that if parent execution status becomes `CANCELLED`, decision routing halts without enqueuing tasks, verifies **AC-6**.
- **Connected Real Endpoint Smoke Test**: Optional live integration test checking real Cloudflare tunnel communication when credentials are provided in environment, verifying transport parity, verifies **AC-9**.
- **Model Decision Quality Distinction**: Documentation and test assertions explicitly differentiate fixture passing (which tests contract mechanics) from fine tuned model accuracy (which requires offline dataset benchmark evaluations), verifies **AC-9**.

## Build plan

The build plan follows the Tracer Bullet approach, standing up a thin vertical thread through contracts, remote wire client, policy validation, database persistence, and LangGraph integration:

1. **Slice 1: Domain Contracts, Upstream Wire Schemas, Configuration, and Offline Fixture Adapter**
   - Implement `backend/src/contracts/decision.py` defining internal domain models (`DecisionAction`, `DecisionType`, `DecisionStatus`, `LayaDecisionRequest`, `DecisionEnvelope`).
   - Implement upstream wire schemas (`LayaWireRequest`, `LayaWireResponse`, `LayaChoiceAnswer`) matching `NandhaKishorM/laya` release `v0.4.9`.
   - Update `backend/src/config/config.py` with `LAYA_ENDPOINT_URL`, `LAYA_API_KEY`, `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`, `LAYA_REQUEST_TIMEOUT_SECONDS`, `LAYA_TOTAL_DEADLINE_SECONDS`, `LAYA_MAX_RETRIES`, and `LAYA_CONFIDENCE_THRESHOLD`.
   - Implement `backend/src/functions/orchestrator/decision/fixtures.py` providing `FixtureLayaDecisionAdapter` for offline wire contract testing with zero external network dependencies.
   - Satisfies **AC-1**, **AC-2**, **AC-9**.

2. **Slice 2: Remote Wire Client, Dual Auth, Cumulative Deadlines, and Telemetry Sanitization**
   - Implement `RemoteLayaDecisionAdapter` in `backend/src/functions/orchestrator/decision/client.py` using `httpx.AsyncClient`.
   - Implement query dispatch to `POST /v1/systemone` and health probe to `GET /health`.
   - Implement separate Bearer and Cloudflare Access headers.
   - Implement cumulative total deadline (5.0s default) across attempts and socket timeouts (2.5s default).
   - Implement transient 5xx retry backoff and fast failure on 4xx or unconfigured URL.
   - Implement payload sanitization stripping Bearer tokens, Cloudflare tokens, and traveler personal details.
   - Satisfies **AC-2**, **AC-4**, **AC-5**, **AC-8**.

3. **Slice 3: Policy Derived Eligible Choices, Policy Engine, and Deterministic Fallback**
   - Implement `LayaPolicyValidator` in `backend/src/functions/orchestrator/decision/policy.py`.
   - Implement dynamic choice filtering excluding exhausted budget capabilities prior to request creation.
   - Implement policy gating rules enforcing option membership, confidence threshold (>= 0.700), abstention verification, and execution lease validity.
   - Implement deterministic fallback resolution mapping timeouts, errors, rejections, and unconfigured states to safe default actions.
   - Satisfies **AC-3**, **AC-6**, **AC-7**.

4. **Slice 4: Database Migration, Persistence Repository, and Replay Deduplication**
   - Create Alembic migration `0011_add_laya_decisions.py` declaring canonical `laya_decisions` table with indexes and unique constraints on `(execution_id, idempotency_key)`.
   - Implement `LayaDecisionModel` in `backend/src/functions/orchestrator/decision/models.py` and `LayaDecisionRepository` in `repository.py`.
   - Implement atomic session commit persisting decision records alongside workflow state updates.
   - Implement result reuse querying existing records before dispatch.
   - Satisfies **AC-1**, **AC-8**.

5. **Slice 5: LangGraph Decision Node, Workflow Wiring, and E2E Acceptance Scenario**
   - Implement `route_evidence_decision_node` in `backend/src/functions/orchestrator/graphs/trip_planner/graph.py` placed between `discover_candidates` and `escalate_dynamic_evidence`.
   - Wire decision routing actions: if `DISPATCH_EVIDENCE_SEARCH` is approved, enqueue static evidence search task in `pending_tasks`; if `DISPATCH_BROWSER_ESCALATION` is approved, pass to `escalate_dynamic_evidence`; if `PROCEED_DETERMINISTIC_EVALUATION` is approved, advance directly to schedule solving without task generation.
   - Update `TripPlanningState` with `laya_decisions: list[dict[str, Any]]`.
   - Add end to end integration tests in `backend/tests/unit/test_laya_decision_router.py` verifying full graph execution with offline and mocked remote responses.
   - Provide optional connected smoke test verifying live Cloudflare endpoint communication when credentials and URL are present.
   - Satisfies **AC-9**.

## Consequences

**Positive**:
- Decouples SWENA domain models from external upstream wire formats, insulating orchestrator logic from protocol changes.
- Avoids packaging heavy machine learning libraries into AWS Lambda packages, preserving the 250 MB ceiling and fast cold starts.
- Enforces strict deterministic policy authority and cumulative timeout deadlines so remote outages never stall workflow execution.
- Keeps traveler PII and API credentials completely secure through systematic sanitization.

**Negative / tradeoffs**:
- External HTTP calls over Cloudflare tunnel introduce network round trip latency per decision.
- Requires maintaining an active Kaggle notebook or server instance and Cloudflare tunnel endpoint.

**Neutral**:
- When LAYA is unconfigured or disabled, the router falls back seamlessly to standard deterministic static evidence collection without delay.

## Follow-up

- [ ] Measure live Cloudflare tunnel latency and Kaggle inference duration during initial connected testing to tune timeout thresholds.
- [ ] Evaluate fine tuning domain accuracy against a designated benchmark dataset once the route optimizer milestone is reached.

## Remaining Blockers

1. **Live Cloudflare Endpoint Configuration**: The remote Kaggle instance URL and Cloudflare tunnel credentials must be supplied to execute connected smoke tests; offline wire fixtures allow full development without blocking.
2. **Kaggle Server Lifetime Management**: Automated restart scripts or health monitoring for the Kaggle notebook instance remain external operational dependencies.
