"""AWS client factories.

Ensures no clients are instantiated upon import.
Clients are created lazily on demand.
"""

from __future__ import annotations

from typing import Any


def get_s3_client() -> Any:
    """Return a boto3 S3 client lazily."""
    import boto3  # type: ignore

    return boto3.client("s3")


def get_sqs_client() -> Any:
    """Return a boto3 SQS client lazily."""
    import boto3  # type: ignore

    return boto3.client("sqs")
