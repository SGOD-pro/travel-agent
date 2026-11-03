"""Media Extractor service for frame sampling, audio extraction, and quality filtering.

Conforms to Phase 9:
- Validates size and duration failure caps.
- Measures runtime execution in milliseconds.
- Non-LLM deduplication and blur filtering.
- Supports both static images and audio/video containers.
- Propagates Orchestrator tracing IDs (job_id, correlation_id) for Phase 11 compliance.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from contracts.media import (
    ExtractedAudio,
    ExtractedFrame,
    MediaExtractionRequest,
    MediaExtractionResult,
    MediaExtractionStatus,
    MediaMetadata,
)
from functions.media_extractor.audio import inspect_wav_audio
from functions.media_extractor.frames import filter_and_deduplicate_frames, process_image_frame

logger = logging.getLogger(__name__)


class MediaExtractorService:
    """Service handling bounded media extraction, FFmpeg execution, and quality filtering."""

    def __init__(self, ffmpeg_bin: str | None = None, ffprobe_bin: str | None = None) -> None:
        self._ffmpeg = ffmpeg_bin or shutil.which("ffmpeg")
        self._ffprobe = ffprobe_bin or shutil.which("ffprobe")

    @property
    def has_ffmpeg(self) -> bool:
        return self._ffmpeg is not None

    async def extract_media(
        self,
        media_bytes: bytes,
        filename_hint: str = "media.jpg",
        request: MediaExtractionRequest | None = None,
        job_id: uuid.UUID | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> MediaExtractionResult:
        """Processes raw media bytes into quality-filtered frames and extracted audio."""
        req = request or MediaExtractionRequest()
        start_time = time.perf_counter()
        size_bytes = len(media_bytes)

        # Ensure we have tracing IDs; generate fallbacks if running in isolation
        active_job_id = job_id or uuid.uuid4()
        active_correlation_id = correlation_id or uuid.uuid4()

        # 1. Size cap check (fail-closed)
        if size_bytes > req.max_size_bytes:
            runtime_ms = (time.perf_counter() - start_time) * 1000.0
            return MediaExtractionResult(
                media_id=active_job_id,
                correlation_id=active_correlation_id,
                status=MediaExtractionStatus.FILE_TOO_LARGE,
                error_message=f"Media size {size_bytes} bytes exceeds ceiling of {req.max_size_bytes} bytes",
                runtime_ms=round(runtime_ms, 2),
            )

        if size_bytes == 0:
            runtime_ms = (time.perf_counter() - start_time) * 1000.0
            return MediaExtractionResult(
                media_id=active_job_id,
                correlation_id=active_correlation_id,
                status=MediaExtractionStatus.CORRUPT_MEDIA,
                error_message="Received empty 0-byte media payload",
                runtime_ms=round(runtime_ms, 2),
            )

        # 2. Check if still image (JPEG, PNG, WebP)
        is_image = False
        try:
            with Image.open(io.BytesIO(media_bytes)) as test_img:
                test_img.verify()
                is_image = True
        except (UnidentifiedImageError, Exception):
            is_image = False

        if is_image:
            return self._extract_from_still_image(
                media_bytes,
                filename_hint,
                req,
                start_time,
                active_job_id,
                active_correlation_id,
            )

        # 3. Handle video/audio container via FFmpeg or fallback
        return await self._extract_from_container(
            media_bytes,
            filename_hint,
            req,
            start_time,
            active_job_id,
            active_correlation_id,
        )

    def _extract_from_still_image(
        self,
        media_bytes: bytes,
        filename_hint: str,
        request: MediaExtractionRequest,
        start_time: float,
        job_id: uuid.UUID,
        correlation_id: uuid.UUID,
    ) -> MediaExtractionResult:
        """Extracts and evaluates a single still image."""
        try:
            with Image.open(io.BytesIO(media_bytes)) as img:
                img_format = img.format or "JPEG"
                width, height = img.size
                frame = process_image_frame(
                    img,
                    timestamp_seconds=0.0,
                    frame_index=0,
                    blur_threshold=request.blur_threshold,
                )

                meta = MediaMetadata(
                    format_name=img_format,
                    duration_seconds=0.0,
                    size_bytes=len(media_bytes),
                    width=width,
                    height=height,
                    fps=0.0,
                    has_audio=False,
                    has_video=False,
                )

                runtime_ms = (time.perf_counter() - start_time) * 1000.0
                return MediaExtractionResult(
                    media_id=job_id,
                    correlation_id=correlation_id,
                    status=MediaExtractionStatus.SUCCESS,
                    metadata=meta,
                    duration_seconds=0.0,
                    total_frames_extracted=1,
                    selected_frames=[frame],
                    audio=None,
                    runtime_ms=round(runtime_ms, 2),
                )
        except Exception as e:
            runtime_ms = (time.perf_counter() - start_time) * 1000.0
            return MediaExtractionResult(
                media_id=job_id,
                correlation_id=correlation_id,
                status=MediaExtractionStatus.CORRUPT_MEDIA,
                error_message=f"Failed parsing image payload: {e!s}",
                runtime_ms=round(runtime_ms, 2),
            )

    async def _extract_from_container(
        self,
        media_bytes: bytes,
        filename_hint: str,
        request: MediaExtractionRequest,
        start_time: float,
        job_id: uuid.UUID,
        correlation_id: uuid.UUID,
    ) -> MediaExtractionResult:
        """Extracts frames and audio from video/audio container with duration caps."""
        # Check if it's raw WAV audio
        wav_audio = inspect_wav_audio(media_bytes)
        if wav_audio is not None:
            if wav_audio.duration_seconds > request.max_duration_seconds:
                runtime_ms = (time.perf_counter() - start_time) * 1000.0
                return MediaExtractionResult(
                    media_id=job_id,
                    correlation_id=correlation_id,
                    status=MediaExtractionStatus.DURATION_EXCEEDED,
                    duration_seconds=wav_audio.duration_seconds,
                    error_message=f"Audio duration {wav_audio.duration_seconds}s exceeds limit {request.max_duration_seconds}s",
                    runtime_ms=round(runtime_ms, 2),
                )

            meta = MediaMetadata(
                format_name="WAV",
                duration_seconds=wav_audio.duration_seconds,
                size_bytes=len(media_bytes),
                has_audio=True,
                has_video=False,
            )
            runtime_ms = (time.perf_counter() - start_time) * 1000.0
            return MediaExtractionResult(
                media_id=job_id,
                correlation_id=correlation_id,
                status=MediaExtractionStatus.SUCCESS,
                metadata=meta,
                duration_seconds=wav_audio.duration_seconds,
                total_frames_extracted=0,
                selected_frames=[],
                audio=wav_audio,
                runtime_ms=round(runtime_ms, 2),
            )

        # If FFmpeg is available on the machine, run async FFmpeg subprocess
        if self._ffmpeg:
            return await self._run_ffmpeg_extraction(
                media_bytes,
                filename_hint,
                request,
                start_time,
                job_id,
                correlation_id,
            )

        # FFmpeg not present: Return unsupported format or corrupt media gracefully
        runtime_ms = (time.perf_counter() - start_time) * 1000.0
        return MediaExtractionResult(
            media_id=job_id,
            correlation_id=correlation_id,
            status=MediaExtractionStatus.UNSUPPORTED_FORMAT,
            error_message="FFmpeg binary not detected in runtime environment for video container parsing",
            runtime_ms=round(runtime_ms, 2),
        )

    async def _run_ffmpeg_extraction(
        self,
        media_bytes: bytes,
        filename_hint: str,
        request: MediaExtractionRequest,
        start_time: float,
        job_id: uuid.UUID,
        correlation_id: uuid.UUID,
    ) -> MediaExtractionResult:
        """Runs bounded FFmpeg subprocess to extract frames and audio."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            in_file = tmp_path / f"input_{uuid.uuid4().hex[:8]}_{filename_hint}"
            in_file.write_bytes(media_bytes)

            frames_dir = tmp_path / "frames"
            frames_dir.mkdir(exist_ok=True)
            out_audio = tmp_path / "audio.wav"

            # Sample frames at sample_fps
            fps = request.sample_fps
            frame_pattern = str(frames_dir / "frame_%04d.jpg")

            cmd_frames = [
                self._ffmpeg or "ffmpeg",
                "-y",
                "-i",
                str(in_file),
                "-vf",
                f"fps={fps}",
                "-vframes",
                str(request.max_frames),
                frame_pattern,
            ]

            cmd_audio = [
                self._ffmpeg or "ffmpeg",
                "-y",
                "-i",
                str(in_file),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(out_audio),
            ]

            try:
                # Run frame and audio extraction concurrently
                proc_frames = await asyncio.create_subprocess_exec(
                    *cmd_frames,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                proc_audio = await asyncio.create_subprocess_exec(
                    *cmd_audio,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                await proc_frames.communicate()
                await proc_audio.communicate()

                # Process all extracted frame files
                frame_files = sorted(frames_dir.glob("frame_*.jpg"))
                extracted_frames: list[ExtractedFrame] = []

                for idx, f_path in enumerate(frame_files):
                    timestamp = float(idx) / float(fps)
                    with Image.open(f_path) as f_img:
                        frame = process_image_frame(
                            f_img,
                            timestamp_seconds=timestamp,
                            frame_index=idx,
                            blur_threshold=request.blur_threshold,
                        )
                        extracted_frames.append(frame)

                # Deduplicate and filter frames
                filtered_frames = filter_and_deduplicate_frames(
                    extracted_frames,
                    dedupe_hamming_threshold=request.dedupe_hamming_threshold,
                )

                # Read audio if present
                audio_res: ExtractedAudio | None = None
                if out_audio.exists() and out_audio.stat().st_size > 44:
                    audio_res = inspect_wav_audio(out_audio.read_bytes())

                duration_est = len(frame_files) / fps if frame_files else (audio_res.duration_seconds if audio_res else 0.0)

                # Duration cap check
                if duration_est > request.max_duration_seconds:
                    runtime_ms = (time.perf_counter() - start_time) * 1000.0
                    return MediaExtractionResult(
                        media_id=job_id,
                        correlation_id=correlation_id,
                        status=MediaExtractionStatus.DURATION_EXCEEDED,
                        duration_seconds=duration_est,
                        error_message=f"Extracted media duration {duration_est}s exceeds ceiling {request.max_duration_seconds}s",
                        runtime_ms=round(runtime_ms, 2),
                    )

                meta = MediaMetadata(
                    format_name="VIDEO_CONTAINER",
                    duration_seconds=duration_est,
                    size_bytes=len(media_bytes),
                    has_audio=audio_res is not None,
                    has_video=len(frame_files) > 0,
                )

                runtime_ms = (time.perf_counter() - start_time) * 1000.0
                return MediaExtractionResult(
                    media_id=job_id,
                    correlation_id=correlation_id,
                    status=MediaExtractionStatus.SUCCESS,
                    metadata=meta,
                    duration_seconds=duration_est,
                    total_frames_extracted=len(frame_files),
                    selected_frames=filtered_frames,
                    audio=audio_res,
                    runtime_ms=round(runtime_ms, 2),
                )
            except Exception as e:
                runtime_ms = (time.perf_counter() - start_time) * 1000.0
                return MediaExtractionResult(
                    media_id=job_id,
                    correlation_id=correlation_id,
                    status=MediaExtractionStatus.CORRUPT_MEDIA,
                    error_message=f"FFmpeg extraction failed: {e!s}",
                    runtime_ms=round(runtime_ms, 2),
                )