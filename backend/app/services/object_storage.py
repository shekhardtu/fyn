"""Private R2 object transport. Domain services own authorization and lifecycle."""
from __future__ import annotations

import hashlib
from typing import Any

from botocore.config import Config

from ..config import Settings


class ObjectStorageError(ValueError):
    pass


def r2_client(settings: Settings) -> Any:
    if not all((settings.r2_account_id, settings.r2_bucket, settings.r2_access_key_id, settings.r2_secret_access_key)):
        raise ObjectStorageError("Attachment storage is not configured.")
    import boto3
    return boto3.client(
        "s3", endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id, aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto", config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=30,
                                         retries={"max_attempts": 2, "mode": "standard"}),
    )


def object_key(key: str, settings: Settings) -> str:
    prefix = settings.r2_object_prefix.strip("/")
    return f"{prefix}/{key}" if prefix else key


class R2ObjectStore:
    """No local fallback: every chat original is private and shared across workers."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = r2_client(settings)

    def read(self, key: str, limit: int, *, digest: str | None = None) -> bytes:
        response = self.client.get_object(Bucket=self.settings.r2_bucket, Key=object_key(key, self.settings))
        body = response["Body"]
        try:
            if response["ContentLength"] > limit:
                raise ObjectStorageError("The uploaded file exceeds its size limit.")
            content = body.read(limit + 1)
        finally:
            body.close()
        if not content or len(content) > limit:
            raise ObjectStorageError("The uploaded file is empty or too large.")
        if digest and hashlib.sha256(content).hexdigest() != digest:
            raise ObjectStorageError("The saved file failed its integrity check.")
        return content

    def write(self, key: str, content: bytes, media_type: str) -> None:
        self.client.put_object(Bucket=self.settings.r2_bucket, Key=object_key(key, self.settings),
                               Body=content, ContentType=media_type,
                               Metadata={"sha256": hashlib.sha256(content).hexdigest()})

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.settings.r2_bucket, Key=object_key(key, self.settings))
