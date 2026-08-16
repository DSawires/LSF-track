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

import uuid
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.models import Item, ItemImage, User

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


def _upload_root() -> Path:
    root = Path(get_settings().upload_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _image_payload(image: ItemImage) -> dict:
    return {
        "id": str(image.id),
        "item_id": str(image.item_id),
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
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    if db.get(Item, item_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown item")

    settings = get_settings()
    head = await file.read(16)
    content_type = _sniff(head)
    if content_type is None:
        raise HTTPException(422, "not a recognised image format")

    image_id = uuid.uuid4()
    destination = _upload_root() / str(image_id)
    size = 0
    try:
        with destination.open("wb") as out:
            out.write(head)
            size = len(head)
            while chunk := await file.read(64 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"image exceeds {settings.max_upload_bytes // (1024 * 1024)}MB",
                    )
                out.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise

    image = ItemImage(
        id=image_id,
        item_id=item_id,
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


@router.get("/images/{image_id}")
def serve_image(
    image_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    image = db.get(ItemImage, image_id)
    if image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown image")
    path = _upload_root() / str(image.id)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file missing from storage")
    return FileResponse(path, media_type=image.content_type)
