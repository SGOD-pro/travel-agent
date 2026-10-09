"""Evidence collector module."""

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
    parse_inr_price,
)
from functions.evidence_collector.service import (
    EvidenceCollectorService,
    TaskDispatchEnvelope,
    parse_task_dispatch_envelope,
)

ScraplingWebExtractionAdapter = ScraplingExtractionAdapter

__all__ = [
    "EvidenceCollectorService",
    "FixtureSearchAdapter",
    "FixtureWebExtractionAdapter",
    "RawObservation",
    "ScraplingExtractionAdapter",
    "ScraplingWebExtractionAdapter",
    "SourceObservationModel",
    "TaskDispatchEnvelope",
    "WebExtractionPort",
    "compute_content_hash",
    "lambda_handler",
    "normalize_url",
    "parse_inr_price",
    "parse_task_dispatch_envelope",
    "process_batch",
    "process_record",
]
