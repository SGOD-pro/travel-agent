from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from PIL import Image

from config.config import Settings
from contracts.media import (
    AsrObservation,
    AsrSegment,
    ExtractedFrame,
    FusedMediaClues,
    MediaAnalyzed,
    MediaClue,
    MediaExtractionStatus,
)
from functions.media_analyzer.aws import AwsMediaAnalyzer
from functions.media_analyzer.graph import LocationGraphBuilder
from functions.media_analyzer.service import MediaAnalyzerService
from functions.media_analyzer.youtube import fetch_youtube_captions, youtube_video_id
from functions.media_extractor.download import download_direct_media
from functions.media_extractor.service import MediaExtractorService
from functions.optimizer.routing import get_route_geometry, get_transit_matrix


@pytest.mark.asyncio
async def test_direct_media_download_uses_allowlist_and_sanitizes_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.media_extractor.download as downloader

    monkeypatch.setattr(
        downloader,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            MEDIA_ALLOWED_HOSTS=["media.example"],
        ),
    )

    async def resolve_public_host(_: str) -> None:
        return None

    monkeypatch.setattr(downloader, "_resolve_public_host", resolve_public_host)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "image/png"}, content=b"image")
    )

    data, filename = await download_direct_media(
        "https://media.example/files/%2F..%2Fimage.png",
        transport=transport,
    )

    assert data == b"image"
    assert filename == "image.png"


@pytest.mark.asyncio
async def test_direct_media_download_fails_closed_without_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.media_extractor.download as downloader

    monkeypatch.setattr(
        downloader,
        "get_settings",
        lambda: Settings(_env_file=None, MEDIA_ALLOWED_HOSTS=[]),
    )
    with pytest.raises(RuntimeError, match="MEDIA_ALLOWED_HOSTS"):
        await download_direct_media("https://media.example/image.png")


@pytest.mark.asyncio
async def test_direct_media_download_rejects_private_dns_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.media_extractor.download as downloader

    monkeypatch.setattr(
        downloader.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(ValueError, match="non-public"):
        await downloader._resolve_public_host("media.example")


@pytest.mark.asyncio
async def test_direct_media_download_rejects_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.media_extractor.download as downloader

    monkeypatch.setattr(
        downloader,
        "get_settings",
        lambda: Settings(_env_file=None, MEDIA_ALLOWED_HOSTS=["media.example"]),
    )

    async def resolve_public_host(_: str) -> None:
        return None

    monkeypatch.setattr(downloader, "_resolve_public_host", resolve_public_host)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(302, headers={"location": "https://other.example/file"})
    )
    with pytest.raises(ValueError, match="redirects"):
        await download_direct_media("https://media.example/image.png", transport=transport)


@pytest.mark.asyncio
async def test_google_route_matrix_maps_provider_durations_and_distances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.optimizer.routing as routing

    monkeypatch.setattr(
        routing,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            GOOGLE_MAPS_API_KEY="test-key",
        ),
    )
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "POST"
        assert request.headers["X-Goog-Api-Key"] == "test-key"
        return httpx.Response(
            200,
            text="\n".join(
                [
                    '{"originIndex":0,"destinationIndex":0,"condition":"ROUTE_EXISTS","duration":"0s","distanceMeters":0}',
                    '{"originIndex":0,"destinationIndex":1,"condition":"ROUTE_EXISTS","duration":"3600s","distanceMeters":120500}',
                    '{"originIndex":1,"destinationIndex":0,"condition":"ROUTE_EXISTS","duration":"1800s","distanceMeters":90200}',
                    '{"originIndex":1,"destinationIndex":1,"condition":"ROUTE_EXISTS","duration":"0s","distanceMeters":0}',
                ]
            ),
        )

    time_matrix, distance_matrix = await get_transit_matrix(
        [(12.0, 77.0), (13.0, 78.0)],
        transport=httpx.MockTransport(respond),
    )

    assert calls == 1
    assert time_matrix == [[0, 60], [30, 0]]
    assert distance_matrix == [[0.0, 120.5], [90.2, 0.0]]


@pytest.mark.asyncio
async def test_google_route_matrix_requires_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.optimizer.routing as routing

    monkeypatch.setattr(
        routing,
        "get_settings",
        lambda: Settings(_env_file=None, GOOGLE_MAPS_API_KEY=None),
    )
    with pytest.raises(RuntimeError, match="GOOGLE_MAPS_API_KEY"):
        await get_transit_matrix([(12.0, 77.0), (13.0, 78.0)])


