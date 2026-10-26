"""Pydantic domain contracts for Media Extraction, Analysis, and Location Verification.

Conforms to archive/phases.md (Phases 9, 10, 11) and archive/boundaries.md:
- Media extractor: frame/audio timestamps, blur/dedupe, size/duration failure caps.
- Media analyzer: OCR languages, asynchronous ASR, observation fusion, uncertainty.
- Media location: accessible/inaccessible/ambiguous/verified outcomes with candidate explanations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal  # <-- FIXED: Added Literal import

from pydantic import BaseModel, ConfigDict, Field


class MediaSourceType(StrEnum):
    PUBLIC_URL = "public_url"
    UPLOAD = "upload"


class MediaExtractionStatus(StrEnum):
    SUCCESS = "success"
    DURATION_EXCEEDED = "duration_exceeded"
    FILE_TOO_LARGE = "file_too_large"
    CORRUPT_MEDIA = "corrupt_media"
    FETCH_FAILED = "fetch_failed"
    UNSUPPORTED_FORMAT = "unsupported_format"


class LocationVerificationStatus(StrEnum):
    VERIFIED = "verified"
    AMBIGUOUS = "ambiguous"
    INACCESSIBLE = "inaccessible"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ExtractedFrame(BaseModel):
    """A sampled video frame or single image with quality and deduplication metrics."""

    model_config = ConfigDict(frozen=True)

    frame_index: int = Field(ge=0)
    timestamp_seconds: float = Field(ge=0.0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    image_hash: str = Field(description="Perceptual dHash hex string for deduplication")
    blur_score: float = Field(description="Laplacian/gradient variance; higher is sharper")
    brightness: float = Field(ge=0.0, le=255.0, description="Average pixel luminance")
    is_blurry: bool = False
    is_duplicate: bool = False
    file_path: str | None = None
    s3_key: str | None = None


class ExtractedAudio(BaseModel):
    """Audio track extracted from media container."""

    model_config = ConfigDict(frozen=True)

    duration_seconds: float = Field(ge=0.0)
    channels: int = Field(default=1, ge=1)
    sample_rate: int = Field(default=16000, ge=8000)
    audio_format: str = "wav"
    file_path: str | None = None
    s3_key: str | None = None


class MediaMetadata(BaseModel):
    """Basic format and stream metadata."""

    model_config = ConfigDict(frozen=True)

    format_name: str
    duration_seconds: float = Field(ge=0.0)
    size_bytes: int = Field(ge=0)
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_audio: bool = False
    has_video: bool = True


class MediaExtractionRequest(BaseModel):
    """Input request specification for media extraction."""

    model_config = ConfigDict(frozen=True)

    source_type: MediaSourceType = MediaSourceType.PUBLIC_URL
    source_url: str | None = None
    file_path: str | None = None
    max_duration_seconds: float = 180.0
    max_size_bytes: int = 50 * 1024 * 1024  # 50 MB
    sample_fps: float = 1.0  # 1 frame per second
    max_frames: int = 30
    blur_threshold: float = 80.0
    dedupe_hamming_threshold: int = 5


class MediaExtractionResult(BaseModel):
    """Output result of frame/audio extraction and scene filtering."""
    model_config = ConfigDict(frozen=True)

    job_id: uuid.UUID
    correlation_id: uuid.UUID
    status: MediaExtractionStatus
    metadata: MediaMetadata | None = None
    duration_seconds: float = 0.0
    total_frames_extracted: int = 0
    selected_frames: list[ExtractedFrame] = Field(default_factory=list)
    audio: ExtractedAudio | None = None
    runtime_ms: float = Field(default=0.0, description="Extraction execution duration in ms")
    error_message: str | None = None


class OcrWord(BaseModel):
    """Word or token detected by OCR."""

    model_config = ConfigDict(frozen=True)

    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    language: str = "en"
    bounding_box: list[float] = Field(default_factory=list)  # [ymin, xmin, ymax, xmax]


class OcrObservation(BaseModel):
    """Text observations extracted from a keyframe."""

    model_config = ConfigDict(frozen=True)

    frame_timestamp: float
    frame_index: int
    detected_texts: list[str] = Field(default_factory=list)
    words: list[OcrWord] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    mean_confidence: float = 1.0


class AsrSegment(BaseModel):
    """Spoken segment detected by ASR."""

    model_config = ConfigDict(frozen=True)

    start_time: float
    end_time: float
    transcript: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    language: str = "en-IN"


class AsrObservation(BaseModel):
    """Speech-to-text observations from extracted audio."""

    model_config = ConfigDict(frozen=True)

    job_id: str
    status: str = "COMPLETED"  # IN_PROGRESS, COMPLETED, FAILED
    full_transcript: str = ""
    segments: list[AsrSegment] = Field(default_factory=list)
    primary_language: str = "en-IN"
    overall_confidence: float = 1.0


class MediaClue(BaseModel):
    """A single piece of evidence extracted from a specific media timestamp."""
    model_config = ConfigDict(frozen=True)
    
    clue_type: Literal["visual", "audio", "text_ocr", "text_asr", "metadata"]
    source_frame_index: int | None = None
    source_timestamp_seconds: float | None = None
    text_content: str
    confidence: float = Field(ge=0.0, le=1.0)
    language: str | None = None


class FusedMediaClues(BaseModel):
    """Corroborated multimodal clues for location identification."""
    model_config = ConfigDict(frozen=True)
    
    clues: list[MediaClue] = Field(default_factory=list)
    signage_texts: list[str] = Field(default_factory=list)
    audio_keywords: list[str] = Field(default_factory=list)
    landmarks_identified: list[str] = Field(default_factory=list)
    script_languages: list[str] = Field(default_factory=list)
    temporal_context: str | None = None


class UncertaintyFlags(BaseModel):
    """Confidence and quality flags for the analyzed media."""
    model_config = ConfigDict(frozen=True)
    
    asr_low_confidence: bool = False
    ocr_sparse: bool = False
    visual_ambiguous: bool = False
    duration_mismatch: bool = False


class MediaAnalyzed(BaseModel):
    """Complete output of the Media Analyzer (Phase 10)."""
    model_config = ConfigDict(frozen=True)
    
    job_id: str
    status: str = "COMPLETED"
    asr_segments: list[AsrSegment] = Field(default_factory=list)
    full_transcript: str = ""
    primary_language: str = "en-IN"
    overall_confidence: float = 1.0
    ocr_results: list[OcrObservation] = Field(default_factory=list)
    fused_clues: FusedMediaClues = Field(default_factory=FusedMediaClues)
    uncertainty: UncertaintyFlags = Field(default_factory=UncertaintyFlags)
    processing_time_ms: int = 0
    models_used: dict[str, str] = Field(default_factory=dict)


class LocationCandidate(BaseModel):
    """A plausible geographical location candidate with human-auditable explanation."""
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    locality: str | None = None
    state: str | None = None
    country: str = "India"
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    confidence: float = Field(ge=0.0, le=1.0)
    
    match_type: Literal["visual", "asr_text", "ocr_text", "metadata", "fused"] = "fused"
    
    matched_clues: list[str] = Field(default_factory=list)
    
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Detailed breakdown of scores (e.g. CLIP similarity, ASR text match, geospatial distance)"
    )
    
    explanation: str = Field(description="Transparent reasoning explaining why this place was proposed")


class NearbyExperiencePreview(BaseModel):
    """Preview of nearby points of interest for verified places."""

    model_config = ConfigDict(frozen=True)

    name: str
    category: str
    distance_km: float
    description: str | None = None


class VerifiedPlace(BaseModel):
    """The authoritative location resolution output."""
    model_config = ConfigDict(frozen=True)

    place_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    primary_name: str
    locality: str
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    confidence: float = Field(ge=0.0, le=1.0)
    verification_status: LocationVerificationStatus
    corroborating_evidence: list[str] = Field(default_factory=list)
    candidates_reviewed: list[LocationCandidate] = Field(default_factory=list)
    nearby_experiences: list[NearbyExperiencePreview] = Field(default_factory=list)
    verified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    
    public_media_urls: dict[str, str] = Field(
        default_factory=dict,
        description="Presigned S3 URLs for keyframes/audio clips that corroborate this location"
    )


class MediaLocationRequest(BaseModel):
    """API input payload for Media Location inspection."""

    model_config = ConfigDict(frozen=True)

    url: str | None = None
    hints: list[str] = Field(default_factory=list)
    preferred_region: str | None = None


class MediaLocationResponse(BaseModel):
    """API response payload for Media Location resolution."""

    model_config = ConfigDict(frozen=True)

    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    status: LocationVerificationStatus
    verified_place: VerifiedPlace | None = None
    ranked_candidates: list[LocationCandidate] = Field(default_factory=list)
    fused_clues: FusedMediaClues | None = None
    audit_notes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MediaLocationGraph(BaseModel):
    """API response payload for Media Location resolution (Phase 11)."""
    model_config = ConfigDict(frozen=True)
    
    job_id: str
    analysis_job_id: str
    candidates: list[LocationCandidate] = Field(default_factory=list)
    verified_locations: list[LocationCandidate] = Field(default_factory=list)
    ambiguous_locations: list[LocationCandidate] = Field(default_factory=list)
    inaccessible_locations: list[LocationCandidate] = Field(default_factory=list)
    public_media_urls: dict[str, str] = Field(default_factory=dict)