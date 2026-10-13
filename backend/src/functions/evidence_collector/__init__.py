"""Evidence collector module conforming to spec 0002."""

from functions.evidence_collector.consumer import (
    lambda_handler,
    process_batch,
    process_record,
)
from functions.evidence_collector.fixtures import (
    FixtureSearchAdapter,
    FixtureWebExtractionAdapter,
    RawObservation,
    compute_content_hash,
    normalize_url,
)
from functions.evidence_collector.models import SourceObservationModel
from functions.evidence_collector.scrape import (
    ScraplingExtractionAdapter,
    WebExtractionPort,
    detect_dynamic_rendering_needed,
    parse_inr_price,
)
from functions.evidence_collector.serpapi_adapter import (
    SerpApiAdapter,
    SerpApiAdapterError,
    SerpApiRateLimitError,
    SerpApiTimeoutError,
)
from functions.evidence_collector.service import (
    EvidenceCollectorService,
    TaskDispatchEnvelope,
    parse_task_dispatch_envelope,
)
from functions.evidence_collector.ssrf import (
    SSRFPayloadSizeExceededError,
    SSRFSecurityError,
    is_ip_allowed,
    safe_fetch_page,
    validate_hostname_or_ip,
    validate_url_ssrf,
)

ScraplingWebExtractionAdapter = ScraplingExtractionAdapter

__all__ = [
    "EvidenceCollectorService",
    "FixtureSearchAdapter",
    "FixtureWebExtractionAdapter",
    "RawObservation",
    "SSRFPayloadSizeExceededError",
    "SSRFSecurityError",
    "ScraplingExtractionAdapter",
    "ScraplingWebExtractionAdapter",
    "SerpApiAdapter",
    "SerpApiAdapterError",
    "SerpApiRateLimitError",
    "SerpApiTimeoutError",
    "SourceObservationModel",
    "TaskDispatchEnvelope",
    "WebExtractionPort",
    "compute_content_hash",
    "detect_dynamic_rendering_needed",
    "is_ip_allowed",
    "lambda_handler",
    "normalize_url",
    "parse_inr_price",
    "parse_task_dispatch_envelope",
    "process_batch",
    "process_record",
    "safe_fetch_page",
    "validate_hostname_or_ip",
    "validate_url_ssrf",
]