@pytest.mark.asyncio
async def test_google_route_matrix_sends_avoid_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.optimizer.routing as routing

    monkeypatch.setattr(
        routing,
        "get_settings",
        lambda: Settings(_env_file=None, GOOGLE_MAPS_API_KEY="test-key"),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        request_payload = json.loads(request.content)
        assert request_payload["routeModifiers"] == {
            "avoidTolls": True,
            "avoidHighways": True,
            "avoidFerries": False,
        }
        return httpx.Response(
            200,
            text="\n".join(
                [
                    '{"originIndex":0,"destinationIndex":0,"condition":"ROUTE_EXISTS","duration":"0s","distanceMeters":0}',
                    '{"originIndex":0,"destinationIndex":1,"condition":"ROUTE_EXISTS","duration":"60s","distanceMeters":1000}',
                    '{"originIndex":1,"destinationIndex":0,"condition":"ROUTE_EXISTS","duration":"60s","distanceMeters":1000}',
                    '{"originIndex":1,"destinationIndex":1,"condition":"ROUTE_EXISTS","duration":"0s","distanceMeters":0}',
                ]
            ),
        )

    await get_transit_matrix(
        [(12.0, 77.0), (13.0, 78.0)],
        route_options={"avoid_tolls": True, "avoid_highways": True},
        transport=httpx.MockTransport(respond),
    )


@pytest.mark.asyncio
async def test_google_directions_returns_geojson_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.optimizer.routing as routing

    monkeypatch.setattr(
        routing,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            GOOGLE_MAPS_API_KEY="test-key",
            GOOGLE_MAPS_DIRECTIONS_URL="https://maps.example/directions",
        ),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        request_payload = json.loads(request.content)
        assert request_payload["origin"]["location"]["latLng"]["latitude"] == 12.0
        assert request_payload["destination"]["location"]["latLng"]["longitude"] == 78.0
        return httpx.Response(
            200,
            json={
                "routes": [{"polyline": {"encodedPolyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@"}}],
            },
        )

    geometry = await get_route_geometry(
        [(12.0, 77.0), (13.0, 78.0)],
        transport=httpx.MockTransport(respond),
    )

    assert geometry["geometry"]["type"] == "LineString"
    assert len(geometry["geometry"]["coordinates"]) == 3


@pytest.mark.asyncio
async def test_image_extraction_keeps_private_bytes_for_analysis_only() -> None:
    image = Image.new("RGB", (16, 16), color="white")
    image_buffer = io.BytesIO()
    image.save(image_buffer, format="PNG")
    raw_image = image_buffer.getvalue()

    result = await MediaExtractorService().extract_media(raw_image, "sample.png")

    assert result.status == MediaExtractionStatus.SUCCESS
    assert result.selected_frames[0].image_bytes == raw_image
    assert "image_bytes" not in result.model_dump()["selected_frames"][0]


@pytest.mark.asyncio
async def test_location_graph_does_not_invent_places_without_geocoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.media_analyzer.graph as graph_module

    monkeypatch.setattr(
        graph_module,
        "get_settings",
        lambda: Settings(_env_file=None, MEDIA_GEOCODER_PROVIDER="none"),
    )
    result = await LocationGraphBuilder().build_graph(MediaAnalyzed(job_id="job-1"))
    assert result.candidates == []
    assert result.verified_locations == []


def test_production_settings_require_real_provider_configuration() -> None:
    with pytest.raises(ValueError, match="Production provider configuration is incomplete"):
        Settings(_env_file=None, ENVIRONMENT="production")

    configured = Settings(
        _env_file=None,
        ENVIRONMENT="production",
        GOOGLE_MAPS_API_KEY="configured",
        MEDIA_ALLOWED_HOSTS=["cdn.example"],
        MEDIA_ANALYSIS_PROVIDER="aws",
        AWS_MEDIA_BUCKET="media-bucket",
        MEDIA_GEOCODER_PROVIDER="nominatim",
        NOMINATIM_USER_AGENT="SWENA support@example.com",
    )
    assert configured.ENVIRONMENT == "production"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=TnmvNUG76Bc", "TnmvNUG76Bc"),
        ("https://youtu.be/TnmvNUG76Bc", "TnmvNUG76Bc"),
        ("https://youtube.com/shorts/TnmvNUG76Bc", "TnmvNUG76Bc"),
        ("https://youtube.com.evil.example/watch?v=TnmvNUG76Bc", None),
    ],
)
def test_youtube_video_id_accepts_only_supported_youtube_hosts(
    url: str,
    expected: str | None,
) -> None:
    assert youtube_video_id(url) == expected


