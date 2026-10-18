"""Decision router module for remote LAYA inference and routing.

Conforms to spec 0004 Slices 1 and 2:
- Upstream wire request and response schemas matching NandhaKishorM/laya v0.4.9.
- Offline fixture adapter for hermetic testing with zero external network calls.
- Remote HTTP client adapter with dual authentication, cumulative deadlines, and telemetry sanitization.
"""

from functions.decision_router.client import (
    QUESTION_KEY_EVIDENCE_ROUTING,
    RemoteLayaDecisionAdapter,
)
from functions.decision_router.fixtures import (
    FixtureLayaDecisionAdapter,
    compute_request_digest,
)
from functions.decision_router.sanitization import (
    sanitize_context,
    sanitize_headers,
    sanitize_text,
)
from functions.decision_router.schemas import (
    LayaChoiceAnswer,
    LayaWireQuestion,
    LayaWireRequest,
    LayaWireResponse,
    LayaWireRouting,
    LayaWireUsage,
    resolve_answer_confidence,
    resolve_model_version,
    resolve_truncation_status,
    validate_wire_probabilities,
)

__all__ = [
    "FixtureLayaDecisionAdapter",
    "LayaChoiceAnswer",
    "LayaWireQuestion",
    "LayaWireRequest",
    "LayaWireResponse",
    "LayaWireRouting",
    "LayaWireUsage",
    "QUESTION_KEY_EVIDENCE_ROUTING",
    "RemoteLayaDecisionAdapter",
    "compute_request_digest",
    "resolve_answer_confidence",
    "resolve_model_version",
    "resolve_truncation_status",
    "sanitize_context",
    "sanitize_headers",
    "sanitize_text",
    "validate_wire_probabilities",
]
