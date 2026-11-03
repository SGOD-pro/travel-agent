# backend/src/functions/api/routes/v1/media.py
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from contracts.media import LocationCandidate, MediaLocationGraph
from functions.media_analyzer.service import MediaAnalyzerService

router = APIRouter(prefix="/api/v1/media", tags=["Media Location Graph"])


@router.get("/location/{job_id}", response_model=MediaLocationGraph)
async def get_media_location_graph(job_id: str) -> MediaLocationGraph:
    """Retrieves the computed location graph, including all ranked candidates and explanations."""
    # In production, fetch the analyzed job from PostgreSQL/Redis
    # Mocking a dummy analyzed payload to demonstrate the graph output
    service = MediaAnalyzerService()
    
    # Return a dummy MediaLocationGraph matching the contract schema
    return MediaLocationGraph(
        job_id=job_id,
        analysis_job_id=job_id,
        candidates=[],
        verified_locations=[],
        ambiguous_locations=[],
        inaccessible_locations=[],
        public_media_urls={}
    )


@router.get("/candidates/{job_id}/explain", response_model=list[LocationCandidate])
async def explain_candidates(job_id: str):
    """
    Phase 11 Gate Endpoint:
    Returns ALL candidate matches with full evidence breakdowns,
    allowing manual review of explanations rather than just the top-1 guess.
    """
    # Fetch candidates from DB
    pass