@pytest.mark.asyncio
async def test_youtube_captions_become_timestamped_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.media_analyzer.youtube as youtube

    class FakeTranscript:
        language_code = "en"

        def fetch(self) -> list[Any]:
            return [
                SimpleNamespace(text="Visit Mysuru", start=1.5, duration=2.0),
                SimpleNamespace(text="and Coorg", start=3.5, duration=1.0),
            ]

    class FakeApi:
        def list(self, video_id: str) -> list[FakeTranscript]:
            assert video_id == "TnmvNUG76Bc"
            return [FakeTranscript()]

    monkeypatch.setattr(youtube, "YouTubeTranscriptApi", FakeApi)

    observation = await fetch_youtube_captions("https://www.youtube.com/watch?v=TnmvNUG76Bc")

    assert observation.full_transcript == "Visit Mysuru and Coorg"
    assert observation.primary_language == "en"
    assert [segment.start_time for segment in observation.segments] == [1.5, 3.5]
    assert all(segment.confidence == 0.75 for segment in observation.segments)


@pytest.mark.asyncio
async def test_youtube_captions_fail_clearly_when_none_are_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.media_analyzer.youtube as youtube

    class FakeApi:
        def list(self, _: str) -> list[Any]:
            return []

    monkeypatch.setattr(youtube, "YouTubeTranscriptApi", FakeApi)

    with pytest.raises(ValueError, match="no publicly accessible captions"):
        await fetch_youtube_captions("https://youtu.be/TnmvNUG76Bc")


@pytest.mark.asyncio
async def test_youtube_media_workflow_uses_captions_without_downloading_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workflows.media_locations.nodes as media_nodes

    observation = AsrObservation(
        job_id="youtube-captions-test",
        full_transcript="Visit Mysuru",
        segments=[
            AsrSegment(
                start_time=0,
                end_time=1,
                transcript="Visit Mysuru",
                confidence=0.75,
                language="en",
            )
        ],
        primary_language="en",
        overall_confidence=0.75,
    )
    analyzed = MediaAnalyzed(job_id="youtube-captions-test", full_transcript="Visit Mysuru")

    async def get_captions(_: str) -> AsrObservation:
        return observation

    async def reject_download(_: str) -> tuple[bytes, str]:
        raise AssertionError("YouTube video must not be downloaded")

    class FakeAnalyzer:
        async def analyze_media(self, **kwargs: Any) -> MediaAnalyzed:
            assert kwargs["frames"] == []
            assert kwargs["caption_observation"] == observation
            return analyzed

        async def build_location_graph(self, _: MediaAnalyzed) -> Any:
            from contracts.media import MediaLocationGraph

            return MediaLocationGraph(
                job_id="youtube-captions-test",
                analysis_job_id="youtube-captions-test",
            )

    monkeypatch.setattr(media_nodes, "fetch_youtube_captions", get_captions)
    monkeypatch.setattr(media_nodes, "download_direct_media", reject_download)
    monkeypatch.setattr(media_nodes, "MediaAnalyzerService", FakeAnalyzer)

    result = await media_nodes.process_media_node(
        {
            "url": "https://www.youtube.com/watch?v=TnmvNUG76Bc",
            "extraction_result": None,
            "caption_observation": None,
            "analyzed": None,
            "location_graph": None,
            "verified_places": [],
            "attempt_count": 0,
        }
    )

    assert result["analyzed"] == analyzed
    assert result["verified_places"] == []


@pytest.mark.asyncio
async def test_media_analyzer_uses_caption_observation_without_video_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.media_analyzer.service as analyzer_module

    monkeypatch.setattr(
        analyzer_module,
        "get_settings",
        lambda: Settings(_env_file=None, MEDIA_ANALYSIS_PROVIDER="mock"),
    )
    observation = AsrObservation(
        job_id="youtube-captions-test",
        full_transcript="Visit Mysuru",
        segments=[
            AsrSegment(
                start_time=0,
                end_time=1,
                transcript="Visit Mysuru",
                confidence=0.75,
                language="en",
            )
        ],
        primary_language="en",
        overall_confidence=0.75,
    )

    analyzed = await MediaAnalyzerService().analyze_media(
        frames=[],
        caption_observation=observation,
    )

    assert analyzed.full_transcript == "Visit Mysuru"
    assert analyzed.models_used["asr"] == "youtube-captions"


