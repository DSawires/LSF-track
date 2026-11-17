"""What counts as a picture, and where one is kept.

Shared by the two ways a picture reaches an item: an upload from a phone
(app/api/images.py) and a picture placed on an item's row of an import file
(app/services/imports.py). Both must agree on the formats accepted and on the
storage key, or a picture stored by one could not be served by the other.
"""

from __future__ import annotations

import uuid

# Magic bytes for the formats phones and spreadsheets actually produce.
_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"RIFF", "image/webp"),  # RIFF....WEBP, checked further below
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def sniff(head: bytes) -> str | None:
    """The content type of an image, from its first bytes -- never from what
    the sender claims. None means it is not a picture we store."""
    for signature, content_type in _SIGNATURES:
        if head.startswith(signature):
            if content_type == "image/webp" and head[8:12] != b"WEBP":
                continue
            return content_type
    # HEIC/HEIF (iPhone default): ISO-BMFF container, brand at offset 8.
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1"):
        return "image/heic"
    return None


def image_key(image_id: uuid.UUID) -> str:
    """Files are named by their row id, never by anything a client supplied."""
    return f"images/{image_id}"
