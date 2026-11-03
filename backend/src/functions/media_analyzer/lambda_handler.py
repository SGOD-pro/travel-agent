"""AWS Lambda entrypoint for the real Phase 10 media analyzer."""

from __future__ import annotations

import asyncio
from typing import Any

from functions.media_analyzer.handler import analyze_media_handler


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Run OCR, Transcribe, and multimodal fusion for a JSON event."""
    del context
    return asyncio.run(analyze_media_handler(event))
