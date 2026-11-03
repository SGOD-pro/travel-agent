"""Media Analyzer service fusing OCR observations, ASR audio clues, and scene metadata.

Conforms to Phase 10:
- Evaluates OCR script languages (English, Hindi, Kannada, Tamil, etc.).
- Integrates asynchronous ASR speech-to-text transcripts.
- Fuses multi-modal observations into structured location clues (MediaClue).
- Returns a complete MediaAnalyzed contract for downstream Phase 11 processing.
"""

from __future__ import annotations

import io
import logging
import re
import time
import uuid
from typing import Any

from PIL import Image

from contracts.media import (
    AsrObservation,
    ExtractedAudio,
    ExtractedFrame,
    FusedMediaClues,
    MediaAnalyzed,
    MediaClue,
    MediaLocationGraph,  # <-- FIXED: Added missing import
    OcrObservation,
    UncertaintyFlags,
)
from functions.media_analyzer.ocr import OcrEngine, map_ocr_to_clues
from functions.media_analyzer.scene_filter import select_informative_keyframes
from functions.media_analyzer.transcription import AsrClient, map_asr_to_clues
from functions.media_analyzer.graph import LocationGraphBuilder  # <-- FIXED: Moved local import down

logger = logging.getLogger(__name__)

# Heuristic patterns for common Indian geographical and landmark tokens
LANDMARK_PATTERNS = [
    re.compile(r"\b(?:temple|mandir|masjid|church|cathedral|gurudwara)\b", re.IGNORECASE),
    re.compile(r"\b(?:fort|palace|mahal|qila|haveli)\b", re.IGNORECASE),
    re.compile(r"\b(?:falls|waterfalls|lake|river|ghat|valley|peak|pass)\b", re.IGNORECASE),
    re.compile(r"\b(?:sanctuary|national park|tiger reserve|zoo|beach)\b", re.IGNORECASE),
]

INDIAN_GEOGRAPHIC_TOKENS = {
    "delhi", "new delhi", "agra", "jaipur", "jodhpur", "udaipur", "pushkar", "jaisalmer",
    "bengaluru", "bangalore", "mysuru", "mysore", "coorg", "madikeri", "wayanad", "ooty",
    "munnar", "kochi", "alleppey", "thekkady", "goa", "alibaug", "ratnagiri", "mumbai",
    "pune", "varanasi", "kashi", "prayagraj", "allahabad", "ayodhya", "lucknow", "rishikesh",
    "haridwar", "shimla", "manali", "kasol", "dharamshala", "srinagar", "gulmarg", "pahalgam",
    "leh", "ladakh", "kolkata", "darjeeling", "gangtok", "shillong", "guwahati", "puri",
    "hampi", "gokarna", "chennai", "madurai", "mahabalipuram", "pondicherry", "hyderabad",
}


