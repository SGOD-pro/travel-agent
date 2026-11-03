"""Worker handler for Media Analyzer in orchestrator execution engine.

Conforms to Phase 10:
- Parses extracted frames and audio from Phase 9.
- Runs OCR, ASR, and multimodal clue fusion.
- Returns a serializable MediaAnalyzed contract for downstream Phase 11 processing.
"""

from __future__ import annotations

import base64
import io
import logging
import uuid
from typing import Any

from PIL import Image

from contracts.media import ExtractedAudio, ExtractedFrame, MediaAnalyzed
from functions.media_analyzer.service import MediaAnalyzerService

logger = logging.getLogger(__name__)


async def analyze_media_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Executor registry handler for ("MEDIA_ANALYZER", "analyze").

    Accepts extracted frames, audio, and optional frame images, runs OCR/ASR/multimodal fusion,
    and returns a serializable MediaAnalyzed dict.
    """
    # 1. Extract and parse tracing IDs (job_id, correlation_id)
    job_id_raw = payload.get("job_id")
    active_job_id = str(uuid.UUID(job_id_raw)) if job_id_raw else str(uuid.uuid4())

    # 2. Parse ExtractedFrames from Phase 9 output
    raw_frames = payload.get("frames", [])
    frames: list[ExtractedFrame] = []
    for f_dict in raw_frames:
        try:
            frames.append(ExtractedFrame(**f_dict))
        except Exception as e:
            logger.warning("Failed to parse frame metadata dict: %s", e)

    # 3. Parse ExtractedAudio from Phase 9 output
    raw_audio = payload.get("audio")
    audio: ExtractedAudio | None = None
    if raw_audio:
        try:
            audio = ExtractedAudio(**raw_audio)
        except Exception as e:
            logger.warning("Failed to parse audio metadata dict: %s", e)

    # 4. Parse mock transcript (used for offline testing and benchmarking)
    mock_transcript = payload.get("mock_transcript")

    # 5. Parse frame images if provided (base64 encoded for direct transmission)
    # In production, the service loads these from file_path or s3_key automatically.
    frame_images: dict[int, Image.Image] = {}
    raw_images = payload.get("frame_images_b64", {})
    for idx_str, b64_str in raw_images.items():
        try:
            img_bytes = base64.b64decode(b64_str)
            frame_images[int(idx_str)] = Image.open(io.BytesIO(img_bytes))
        except Exception as e:
            logger.debug("Failed to load frame image %s: %s", idx_str, e)

    # 6. Execute Analyzer Service
    service = MediaAnalyzerService()
    result: MediaAnalyzed = await service.analyze_media(
        frames=frames,
        audio=audio,
        frame_images=frame_images if frame_images else None,
        mock_transcript=mock_transcript,
        job_id=active_job_id,
    )

    # 7. Return JSON-serializable dictionary for the orchestrator
    return result.model_dump(mode="json")