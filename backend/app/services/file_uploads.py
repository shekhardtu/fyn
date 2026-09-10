"""Shared file policy and bounded IO for chat and lending uploads."""
from __future__ import annotations

import hashlib
import io
import re
from typing import BinaryIO

from ..config import FILE_UPLOAD_MAX_BYTES


class FileUploadError(ValueError):
    pass


def clean_filename(raw: str | None) -> str:
    name = (raw or "file").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[\x00-\x1f\x7f]", "", name)[:240].strip(" .") or "file"


def read_upload(source: BinaryIO, limit: int = FILE_UPLOAD_MAX_BYTES) -> tuple[bytes, str]:
    """Read once, enforcing actual size while computing the original's digest."""
    limit = min(limit, FILE_UPLOAD_MAX_BYTES)
    size = 0
    digest = hashlib.sha256()
    destination = io.BytesIO()
    while True:
        chunk = source.read(min(1024 * 1024, limit + 1 - size))
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise FileUploadError(f"That file is larger than the {limit // (1024 * 1024)} MB limit.")
        digest.update(chunk)
        destination.write(chunk)
    if size == 0:
        raise FileUploadError("The selected file is empty.")
    return destination.getvalue(), digest.hexdigest()
