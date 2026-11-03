"""Public YouTube captions retrieval without downloading video content."""

from __future__ import annotations

import asyncio
import re
import uuid
from urllib.parse import parse_qs, urlsplit

from youtube_transcript_api import (
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)

from contracts.media import AsrObservation, AsrSegment

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_CAPTION_CHARS = 50_000
MAX_CAPTION_SEGMENTS = 3_000


def youtube_video_id(url: str) -> str | None:
    """Return a valid video ID for supported YouTube URLs, or None for other hosts."""
    parsed = urlsplit(url)
    if not parsed.hostname or parsed.hostname.lower().rstrip(".") not in YOUTUBE_HOSTS:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 80, 443)
    ):
        raise ValueError("YouTube URL must be a public HTTP(S) URL without credentials.")

    host = parsed.hostname.lower().rstrip(".")
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        path_parts = parsed.path.strip("/").split("/")
        video_id = (
            path_parts[1]
            if len(path_parts) == 2
            and path_parts[0]
            in {
                "embed",
                "live",
                "shorts",
            }
            else ""
        )

    if not VIDEO_ID_PATTERN.fullmatch(video_id):
        raise ValueError("Enter a valid YouTube video URL.")
    return video_id


async def fetch_youtube_captions(url: str) -> AsrObservation:
    """Fetch bounded public captions and map them to timestamped text observations."""
    video_id = youtube_video_id(url)
    if video_id is None:
        raise ValueError("A supported YouTube video URL is required.")

    api = YouTubeTranscriptApi()
    try:
        transcript_list = await asyncio.to_thread(api.list, video_id)
        transcripts = list(transcript_list)
        if not transcripts:
            raise ValueError("This YouTube video has no publicly accessible captions.")

        transcript = next(
            (item for item in transcripts if item.language_code.casefold().startswith("en")),
            transcripts[0],
        )
        fetched = await asyncio.to_thread(transcript.fetch)
    except (TranscriptsDisabled, NoTranscriptFound):
        raise ValueError("This YouTube video has no publicly accessible captions.") from None
    except (VideoUnavailable, RequestBlocked, IpBlocked):
        raise RuntimeError(
            "YouTube captions could not be accessed from this server. Try again later or enter destinations manually."
        ) from None

    segments: list[AsrSegment] = []
    collected_text: list[str] = []
    total_chars = 0
    for snippet in fetched:
        if len(segments) >= MAX_CAPTION_SEGMENTS or total_chars >= MAX_CAPTION_CHARS:
            break
        text = " ".join(snippet.text.split())
        if not text:
            continue
        remaining_chars = MAX_CAPTION_CHARS - total_chars
        text = text[:remaining_chars]
        start_time = max(0.0, float(snippet.start))
        duration = max(0.0, float(snippet.duration))
        segments.append(
            AsrSegment(
                start_time=start_time,
                end_time=start_time + duration,
                transcript=text,
                confidence=0.75,
                language=transcript.language_code,
            )
        )
        collected_text.append(text)
        total_chars += len(text)

    if not segments:
        raise ValueError("This YouTube video has no readable public captions.")

    return AsrObservation(
        job_id=f"youtube-captions-{uuid.uuid4()}",
        full_transcript=" ".join(collected_text),
        segments=segments,
        primary_language=transcript.language_code,
        overall_confidence=0.75,
    )
