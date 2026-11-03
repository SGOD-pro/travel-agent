"""AWS Textract and Transcribe adapters for media analysis."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import uuid
from typing import Any

from PIL import Image

from config.config import get_settings
from contracts.media import (
    AsrObservation,
    ExtractedFrame,
    OcrObservation,
    OcrWord,
)
from functions.media_analyzer.ocr import detect_languages_in_text
from functions.media_analyzer.transcription import AsrClient

logger = logging.getLogger(__name__)


class AwsMediaAnalyzer:
    """Uses AWS-managed OCR and speech transcription with the default IAM chain."""

    def __init__(
        self,
        textract_client: Any | None = None,
        transcribe_client: Any | None = None,
        s3_client: Any | None = None,
    ) -> None:
        import boto3

        region = get_settings().AWS_REGION
        self._textract = textract_client or boto3.client("textract", region_name=region)
        self._transcribe = transcribe_client or boto3.client("transcribe", region_name=region)
        self._s3 = s3_client or boto3.client("s3", region_name=region)

    def extract_text(self, image: Image.Image, frame: ExtractedFrame) -> OcrObservation:
        image_bytes = frame.image_bytes
        if image_bytes is None:
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG")
            image_bytes = buffer.getvalue()
        response = self._textract.detect_document_text(Document={"Bytes": image_bytes})

        words: list[OcrWord] = []
        lines: list[str] = []
        for block in response.get("Blocks", []):
            text = block.get("Text")
            if not isinstance(text, str) or not text.strip():
                continue
            if block.get("BlockType") == "LINE":
                lines.append(text)
            elif block.get("BlockType") == "WORD":
                box = block.get("Geometry", {}).get("BoundingBox", {})
                top = float(box.get("Top", 0.0))
                left = float(box.get("Left", 0.0))
                words.append(
                    OcrWord(
                        text=text,
                        confidence=max(0.0, min(1.0, float(block.get("Confidence", 0.0)) / 100)),
                        language=detect_languages_in_text(text)[0],
                        bounding_box=[
                            top,
                            left,
                            top + float(box.get("Height", 0.0)),
                            left + float(box.get("Width", 0.0)),
                        ],
                    )
                )

        confidences = [word.confidence for word in words]
        text_for_language = " ".join(lines)
        return OcrObservation(
            frame_timestamp=frame.timestamp_seconds,
            frame_index=frame.frame_index,
            detected_texts=lines,
            words=words,
            languages=detect_languages_in_text(text_for_language) if text_for_language else [],
            mean_confidence=sum(confidences) / len(confidences) if confidences else 0.0,
        )

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        *,
        job_id: str,
        language_code: str = "en-IN",
    ) -> AsrObservation:
        settings = get_settings()
        bucket = settings.AWS_MEDIA_BUCKET
        if not bucket:
            raise RuntimeError("AWS_MEDIA_BUCKET is required for AWS Transcribe.")
        if not audio_bytes:
            raise ValueError("Audio bytes are required for AWS Transcribe.")

        run_id = uuid.uuid4().hex
        input_key = f"media-analysis/{run_id}/input.wav"
        output_key = f"media-analysis/{run_id}/transcript.json"
        transcription_job_name = f"swena-{run_id}"
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=bucket,
            Key=input_key,
            Body=audio_bytes,
            ContentType="audio/wav",
        )
        await asyncio.to_thread(
            self._transcribe.start_transcription_job,
            TranscriptionJobName=transcription_job_name,
            LanguageCode=language_code,
            MediaFormat="wav",
            Media={"MediaFileUri": f"s3://{bucket}/{input_key}"},
            OutputBucketName=bucket,
            OutputKey=output_key,
        )

        try:
            deadline = time.monotonic() + settings.MEDIA_TRANSCRIBE_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                response = await asyncio.to_thread(
                    self._transcribe.get_transcription_job,
                    TranscriptionJobName=transcription_job_name,
                )
                job = response.get("TranscriptionJob", {})
                status = job.get("TranscriptionJobStatus")
                if status == "COMPLETED":
                    transcript_obj = await asyncio.to_thread(
                        self._s3.get_object,
                        Bucket=bucket,
                        Key=output_key,
                    )
                    body = transcript_obj["Body"]
                    try:
                        payload = json.loads(body.read())
                    finally:
                        close = getattr(body, "close", None)
                        if callable(close):
                            close()
                    return AsrClient().parse_transcribe_payload(payload, job_id)
                if status == "FAILED":
                    reason = job.get("FailureReason", "AWS Transcribe failed.")
                    raise RuntimeError(str(reason))
                await asyncio.sleep(settings.MEDIA_TRANSCRIBE_POLL_INTERVAL_SECONDS)

            raise TimeoutError(
                f"AWS Transcribe did not finish within "
                f"{settings.MEDIA_TRANSCRIBE_TIMEOUT_SECONDS} seconds."
            )
        finally:
            await self._delete_object(bucket, input_key)
            await self._delete_object(bucket, output_key)

    async def _delete_object(self, bucket: str, key: str) -> None:
        """Delete temporary Transcribe objects without masking the original result."""
        try:
            await asyncio.to_thread(self._s3.delete_object, Bucket=bucket, Key=key)
        except Exception:
            logger.warning(
                "Failed to clean up temporary media-analysis object s3://%s/%s",
                bucket,
                key,
                exc_info=True,
            )