@pytest.mark.asyncio
async def test_location_graph_requires_two_media_modalities_to_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.media_analyzer.graph as graph_module

    monkeypatch.setattr(
        graph_module,
        "get_settings",
        lambda: Settings(_env_file=None, MEDIA_GEOCODER_PROVIDER="nominatim"),
    )

    class FakeGeocoder:
        async def search(self, query: str) -> list[dict[str, Any]]:
            return [
                {
                    "name": "Abbey Falls",
                    "display_name": "Abbey Falls, Karnataka, India",
                    "lat": "12.4542",
                    "lon": "75.7196",
                    "osm_type": "way",
                    "osm_id": 123,
                    "address": {"state": "Karnataka"},
                }
            ]

    analyzed = MediaAnalyzed(
        job_id="job-2",
        fused_clues=FusedMediaClues(
            clues=[
                MediaClue(clue_type="text_ocr", text_content="Abbey Falls", confidence=0.9),
                MediaClue(clue_type="text_asr", text_content="Abbey Falls", confidence=0.9),
            ]
        ),
    )
    graph = await LocationGraphBuilder(FakeGeocoder()).build_graph(analyzed)
    assert len(graph.candidates) == 1
    assert len(graph.verified_locations) == 1
    assert graph.verified_locations[0].confidence >= 0.8


def test_aws_textract_adapter_parses_line_and_word_confidence() -> None:
    class FakeTextract:
        def detect_document_text(self, **_: Any) -> dict[str, Any]:
            return {
                "Blocks": [
                    {"BlockType": "LINE", "Text": "Mysuru Palace"},
                    {
                        "BlockType": "WORD",
                        "Text": "Mysuru",
                        "Confidence": 90,
                        "Geometry": {
                            "BoundingBox": {
                                "Top": 0.1,
                                "Left": 0.2,
                                "Height": 0.3,
                                "Width": 0.4,
                            }
                        },
                    },
                ]
            }

    analyzer = AwsMediaAnalyzer(
        textract_client=FakeTextract(),
        transcribe_client=object(),
        s3_client=object(),
    )
    image = Image.new("RGB", (10, 10))
    frame = ExtractedFrame(
        frame_index=0,
        timestamp_seconds=1.0,
        width=10,
        height=10,
        image_hash="0",
        blur_score=100,
        brightness=100,
        image_bytes=b"test-image",
    )
    result = analyzer.extract_text(image, frame)
    assert result.detected_texts == ["Mysuru Palace"]
    assert result.words[0].confidence == pytest.approx(0.9)
    assert result.words[0].bounding_box == pytest.approx([0.1, 0.2, 0.4, 0.6])


@pytest.mark.asyncio
async def test_aws_transcribe_uploads_and_parses_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.media_analyzer.aws as aws_module

    monkeypatch.setattr(
        aws_module,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            AWS_MEDIA_BUCKET="media-bucket",
            MEDIA_TRANSCRIBE_TIMEOUT_SECONDS=10,
            MEDIA_TRANSCRIBE_POLL_INTERVAL_SECONDS=0.1,
        ),
    )

    class FakeS3:
        async_calls: list[dict[str, Any]]
        delete_calls: list[dict[str, Any]]

        def __init__(self) -> None:
            self.async_calls = []
            self.delete_calls = []

        def put_object(self, **kwargs: Any) -> None:
            self.async_calls.append(kwargs)

        def delete_object(self, **kwargs: Any) -> None:
            self.delete_calls.append(kwargs)

        def get_object(self, **_: Any) -> dict[str, Any]:
            transcript = {
                "results": {
                    "transcripts": [{"transcript": "Mysuru"}],
                    "items": [
                        {
                            "type": "pronunciation",
                            "start_time": "0.1",
                            "end_time": "0.8",
                            "alternatives": [{"content": "Mysuru", "confidence": "0.95"}],
                        }
                    ],
                }
            }
            return {"Body": io.BytesIO(json.dumps(transcript).encode())}

    class FakeTranscribe:
        def start_transcription_job(self, **_: Any) -> None:
            return None

        def get_transcription_job(self, **_: Any) -> dict[str, Any]:
            return {"TranscriptionJob": {"TranscriptionJobStatus": "COMPLETED"}}

    s3 = FakeS3()
    analyzer = AwsMediaAnalyzer(
        textract_client=object(),
        transcribe_client=FakeTranscribe(),
        s3_client=s3,
    )
    observation = await analyzer.transcribe_audio(
        b"RIFF-audio",
        job_id="job-3",
    )
    assert observation.full_transcript == "Mysuru"
    assert observation.segments[0].confidence == pytest.approx(0.95)
    assert len(s3.async_calls) == 1
    assert len(s3.delete_calls) == 2
    assert all(call["Bucket"] == "media-bucket" for call in s3.delete_calls)
    assert {call["Key"].split("/")[-1] for call in s3.delete_calls} == {
        "input.wav",
        "transcript.json",
    }


