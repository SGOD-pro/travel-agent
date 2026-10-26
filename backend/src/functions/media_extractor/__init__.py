"""Media Extractor module conforming to Phase 9."""

from functions.media_extractor.audio import inspect_wav_audio
from functions.media_extractor.frames import (
    compute_blur_score,
    compute_brightness,
    compute_dhash,
    filter_and_deduplicate_frames,
    hamming_distance,
    process_image_frame,
)
from functions.media_extractor.handler import extract_media_handler
from functions.media_extractor.service import MediaExtractorService

__all__ = [
    "MediaExtractorService",
    "compute_blur_score",
    "compute_brightness",
    "compute_dhash",
    "extract_media_handler",
    "filter_and_deduplicate_frames",
    "hamming_distance",
    "inspect_wav_audio",
    "process_image_frame",
]
