"""State schema for the Media Location Workflow."""

from typing import TypedDict

from contracts.media import (
    AsrObservation,
    MediaAnalyzed,
    MediaExtractionResult,
    MediaLocationGraph,
    VerifiedPlace,
)


class MediaLocationState(TypedDict):
    url: str
    extraction_result: MediaExtractionResult | None
    caption_observation: AsrObservation | None
    analyzed: MediaAnalyzed | None
    location_graph: MediaLocationGraph | None
    verified_places: list[VerifiedPlace]
    attempt_count: int