@pytest.mark.asyncio
async def test_aws_transcribe_failure_cleans_up_temporary_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.media_analyzer.aws as aws_module

    monkeypatch.setattr(
        aws_module,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            AWS_MEDIA_BUCKET="media-bucket",
            MEDIA_TRANSCRIBE_TIMEOUT_SECONDS=10,
            MEDIA_TRANSCRIBE_POLL_INTERVAL_SECONDS=0.1,
        ),
    )

    class FakeS3:
        delete_calls: list[str]

        def __init__(self) -> None:
            self.delete_calls = []

        def put_object(self, **_: Any) -> None:
            return None

        def delete_object(self, **kwargs: Any) -> None:
            self.delete_calls.append(kwargs["Key"])

    class FailedTranscribe:
        def start_transcription_job(self, **_: Any) -> None:
            return None

        def get_transcription_job(self, **_: Any) -> dict[str, Any]:
            return {
                "TranscriptionJob": {
                    "TranscriptionJobStatus": "FAILED",
                    "FailureReason": "unsupported language",
                }
            }

    s3 = FakeS3()
    analyzer = AwsMediaAnalyzer(
        textract_client=object(),
        transcribe_client=FailedTranscribe(),
        s3_client=s3,
    )
    with pytest.raises(RuntimeError, match="unsupported language"):
        await analyzer.transcribe_audio(b"RIFF-audio", job_id="job-failed")
    assert len(s3.delete_calls) == 2


@pytest.mark.asyncio
async def test_aws_transcribe_timeout_cleans_up_temporary_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import functions.media_analyzer.aws as aws_module

    monkeypatch.setattr(
        aws_module,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            AWS_MEDIA_BUCKET="media-bucket",
            MEDIA_TRANSCRIBE_TIMEOUT_SECONDS=10,
            MEDIA_TRANSCRIBE_POLL_INTERVAL_SECONDS=0.1,
        ),
    )

    class FakeS3:
        delete_calls: list[str]

        def __init__(self) -> None:
            self.delete_calls = []

        def put_object(self, **_: Any) -> None:
            return None

        def delete_object(self, **kwargs: Any) -> None:
            self.delete_calls.append(kwargs["Key"])

    class PendingTranscribe:
        def start_transcription_job(self, **_: Any) -> None:
            return None

        def get_transcription_job(self, **_: Any) -> dict[str, Any]:
            return {"TranscriptionJob": {"TranscriptionJobStatus": "IN_PROGRESS"}}

    settings = Settings(
        _env_file=None,
        AWS_MEDIA_BUCKET="media-bucket",
        MEDIA_TRANSCRIBE_TIMEOUT_SECONDS=10,
        MEDIA_TRANSCRIBE_POLL_INTERVAL_SECONDS=0.1,
    )
    monkeypatch.setattr(aws_module, "get_settings", lambda: settings)
    clock = iter((0.0, 11.0))
    monkeypatch.setattr(
        aws_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(clock)),
    )
    s3 = FakeS3()
    analyzer = AwsMediaAnalyzer(
        textract_client=object(),
        transcribe_client=PendingTranscribe(),
        s3_client=s3,
    )
    with pytest.raises(TimeoutError):
        await analyzer.transcribe_audio(b"RIFF-audio", job_id="job-timeout")
    assert len(s3.delete_calls) == 2
