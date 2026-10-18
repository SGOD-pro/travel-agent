"""Remote HTTP client adapter for LAYA decision routing.

Conforms to spec 0004 Slice 2:
- Translates internal domain requests to upstream POST /v1/systemone wire payloads.
- Supports dual authentication: LAYA application Bearer token and Cloudflare Access service tokens.
- Enforces individual socket timeout (default 2.5s) and cumulative retry deadline (default 5.0s).
- Implements transient 5xx retry backoff (at most 2 retries) and immediate fast failure on 4xx or unconfigured URL.
- Handles strict mode responses gracefully, keeping entropy confidence null and resolving unknown truncation status.
- Sanitizes headers, credentials, and personal traveler data from telemetry and logs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from pydantic import ValidationError

from config.config import Settings, get_settings
from contracts.decision import (
    DecisionAction,
    DecisionEnvelope,
    DecisionPhase,
    DecisionStatus,
    LayaDecisionPort,
    LayaDecisionRequest,
)
from functions.decision_router.fixtures import compute_request_digest
from functions.decision_router.sanitization import (
    sanitize_context,
    sanitize_text,
)
from functions.decision_router.schemas import (
    LayaWireQuestion,
    LayaWireRequest,
    LayaWireResponse,
    resolve_answer_confidence,
    resolve_model_version,
    validate_wire_probabilities,
)

logger = logging.getLogger(__name__)

QUESTION_KEY_EVIDENCE_ROUTING = "evidence_routing"


def _normalize_base_url(url: str) -> str:
    """Normalizes configured endpoint URL, stripping trailing /v1/systemone or /v1 paths."""
    cleaned = url.strip().rstrip("/")
    if cleaned.endswith("/v1/systemone"):
        cleaned = cleaned[:-len("/v1/systemone")].rstrip("/")
    elif cleaned.endswith("/v1"):
        cleaned = cleaned[:-len("/v1")].rstrip("/")
    return cleaned


class RemoteLayaDecisionAdapter(LayaDecisionPort):
    """Remote HTTP client adapter communicating with LAYA SystemOne over Cloudflare tunnel."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = http_client
        self.call_count: int = 0
        self.retry_count: int = 0
        self.last_latency_ms: int = 0

    async def aclose(self) -> None:
        """Closes the underlying HTTP client if owned."""
        if self._client is not None:
            await self._client.aclose()

    async def __aenter__(self) -> RemoteLayaDecisionAdapter:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.aclose()

    def _resolve_fallback_action(self, request: LayaDecisionRequest) -> DecisionAction:
        """Determines the phase derived conservative fallback action.

        Preserves required evidence collection and active execution capabilities.
        """
        eligible = request.policy_derived_eligible_choices

        if request.phase == DecisionPhase.SEARCH_COMPLETION:
            if DecisionAction.DISPATCH_EVIDENCE_SEARCH in eligible:
                return DecisionAction.DISPATCH_EVIDENCE_SEARCH

        elif request.phase == DecisionPhase.BROWSER_ELIGIBILITY:
            unhandled_browser = int(request.context.get("unhandled_browser_requests", 0))
            if (
                unhandled_browser > 0
                and request.budget_remaining > 0
                and DecisionAction.DISPATCH_BROWSER_ESCALATION in eligible
            ):
                return DecisionAction.DISPATCH_BROWSER_ESCALATION
            if DecisionAction.DISPATCH_EVIDENCE_SEARCH in eligible:
                return DecisionAction.DISPATCH_EVIDENCE_SEARCH
            if DecisionAction.PROCEED_DETERMINISTIC_EVALUATION in eligible:
                return DecisionAction.PROCEED_DETERMINISTIC_EVALUATION

        elif request.phase == DecisionPhase.EVALUATION:
            if DecisionAction.PROCEED_DETERMINISTIC_EVALUATION in eligible:
                return DecisionAction.PROCEED_DETERMINISTIC_EVALUATION

        # Fallback safeguard: pick the first eligible choice
        return eligible[0]

    def _create_envelope(
        self,
        request: LayaDecisionRequest,
        allowed_choice: DecisionAction,
        status: DecisionStatus,
        *,
        confidence: float | None = None,
        answer_confidence: float | None = None,
        model_version: str | None = None,
        rationale: str | None = None,
        latency_ms: int = 0,
    ) -> DecisionEnvelope:
        try:
            from functions.orchestrator.decision.repository import compute_canonical_replay_digest

            digest, _ = compute_canonical_replay_digest(
                execution_id=request.execution_id,
                phase=request.phase,
                context_version=request.context_version,
                policy_version=request.policy_version,
                question_version=request.question_version,
                eligible_actions=request.policy_derived_eligible_choices,
                sanitized_context=request.context,
            )
        except Exception:
            digest = compute_request_digest(
                execution_id=str(request.execution_id),
                phase=request.phase.value,
                context_version=request.context_version,
                policy_version=request.policy_version,
                question_version=request.question_version,
                eligible_choices=[c.value for c in request.policy_derived_eligible_choices],
            )
        return DecisionEnvelope(
            decision_id=request.decision_id or uuid4(),
            decision_type=request.decision_type,
            phase=request.phase,
            execution_id=request.execution_id,
            trip_id=request.trip_id,
            idempotency_key=request.idempotency_key,
            allowed_choice=allowed_choice,
            status=status,
            confidence=confidence,
            answer_confidence=answer_confidence,
            model_version=model_version,
            policy_version=request.policy_version,
            rationale=rationale,
            latency_ms=latency_ms,
            request_digest=digest,
            created_at=datetime.now(UTC).isoformat(),
        )

    def _build_request_headers(self) -> dict[str, str]:
        """Constructs dual authentication headers for outbound requests."""
        headers = {"Content-Type": "application/json"}
        if self.settings.LAYA_API_KEY:
            headers["Authorization"] = f"Bearer {self.settings.LAYA_API_KEY}"
        if self.settings.CF_ACCESS_CLIENT_ID:
            headers["CF-Access-Client-Id"] = self.settings.CF_ACCESS_CLIENT_ID
        if self.settings.CF_ACCESS_CLIENT_SECRET:
            headers["CF-Access-Client-Secret"] = self.settings.CF_ACCESS_CLIENT_SECRET
        return headers

    def _build_wire_payload(self, request: LayaDecisionRequest) -> dict[str, Any]:
        """Translates domain request into sanitized upstream wire payload."""
        sanitized_state = sanitize_context(request.context)
        criteria: dict[str, str] = {}
        action_descriptions = {
            DecisionAction.DISPATCH_BROWSER_ESCALATION: "Escalate dynamic lodging page to browser rendering worker",
            DecisionAction.PROCEED_DETERMINISTIC_EVALUATION: "Bypass browser escalation and proceed directly to schedule solving with existing evidence",
            DecisionAction.DISPATCH_EVIDENCE_SEARCH: "Perform secondary web search for regional transit advisories",
        }
        for act in request.policy_derived_eligible_choices:
            criteria[act.value] = action_descriptions.get(act, f"Action {act.value}")

        wire_req = LayaWireRequest(
            state=sanitized_state,
            questions={
                QUESTION_KEY_EVIDENCE_ROUTING: LayaWireQuestion(
                    type="choice",
                    instructions="Select next routing step based on evidence completeness and budget",
                    criteria=criteria,
                )
            },
            min_confidence=self.settings.LAYA_CONFIDENCE_THRESHOLD,
            model=self.settings.LAYA_MODEL_NAME,
        )
        return wire_req.model_dump(mode="json")

    async def query_decision(self, request: LayaDecisionRequest) -> DecisionEnvelope:
        """Translates domain request, invokes remote LAYA model, and returns normalized envelope."""
        self.call_count += 1
        start_time = time.monotonic()

        # 1. Deterministic Singleton Bypass
        if len(request.policy_derived_eligible_choices) == 1:
            single_choice = request.policy_derived_eligible_choices[0]
            self.last_latency_ms = 0
            return self._create_envelope(
                request=request,
                allowed_choice=single_choice,
                status=DecisionStatus.DETERMINISTIC_SINGLETON,
                confidence=None,
                answer_confidence=None,
                model_version="deterministic_singleton",
                rationale="single eligible choice: deterministic bypass",
                latency_ms=0,
            )

        # 2. Unconfigured Endpoint Fast Failure
        endpoint_url = (self.settings.LAYA_ENDPOINT_URL or "").strip()
        if not endpoint_url:
            fallback_action = self._resolve_fallback_action(request)
            self.last_latency_ms = 0
            return self._create_envelope(
                request=request,
                allowed_choice=fallback_action,
                status=DecisionStatus.FALLBACK_UNCONFIGURED,
                confidence=None,
                answer_confidence=None,
                model_version=None,
                rationale="LAYA_ENDPOINT_URL is empty or unconfigured",
                latency_ms=0,
            )

        headers = self._build_request_headers()
        wire_payload = self._build_wire_payload(request)
        base_url = _normalize_base_url(endpoint_url)
        target_url = f"{base_url}/v1/systemone"

        total_deadline = self.settings.LAYA_TOTAL_DEADLINE_SECONDS
        socket_timeout = self.settings.LAYA_REQUEST_TIMEOUT_SECONDS
        max_retries = self.settings.LAYA_MAX_RETRIES

        owns_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=socket_timeout)
            owns_client = True

        raw_response: httpx.Response | None = None
        last_error_rationale: str | None = None
        last_error_status: DecisionStatus = DecisionStatus.FALLBACK_ERROR

        try:
            for attempt in range(max_retries + 1):
                elapsed = time.monotonic() - start_time
                remaining_deadline = total_deadline - elapsed

                if remaining_deadline <= 0 or (attempt > 0 and remaining_deadline < 0.5):
                    last_error_rationale = (
                        f"Cumulative retry deadline ({total_deadline:.1f}s) exceeded before attempt {attempt + 1}"
                    )
                    last_error_status = DecisionStatus.FALLBACK_TIMEOUT
                    break

                attempt_timeout = min(socket_timeout, remaining_deadline)

                try:
                    resp = await client.post(
                        target_url,
                        json=wire_payload,
                        headers=headers,
                        timeout=attempt_timeout,
                    )

                    if resp.status_code == 200:
                        raw_response = resp
                        break

                    # 4xx Client or Auth Error: fail immediately with zero retries
                    if 400 <= resp.status_code < 500:
                        last_error_rationale = (
                            f"Remote HTTP {resp.status_code} client error: zero retries initiated"
                        )
                        last_error_status = DecisionStatus.FALLBACK_ERROR
                        break

                    # 5xx Server Error: retry if budget remains
                    if resp.status_code >= 500:
                        last_error_rationale = f"Remote HTTP {resp.status_code} server error"
                        last_error_status = DecisionStatus.FALLBACK_ERROR
                        if attempt < max_retries:
                            self.retry_count += 1
                            rem = total_deadline - (time.monotonic() - start_time)
                            backoff = min(0.5 * (2**attempt), max(0.0, rem - 0.1))
                            if backoff > 0 and rem >= 0.5:
                                await asyncio.sleep(backoff)
                            continue
                        break

                except httpx.TimeoutException:
                    last_error_rationale = f"Socket timeout ({attempt_timeout:.1f}s) on attempt {attempt + 1}"
                    last_error_status = DecisionStatus.FALLBACK_TIMEOUT
                    if attempt < max_retries:
                        rem = total_deadline - (time.monotonic() - start_time)
                        if rem >= 0.5:
                            self.retry_count += 1
                            backoff = min(0.5 * (2**attempt), max(0.0, rem - 0.1))
                            if backoff > 0:
                                await asyncio.sleep(backoff)
                            continue
                    break

                except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.NetworkError) as net_err:
                    clean_msg = sanitize_text(
                        str(net_err),
                        [self.settings.LAYA_API_KEY, self.settings.CF_ACCESS_CLIENT_SECRET],
                    )
                    last_error_rationale = f"Transient network error on attempt {attempt + 1}: {clean_msg}"
                    last_error_status = DecisionStatus.FALLBACK_ERROR
                    if attempt < max_retries:
                        rem = total_deadline - (time.monotonic() - start_time)
                        if rem >= 0.5:
                            self.retry_count += 1
                            backoff = min(0.5 * (2**attempt), max(0.0, rem - 0.1))
                            if backoff > 0:
                                await asyncio.sleep(backoff)
                            continue
                    break

        finally:
            if owns_client:
                await client.aclose()

        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        self.last_latency_ms = elapsed_ms

        # Network or HTTP failure occurred
        if raw_response is None:
            fallback_action = self._resolve_fallback_action(request)
            return self._create_envelope(
                request=request,
                allowed_choice=fallback_action,
                status=last_error_status,
                confidence=None,
                answer_confidence=None,
                model_version=None,
                rationale=last_error_rationale or "Remote inference call failed",
                latency_ms=elapsed_ms,
            )

        # 3. Parse and Validate Successful Response
        try:
            resp_data = raw_response.json()
            wire_response = LayaWireResponse.model_validate(resp_data)
        except (ValueError, ValidationError) as parse_err:
            fallback_action = self._resolve_fallback_action(request)
            return self._create_envelope(
                request=request,
                allowed_choice=fallback_action,
                status=DecisionStatus.FALLBACK_MALFORMED,
                confidence=None,
                answer_confidence=None,
                model_version=None,
                rationale=f"Malformed response JSON or schema failure: {parse_err}",
                latency_ms=elapsed_ms,
            )

        # Truncation check
        if wire_response.usage and wire_response.usage.truncated is True:
            fallback_action = self._resolve_fallback_action(request)
            return self._create_envelope(
                request=request,
                allowed_choice=fallback_action,
                status=DecisionStatus.FALLBACK_TRUNCATED,
                confidence=None,
                answer_confidence=None,
                model_version=resolve_model_version(wire_response),
                rationale="Remote tokenizer truncated request prompt state",
                latency_ms=elapsed_ms,
            )

        # Extract answer
        answer = wire_response.answers.get(QUESTION_KEY_EVIDENCE_ROUTING)
        if answer is None:
            fallback_action = self._resolve_fallback_action(request)
            return self._create_envelope(
                request=request,
                allowed_choice=fallback_action,
                status=DecisionStatus.FALLBACK_MALFORMED,
                confidence=None,
                answer_confidence=None,
                model_version=resolve_model_version(wire_response),
                rationale=f"Response missing answer for '{QUESTION_KEY_EVIDENCE_ROUTING}'",
                latency_ms=elapsed_ms,
            )

        # Choice membership check
        try:
            chosen_action = DecisionAction(answer.choice)
        except ValueError:
            fallback_action = self._resolve_fallback_action(request)
            return self._create_envelope(
                request=request,
                allowed_choice=fallback_action,
                status=DecisionStatus.FALLBACK_MALFORMED,
                confidence=None,
                answer_confidence=None,
                model_version=resolve_model_version(wire_response),
                rationale=f"Model proposed unknown action '{answer.choice}'",
                latency_ms=elapsed_ms,
            )

        if chosen_action not in request.policy_derived_eligible_choices:
            fallback_action = self._resolve_fallback_action(request)
            return self._create_envelope(
                request=request,
                allowed_choice=fallback_action,
                status=DecisionStatus.REJECTED_BY_POLICY,
                confidence=None,
                answer_confidence=None,
                model_version=resolve_model_version(wire_response),
                rationale=f"Proposed choice '{chosen_action.value}' is not among eligible actions",
                latency_ms=elapsed_ms,
            )

        # Probability validation if probabilities dict is present
        if answer.probabilities:
            try:
                allowed_str_keys = [c.value for c in request.policy_derived_eligible_choices]
                validate_wire_probabilities(answer, allowed_choices=allowed_str_keys)
            except ValueError as val_err:
                fallback_action = self._resolve_fallback_action(request)
                return self._create_envelope(
                    request=request,
                    allowed_choice=fallback_action,
                    status=DecisionStatus.FALLBACK_MALFORMED,
                    confidence=None,
                    answer_confidence=None,
                    model_version=resolve_model_version(wire_response),
                    rationale=f"Probability distribution validation failed: {val_err}",
                    latency_ms=elapsed_ms,
                )

        resolved_ans_conf = resolve_answer_confidence(answer)
        model_version = resolve_model_version(wire_response)

        # Abstention check
        if answer.abstention == "abstained":
            fallback_action = self._resolve_fallback_action(request)
            return self._create_envelope(
                request=request,
                allowed_choice=fallback_action,
                status=DecisionStatus.FALLBACK_ABSTAINED,
                confidence=answer.confidence,
                answer_confidence=resolved_ans_conf,
                model_version=model_version,
                rationale="Model server explicitly abstained from proposal",
                latency_ms=elapsed_ms,
            )

        # Low confidence check
        if answer.low_confidence is True or resolved_ans_conf < self.settings.LAYA_CONFIDENCE_THRESHOLD:
            fallback_action = self._resolve_fallback_action(request)
            return self._create_envelope(
                request=request,
                allowed_choice=fallback_action,
                status=DecisionStatus.FALLBACK_LOW_CONFIDENCE,
                confidence=answer.confidence,
                answer_confidence=resolved_ans_conf,
                model_version=model_version,
                rationale=(
                    f"Resolved answer confidence {resolved_ans_conf:.3f} below threshold "
                    f"{self.settings.LAYA_CONFIDENCE_THRESHOLD:.3f}"
                ),
                latency_ms=elapsed_ms,
            )

        # Proposal accepted and validated
        return self._create_envelope(
            request=request,
            allowed_choice=chosen_action,
            status=DecisionStatus.VALIDATED,
            confidence=answer.confidence,
            answer_confidence=resolved_ans_conf,
            model_version=model_version,
            rationale=f"Remote inference proposal validated (answer_confidence={resolved_ans_conf:.3f})",
            latency_ms=elapsed_ms,
        )

    async def check_health(self) -> bool:
        """Executes lightweight GET /health probe against remote LAYA endpoint."""
        endpoint_url = (self.settings.LAYA_ENDPOINT_URL or "").strip()
        if not endpoint_url:
            return False

        headers = self._build_request_headers()
        base_url = _normalize_base_url(endpoint_url)
        health_url = f"{base_url}/health"
        timeout = min(self.settings.LAYA_REQUEST_TIMEOUT_SECONDS, 2.5)

        owns_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=timeout)
            owns_client = True

        try:
            resp = await client.get(health_url, headers=headers, timeout=timeout)
            return resp.status_code == 200
        except Exception as exc:
            safe_err = sanitize_text(
                str(exc),
                [self.settings.LAYA_API_KEY, self.settings.CF_ACCESS_CLIENT_SECRET],
            )
            logger.warning("Remote LAYA health check failed: %s", safe_err)
            return False
        finally:
            if owns_client:
                await client.aclose()
