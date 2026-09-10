"""Validate original files without extracting a second document representation.

Native model inputs own document interpretation. Validation only establishes
the media type, supported input capability, and safe resource bounds.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from PIL import Image
from pypdf import PdfReader

MAX_PDF_PAGES = 200
TEXT_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".json": "application/json",
              ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".log": "text/plain"}
OFFICE_TYPES = {
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "word/document.xml"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xl/workbook.xml"),
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "ppt/presentation.xml"),
}


@dataclass
class ValidatedAttachment:
    media_type: str
    read_mode: str = "native"
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def decode_text(content: bytes) -> str:
    encoding = "utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    value = content.decode(encoding)
    if "\0" in value or not value.strip():
        raise ValueError("This file has no readable text. Export a text copy and attach it again.")
    return value


def validate_file(filename: str, content: bytes) -> ValidatedAttachment:
    suffix = Path(filename).suffix.lower()
    if content.startswith(b"%PDF-"):
        result = ValidatedAttachment("application/pdf")
        try:
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise ValueError("This PDF is password protected. Upload an unlocked copy.")
            page_count = len(reader.pages)
            result.metadata = {"pageCount": page_count}
            if not 0 < page_count <= MAX_PDF_PAGES:
                raise ValueError(f"Use a PDF with 1–{MAX_PDF_PAGES} pages. Split larger documents.")
        except Exception as error:
            result.read_mode = "unavailable"
            result.error = str(error) if isinstance(error, ValueError) else "This PDF could not be opened. Export it again."
        return result
    image_format = None
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        image_format = "png"
    elif content.startswith(b"\xff\xd8\xff"):
        image_format = "jpeg"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        image_format = "webp"
    if image_format:
        try:
            with Image.open(io.BytesIO(content)) as image:
                if image.width * image.height > 25_000_000:
                    raise ValueError("Image exceeds the pixel limit.")
                image.verify()
            return ValidatedAttachment(f"image/{image_format}")
        except Exception as error:
            raise ValueError("This image could not be opened. Choose the original or a smaller copy.") from error
    if suffix in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("The file contents do not match its name. Choose the original file.")
    if suffix in OFFICE_TYPES:
        media_type, expected_part = OFFICE_TYPES[suffix]
        try:
            with ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                if len(entries) > 5000 or sum(item.file_size for item in entries) > 100 * 1024 * 1024:
                    raise ValueError("This document is too complex. Export a smaller copy.")
                if expected_part not in archive.namelist() or "[Content_Types].xml" not in archive.namelist():
                    raise ValueError("The file contents do not match its document format.")
            return ValidatedAttachment(media_type)
        except BadZipFile as error:
            raise ValueError("This document could not be opened. Choose the original file.") from error
    if suffix in TEXT_TYPES:
        result = ValidatedAttachment(TEXT_TYPES[suffix])
        try:
            decode_text(content)
        except (ValueError, UnicodeError) as error:
            result.read_mode = "unavailable"
            result.error = "Save this text file as UTF-8 and attach it again." if isinstance(error, UnicodeError) else str(error)
        return result
    return ValidatedAttachment("application/octet-stream", "unavailable", error="Saved. Reading this file format is not supported yet.")
