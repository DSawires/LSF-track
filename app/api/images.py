"""Item photos.

Files land on disk under LSF_UPLOAD_DIR named by their row id (never by the
client's filename), and are only ever served back by id through an authenticated
endpoint with the stored content type. The magic-byte sniff means a renamed
executable doesn't get stored as a "photo" even if the phone lies about the
content type.

Uploads are an online-only convenience, like the office screens. The offline
guarantee covers the logging path; queueing multi-megabyte blobs in IndexedDB
would put the sync queue's reliability at risk for the sake of a nice-to-have.
"""

from __future__ import annotations

import tempfile
import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.models import Item, ItemImage, User
from app.schemas import ImageNoteUpdate
from app.storage import get_storage

router = APIRouter(prefix="/api", tags=["images"])

# Magic bytes for the formats phones actually produce.
_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"RIFF", "image/webp"),  # RIFF....WEBP, checked further below
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def _sniff(head: bytes) -> str | None:
    for signature, content_type in _SIGNATURES:
        if head.startswith(signature):
            if content_type == "image/webp" and head[8:12] != b"WEBP":
                continue
            return content_type
    # HEIC/HEIF (iPhone default): ISO-BMFF container, brand at offset 8.
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1"):
        return "image/heic"
    return None


def _image_key(image_id: uuid.UUID) -> str:
    return f"images/{image_id}"


_KINDS = {"snag", "icon"}


def _image_payload(image: ItemImage) -> dict:
    return {
        "id": str(image.id),
        "item_id": str(image.item_id),
        "kind": image.kind,
        "content_type": image.content_type,
        "size_bytes": image.size_bytes,
        "filename": image.filename,
        "note": image.note,
        "uploaded_at": image.uploaded_at.isoformat(),
        "url": f"/api/images/{image.id}",
    }


@router.post("/items/{item_id}/images", status_code=status.HTTP_201_CREATED)
async def upload_image(
    item_id: uuid.UUID,
    file: UploadFile = File(...),
    note: str = Form(default=""),
    kind: str = Form(default="snag"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    if db.get(Item, item_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown item")
    if kind not in _KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(_KINDS)}")

    settings = get_settings()
    head = await file.read(16)
    content_type = _sniff(head)
    if content_type is None:
        raise HTTPException(422, "not a recognised image format")

    image_id = uuid.uuid4()
    size = 0
    # Spooled locally first so the size cap is enforced before a byte reaches
    # the storage backend; small photos never touch disk at all.
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as spool:
        spool.write(head)
        size = len(head)
        while chunk := await file.read(64 * 1024):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    f"image exceeds {settings.max_upload_bytes // (1024 * 1024)}MB",
                )
            spool.write(chunk)
        spool.seek(0)
        get_storage().put(_image_key(image_id), spool, content_type)

    image = ItemImage(
        id=image_id,
        item_id=item_id,
        kind=kind,
        filename=(file.filename or "photo")[:255],
        content_type=content_type,
        size_bytes=size,
        uploaded_by_user_id=user.id,
        note=note.strip()[:255] or None,
    )
    db.add(image)
    db.flush()
    return _image_payload(image)


@router.get("/items/{item_id}/images")
def list_images(
    item_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    rows = db.scalars(
        sa.select(ItemImage)
        .where(ItemImage.item_id == item_id)
        .order_by(ItemImage.uploaded_at.desc())
    )
    return {"images": [_image_payload(image) for image in rows]}


@router.patch("/images/{image_id}")
def update_image_note(
    image_id: uuid.UUID,
    payload: ImageNoteUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Describe a snag after the fact. Only the note is editable: the bytes an
    image id names never change, which is what lets them be cached forever."""
    image = db.get(ItemImage, image_id)
    if image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown image")
    image.note = payload.note.strip()[:255] or None
    db.flush()
    return _image_payload(image)


@router.get("/images/{image_id}")
def serve_image(
    image_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    image = db.get(ItemImage, image_id)
    if image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown image")
    response = get_storage().response(_image_key(image.id), image.content_type)
    if response is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file missing from storage")
    # An image id names immutable bytes: replacing an icon uploads a new id and
    # the latest wins. So the browser may cache a given URL forever.
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return response
