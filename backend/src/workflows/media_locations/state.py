"""State schema for the Media Location Workflow."""
from typing import TypedDict, List, Optional
from contracts.media import MediaExtractionResult, MediaAnalyzed, VerifiedPlace

class MediaLocationState(TypedDict):
    url: str
    extraction_result: Optional[MediaExtractionResult]
    analyzed: Optional[MediaAnalyzed]
    verified_places: List[VerifiedPlace]
    attempt_count: int