"""Evidence collector module."""

from functions.evidence_collector.scrape import (
    ScraplingExtractionAdapter,
    WebExtractionPort,
    parse_inr_price,
)

ScraplingWebExtractionAdapter = ScraplingExtractionAdapter

__all__ = [
    "ScraplingExtractionAdapter",
    "ScraplingWebExtractionAdapter",
    "WebExtractionPort",
    "parse_inr_price",
]
