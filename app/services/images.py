"""What counts as a picture, what is kept of one, and where.

Shared by every way a picture reaches an item: an upload from a phone
(app/api/images.py), a picture placed on an item's row of an import file
(app/services/imports.py), and `manage.py compress-images`, which brings
pictures stored before this existed into line. All of them store through
`store()` and remove through `remove()`, so a picture is always the same two
files, wherever it came from.

What is kept is not what was sent. A phone photo is 2-5 MB of pixels nobody
looks at, carrying the GPS position it was taken at, in whatever format the
phone chose -- HEIC, from an iPhone, which Chrome cannot show and the work
order PDF cannot draw. So every picture is decoded, turned upright, stripped
of its metadata and stored as two WebP files:

- the display copy, at most DISPLAY_EDGE pixels on its long edge: what opens
  when a photo is tapped, and what a work order prints;
- the thumbnail, at most THUMB_EDGE: item icons in the lists, photo grids.

The original is not kept. A snag photo at 2048 px is still sharp enough to
read the defect; it is a working record, not evidence held at full resolution.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass

DISPLAY_EDGE = 2048
THUMB_EDGE = 320
DISPLAY_QUALITY = 80
THUMB_QUALITY = 72
STORED_TYPE = "image/webp"
# A 48 MP phone sensor is ~49 M pixels; anything past this is not a photo
# but an attempt to make the server inflate one.
MAX_PIXELS = 64_000_000

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
    """The display copy. Files are named by their row id, never by anything a
    client supplied."""
    return f"images/{image_id}"


def thumb_key(image_id: uuid.UUID) -> str:
    return f"images/{image_id}.thumb"


class Unreadable(ValueError):
    """Starts like a picture, but cannot be decoded as one."""


@dataclass
class Prepared:
    display: bytes
    thumb: bytes


_heif_registered = False


def _register_heif() -> None:
    global _heif_registered
    if not _heif_registered:
        from pillow_heif import register_heif_opener

        register_heif_opener()
        _heif_registered = True


def _encode(picture, edge: int, quality: int) -> bytes:
    copy = picture.copy()
    copy.thumbnail((edge, edge))  # shrinks only, keeps the proportions
    out = io.BytesIO()
    # No exif= argument: nothing of the original's metadata is written.
    copy.save(out, "WEBP", quality=quality, method=4)
    return out.getvalue()


def prepare(data: bytes) -> Prepared:
    """The two files kept of a picture. Raises Unreadable if it is not one."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    _register_heif()
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > MAX_PIXELS:
                raise Unreadable("the picture is too large to process")
            # Phones store "rotate this" in the metadata rather than turning
            # the pixels; once the metadata goes, the pixels must be upright.
            picture = ImageOps.exif_transpose(source)
            picture.load()
    except Unreadable:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
        raise Unreadable("the picture could not be read") from error
    has_alpha = picture.mode in ("RGBA", "LA", "PA") or (
        picture.mode == "P" and "transparency" in picture.info
    )
    picture = picture.convert("RGBA" if has_alpha else "RGB")
    return Prepared(
        display=_encode(picture, DISPLAY_EDGE, DISPLAY_QUALITY),
        thumb=_encode(picture, THUMB_EDGE, THUMB_QUALITY),
    )


def store(storage, image_id: uuid.UUID, prepared: Prepared) -> int:
    """Write both files; returns the display copy's size. The display copy
    first: a crash between the two leaves a picture that is served (the
    thumbnail falls back to it) and that `compress-images` finishes."""
    storage.put(image_key(image_id), io.BytesIO(prepared.display), STORED_TYPE)
    storage.put(thumb_key(image_id), io.BytesIO(prepared.thumb), STORED_TYPE)
    return len(prepared.display)


def remove(storage, image_id: uuid.UUID) -> None:
    storage.delete(thumb_key(image_id))
    storage.delete(image_key(image_id))
