"""Worker handler for Media Extractor in orchestrator execution engine."""

from __future__ import annotations

import base64
import uuid
from typing import Any

from contracts.media import MediaExtractionRequest
from functions.media_extractor.service import MediaExtractorService


async def extract_media_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Executor registry handler for ("MEDIA_EXTRACTOR", "extract").

    Accepts base64-encoded media_bytes or file_path, processes frames & audio,
    and returns serializable MediaExtractionResult dict containing tracing IDs.
    """
    # 1. Extract and parse tracing IDs from the orchestrator payload
    # Fallback to uuid.uuid4() if running in isolation/local unit tests
    job_id_raw = payload.get("job_id")
    correlation_id_raw = payload.get("correlation_id")

    job_id = uuid.UUID(job_id_raw) if job_id_raw else uuid.uuid4()
    correlation_id = uuid.UUID(correlation_id_raw) if correlation_id_raw else uuid.uuid4()

    # 2. Resolve raw media bytes
    raw_data = payload.get("media_bytes")
    if isinstance(raw_data, str):
        media_bytes = base64.b64decode(raw_data)
    elif isinstance(raw_data, bytes):
        media_bytes = raw_data
    elif "file_path" in payload:
        with open(payload["file_path"], "rb") as f:
            media_bytes = f.read()
    else:
        raise ValueError("Payload missing 'media_bytes' or 'file_path'")

    # 3. Initialize extraction request contract
    filename = payload.get("filename", "media.jpg")
    req_dict = payload.get("request", {})
    req = MediaExtractionRequest(**req_dict) if req_dict else None

    # 4. Execute extraction service with tracing context
    service = MediaExtractorService()
    result = await service.extract_media(
        media_bytes,
        filename_hint=filename,
        request=req,
        job_id=job_id,
        correlation_id=correlation_id,
    )

    # 5. Dump to JSON-serializable dictionary (includes media_id and correlation_id)
    return result.model_dump(mode="json")