class MediaAnalyzerService:
    """Service that orchestrates OCR, ASR, and multimodal clue fusion."""

    def __init__(
        self,
        ocr_engine: OcrEngine | None = None,
        asr_client: AsrClient | None = None,
    ) -> None:
        self.ocr_engine = ocr_engine or OcrEngine()
        self.asr_client = asr_client or AsrClient()

    async def analyze_media(
        self,
        frames: list[ExtractedFrame],
        audio: ExtractedAudio | None = None,
        frame_images: dict[int, Image.Image] | None = None,
        audio_bytes: bytes | None = None,
        mock_transcript: str | None = None,
        job_id: str | None = None,
    ) -> MediaAnalyzed:
        """Runs scene filtering, OCR, ASR, and fuses clues into a unified MediaAnalyzed contract."""
        start_time = time.perf_counter()
        frame_map = frame_images or {}
        active_job_id = job_id or str(uuid.uuid4())

        # 1. Select top informative keyframes
        keyframes = select_informative_keyframes(frames, max_keyframes=8)

        # 2. Run OCR across keyframes
        ocr_observations: list[OcrObservation] = []
        for kf in keyframes:
            img = frame_map.get(kf.frame_index)
            if img is None and kf.file_path:
                try:
                    img = Image.open(kf.file_path)
                except Exception as e:
                    logger.debug("Failed loading frame image from %s: %s", kf.file_path, e)
            if img is None:
                # Synthetic dummy image for metadata-driven OCR
                img = Image.new("RGB", (kf.width, kf.height), color=(200, 200, 200))

            obs = self.ocr_engine.extract_text(img, kf)
            if obs.detected_texts:
                ocr_observations.append(obs)

        # 3. Run ASR on audio if present
        asr_observation: AsrObservation | None = None
        if audio is not None or audio_bytes is not None or mock_transcript is not None:
            job_id = await self.asr_client.start_transcription_job(
                media_uri="in_memory_audio",
                mock_transcript=mock_transcript,
            )
            asr_observation = await self.asr_client.get_transcription_job(job_id)

        # 4. Multimodal Fusion: Map raw observations to structured MediaClue objects
        fused_clues = self._fuse_observations(ocr_observations, asr_observation)

        # 5. Compute Uncertainty Flags
        uncertainty = self._compute_uncertainty(ocr_observations, asr_observation)

        runtime_ms = int((time.perf_counter() - start_time) * 1000)

        return MediaAnalyzed(
            job_id=active_job_id,
            status="COMPLETED",
            asr_segments=asr_observation.segments if asr_observation else [],
            full_transcript=asr_observation.full_transcript if asr_observation else "",
            primary_language=asr_observation.primary_language if asr_observation else "en-IN",
            overall_confidence=asr_observation.overall_confidence if asr_observation else 1.0,
            ocr_results=ocr_observations,
            fused_clues=fused_clues,
            uncertainty=uncertainty,
            processing_time_ms=runtime_ms,
            models_used={
                "ocr": "paddleocr-mock",
                "asr": "whisper-mock",
            },
        )

    def build_location_graph(self, analyzed: MediaAnalyzed) -> MediaLocationGraph:
        """Runs the geospatial fusion engine on the analyzed media clues."""
        builder = LocationGraphBuilder()
        return builder.build_graph(analyzed)

    def _fuse_observations(
        self,
        ocr_obs: list[OcrObservation],
        asr_obs: AsrObservation | None,
    ) -> FusedMediaClues:
        """Extracts and normalizes geographic names, signage, and landmarks from OCR & ASR."""
        signage_texts: list[str] = []
        audio_keywords: list[str] = []
        landmarks: list[str] = []
        languages: set[str] = set()
        clues: list[MediaClue] = []

        # Harvest OCR clues and map them to structured MediaClue objects
        for obs in ocr_obs:
            for lang in obs.languages:
                languages.add(lang)
            for text in obs.detected_texts:
                clean_t = text.strip()
                if clean_t and clean_t not in signage_texts:
                    signage_texts.append(clean_t)
                self._check_for_landmarks_and_geo(clean_t, landmarks)

            # Map OCR observation to structured clues
            clues.extend(map_ocr_to_clues(obs))

        # Harvest ASR audio clues and map them to structured MediaClue objects
        if asr_obs and asr_obs.full_transcript:
            clean_transcript = asr_obs.full_transcript.strip()
            self._check_for_landmarks_and_geo(clean_transcript, landmarks)

            words = clean_transcript.split()
            for w in words:
                cleaned_word = re.sub(r"[^\w\s]", "", w).strip().lower()
                if cleaned_word in INDIAN_GEOGRAPHIC_TOKENS:
                    if cleaned_word.capitalize() not in audio_keywords:
                        audio_keywords.append(cleaned_word.capitalize())

            # Map ASR observation to structured clues
            clues.extend(map_asr_to_clues(asr_obs))

        return FusedMediaClues(
            clues=clues,
            signage_texts=signage_texts,
            audio_keywords=audio_keywords,
            landmarks_identified=list(dict.fromkeys(landmarks)),
            script_languages=sorted(list(languages)),
            temporal_context=None,
        )

    def _compute_uncertainty(
        self,
        ocr_obs: list[OcrObservation],
        asr_obs: AsrObservation | None,
    ) -> UncertaintyFlags:
        """Evaluates confidence thresholds to flag ambiguous or sparse observations."""
        flags = UncertaintyFlags()
        
        if not ocr_obs:
            flags.ocr_sparse = True
        if not asr_obs or not asr_obs.full_transcript:
            flags.asr_low_confidence = True
            
        return flags

    def _check_for_landmarks_and_geo(self, text: str, landmarks: list[str]) -> None:
        """Helper to scan text for known geographic tokens and landmark keywords."""
        lower = text.lower()
        for token in INDIAN_GEOGRAPHIC_TOKENS:
            pattern = re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)
            if pattern.search(lower):
                capitalized = " ".join(part.capitalize() for part in token.split())
                if capitalized not in landmarks:
                    landmarks.append(capitalized)

        for pat in LANDMARK_PATTERNS:
            matches = pat.findall(lower)
            for m in matches:
                cap_m = m.capitalize()
                if cap_m not in landmarks:
                    landmarks.append(cap_m)