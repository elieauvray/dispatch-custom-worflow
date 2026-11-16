"""Workflow diagrams uploaded on the describe page (an image or a PDF Claude reads like a description)."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

IMAGE_MAX_BYTES = 3_750_000  # 5 MB once base64-encoded: the API limit for one image
PDF_MAX_BYTES = 10_000_000

# Placeholder description for a workflow given only as a diagram (Maestro workflows always have one).
DIAGRAM_ONLY_DESCRIPTION = "The process is described by the attached diagram."


def detect_media_type(data: bytes) -> str | None:
    """Media type from the file's first bytes (never trust the browser's)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def validate(data: bytes) -> tuple[str | None, str | None]:
    """(media_type, error)."""
    media_type = detect_media_type(data)
    if media_type is None:
        return None, "The diagram must be a PNG, JPEG, GIF or WebP image, or a PDF."
    limit = PDF_MAX_BYTES if media_type == "application/pdf" else IMAGE_MAX_BYTES
    if len(data) > limit:
        return None, f"The diagram is too large ({len(data) / 1e6:.1f} MB, {limit / 1e6:.2f} MB max)."
    return media_type, None


def content_block(media_type: str, data: bytes) -> dict[str, Any]:
    source = {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()}
    if media_type == "application/pdf":
        return {"type": "document", "source": source}
    return {"type": "image", "source": source}


def with_diagram(text: str, diagram: dict[str, Any] | None) -> str | list[dict[str, Any]]:
    """User message content: the diagram first (Claude reads it best before the text), then the text."""
    if not diagram:
        return text
    note = (
        "The attached diagram describes the process. Read it as the process description: stages, order, "
        "actors, gates, loops and tools. The text below complements it; where they differ, ask or follow the text."
    )
    return [content_block(diagram["media_type"], diagram["data"]), {"type": "text", "text": note + "\n\n" + text}]


def fingerprint(data: bytes | None) -> str:
    return hashlib.sha256(data).hexdigest() if data else ""
