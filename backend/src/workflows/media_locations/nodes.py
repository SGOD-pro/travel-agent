"""Execution nodes for direct-media extraction, analysis, and verification."""

from __future__ import annotations

from typing import Any

from contracts.media import (
    AsrObservation,
    LocationVerificationStatus,
    MediaExtractionRequest,
    MediaExtractionStatus,
    VerifiedPlace,
)
from functions.media_analyzer.service import MediaAnalyzerService
from functions.media_analyzer.youtube import fetch_youtube_captions, youtube_video_id
from functions.media_extractor.download import download_direct_media
from functions.media_extractor.service import MediaExtractorService

from .state import MediaLocationState


async def extract_media_node(state: MediaLocationState) -> dict[str, Any]:
    """Download an allowlisted media file and extract timestamped frames/audio."""
    media_bytes, filename = await download_direct_media(state["url"])
    result = await MediaExtractorService().extract_media(
        media_bytes,
        filename_hint=filename,
        request=MediaExtractionRequest(
            source_url=state["url"],
            max_size_bytes=50 * 1024 * 1024,
        ),
    )
    if result.status != MediaExtractionStatus.SUCCESS:
        raise RuntimeError(
            result.error_message or f"Media extraction failed: {result.status.value}"
        )
    return {"extraction_result": result, "attempt_count": state.get("attempt_count", 0) + 1}


async def analyze_media_node(state: MediaLocationState) -> dict[str, Any]:
    """Analyze the extracted image frames and audio without fabricating clues."""
    extraction = state.get("extraction_result")
    caption_observation = state.get("caption_observation")
    if extraction is None and caption_observation is None:
        raise RuntimeError("Media extraction must succeed before analysis.")

    analyzed = await MediaAnalyzerService().analyze_media(
        frames=extraction.selected_frames if extraction else [],
        audio=extraction.audio if extraction else None,
        audio_bytes=(extraction.audio.audio_bytes if extraction and extraction.audio else None),
        caption_observation=caption_observation,
        job_id=str(extraction.job_id) if extraction else None,
    )
    return {"analyzed": analyzed}


async def verify_candidates_node(state: MediaLocationState) -> dict[str, Any]:
    """Resolve observed clues and return only independently corroborated places."""
    analyzed = state.get("analyzed")
    if analyzed is None:
        raise RuntimeError("Media analysis must complete before location verification.")
    graph = await MediaAnalyzerService().build_location_graph(analyzed)
    return {
        "location_graph": graph,
        "verified_places": [
            VerifiedPlace(
                primary_name=candidate.name,
                locality=candidate.locality or candidate.state or "India",
                latitude=candidate.latitude,
                longitude=candidate.longitude,
                confidence=candidate.confidence,
                verification_status=LocationVerificationStatus.VERIFIED,
                corroborating_evidence=candidate.matched_clues,
                candidates_reviewed=[candidate],
            )
            for candidate in graph.verified_locations
        ],
    }


async def process_media_node(state: MediaLocationState) -> dict[str, Any]:
    """Keep transient media bytes within one checkpointed graph node."""
    if youtube_video_id(state["url"]) is not None:
        caption_observation: AsrObservation | None = await fetch_youtube_captions(state["url"])
        extraction_result = None
        attempt_count = state.get("attempt_count", 0) + 1
    else:
        extraction = await extract_media_node(state)
        caption_observation = None
        extraction_result = extraction["extraction_result"]
        attempt_count = extraction["attempt_count"]

    next_state: MediaLocationState = {
        "url": state["url"],
        "extraction_result": extraction_result,
        "caption_observation": caption_observation,
        "analyzed": None,
        "location_graph": None,
        "verified_places": [],
        "attempt_count": attempt_count,
    }
    analysis = await analyze_media_node(next_state)
    next_state["analyzed"] = analysis["analyzed"]
    verification = await verify_candidates_node(next_state)
    next_state["location_graph"] = verification["location_graph"]
    next_state["verified_places"] = verification["verified_places"]
    return {
        "extraction_result": next_state["extraction_result"],
        "caption_observation": next_state["caption_observation"],
        "analyzed": next_state["analyzed"],
        "location_graph": next_state["location_graph"],
        "verified_places": next_state["verified_places"],
        "attempt_count": next_state["attempt_count"],
    }
