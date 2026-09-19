"""Unit tests for Artifact domain models."""

import uuid

from travel.domain.artifacts import Artifact, ArtifactStatus


def test_artifact_lifecycle() -> None:
    trip_id = uuid.uuid4()
    artifact = Artifact(
        trip_id=trip_id,
        trip_version=1,
        object_key=f"trips/{trip_id}/v1/itinerary.pdf",
    )

    assert artifact.status == ArtifactStatus.PENDING
    assert artifact.content_type == "application/pdf"
    assert artifact.size_bytes is None
    assert artifact.download_url is None

    # Mark completed
    artifact.mark_completed(
        size_bytes=4096, download_url=f"/api/v1/artifacts/{artifact.id}/download"
    )
    assert artifact.status == ArtifactStatus.COMPLETED
    assert artifact.size_bytes == 4096
    assert artifact.download_url == f"/api/v1/artifacts/{artifact.id}/download"


def test_artifact_mark_failed() -> None:
    trip_id = uuid.uuid4()
    artifact = Artifact(
        trip_id=trip_id,
        trip_version=2,
        object_key=f"trips/{trip_id}/v2/itinerary.pdf",
    )

    artifact.mark_failed("Storage connection timed out")
    assert artifact.status == ArtifactStatus.FAILED
    assert artifact.metadata["error"] == "Storage connection timed out"
