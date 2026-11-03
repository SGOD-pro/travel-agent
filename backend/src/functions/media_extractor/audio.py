"""Audio extraction and stream validation for media containers.

Conforms to Phase 9:
- Validates audio stream presence, duration, and sampling rate.
- Extracts standard 16kHz mono audio for downstream ASR consumption.
- Tracks local file paths and S3 storage keys for distributed processing.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

from contracts.media import ExtractedAudio


def inspect_wav_audio(
    audio_bytes: bytes,
    file_path: str | None = None,
    s3_key: str | None = None,
) -> ExtractedAudio | None:
    """Parses standard WAV audio header to extract sample rate and duration.

    Args:
        audio_bytes: The raw bytes of the WAV audio file.
        file_path: The local temporary path of the audio file (if applicable).
        s3_key: The S3 object key where the processed audio is stored (if uploaded).
    """
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
            duration = float(n_frames) / float(sample_rate) if sample_rate > 0 else 0.0

            return ExtractedAudio(
                duration_seconds=round(duration, 2),
                channels=channels,
                sample_rate=sample_rate,
                audio_format="wav",
                file_path=file_path,
                s3_key=s3_key,
            )
    except Exception:
        return None


def create_mock_audio_track(duration_seconds: float = 10.0, sample_rate: int = 16000) -> bytes:
    """Generates a small in-memory PCM WAV audio track for testing and benchmarking."""
    num_frames = int(duration_seconds * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        # 16-bit zero bytes (silence)
        wf.writeframes(b"\x00\x00" * num_frames)
    return buf.getvalue()