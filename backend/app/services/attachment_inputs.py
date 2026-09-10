"""Provider-native media and source identity for one attachment capability."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agno.media import File, Image
from pydantic import BaseModel

from ..models import ConversationAttachment
from .attachment_validation import OFFICE_TYPES


class AttachmentSource(BaseModel):
    id: str
    filename: str
    provider_filename: str
    media_type: str


def attachment_source(row: ConversationAttachment) -> AttachmentSource:
    suffixes = {"application/pdf": ".pdf", "text/csv": ".csv", "text/tab-separated-values": ".tsv",
                "application/json": ".json", "text/markdown": ".md", "text/plain": ".txt",
                "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                **{media: extension for extension, (media, _part) in OFFICE_TYPES.items()}}
    filename = f"{row.id}-{Path(row.filename).stem[:160]}{suffixes.get(row.media_type, '.bin')}"
    return AttachmentSource(id=str(row.id), filename=row.filename, provider_filename=filename, media_type=row.media_type)


@dataclass
class NativeAttachmentInputs:
    files: list[File] = field(default_factory=list)
    images: list[Image] = field(default_factory=list)

    def add(self, source: AttachmentSource, content: bytes) -> None:
        if source.media_type.startswith("image/"):
            self.images.append(Image(id=source.id, content=content, mime_type=source.media_type))
        else:
            self.files.append(File(content=content, mime_type=source.media_type, filename=source.provider_filename))
