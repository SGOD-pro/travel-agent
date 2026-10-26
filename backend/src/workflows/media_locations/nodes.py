"""Nodes for the Media Location Workflow."""
from .state import MediaLocationState
from functions.media_extractor.service import MediaExtractorService
from functions.media_analyzer.service import MediaAnalyzerService
from functions.media_analyzer.graph import LocationGraphBuilder

async def extract_media_node(state: MediaLocationState) -> dict:
    """Extracts frames and audio from the social URL."""
    service = MediaExtractorService()
    # Mock extraction for now
    return {"extraction_result": None, "attempt_count": state.get("attempt_count", 0) + 1}

async def analyze_media_node(state: MediaLocationState) -> dict:
    """Analyzes the extracted media for OCR, ASR, and clues."""
    service = MediaAnalyzerService()
    # Mock analysis for now
    return {"analyzed": None}

async def verify_candidates_node(state: MediaLocationState) -> dict:
    """Verifies the candidates and produces VerifiedPlace[]."""
    builder = LocationGraphBuilder()
    # Mock verification for now
    return {"verified_places": []}