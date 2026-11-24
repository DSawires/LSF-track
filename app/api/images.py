"""Item photos.

Files land in storage named by their row id (never by the client's filename),
and are only ever served back by id through an authenticated endpoint. What is
stored is not what was sent: every picture is decoded and re-encoded as a WebP
display copy and a thumbnail, upright and without its metadata, and the
original is dropped (app/services/images.py). A file that cannot be decoded is
refused, so a renamed executable never becomes a "photo" whatever the phone
claims it is.

Uploads are an online-only convenience, like the office screens. The offline
guarantee covers the logging path; queueing multi-megabyte blobs in IndexedDB
would put the sync queue's reliability at risk for the sake of a nice-to-have.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import current_user, require_admin
from app.models import Item, ItemImage, User, WorkOrderLine
from app.schemas import ImageNoteUpdate
from app.services import images as pictures
from app.storage import get_storage

router = APIRouter(prefix="/api", tags=["images"])

# snag: a defect photo. icon: the item's thumbnail (latest wins). document: a
# picture chosen for a work order instead of the icon -- it does not replace it.
_KINDS = {"snag", "icon", "document"}


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
        "thumb_url": f"/api/images/{image.id}/thumb",
    }


@router.post("/items/{item_id}/images", status_code=status.HTTP_201_CREATED)
# A plain `def`, so FastAPI runs it on a worker thread. Everything in here
# blocks -- the database, and with S3 the upload itself -- and as a coroutine it
# would hold the one event loop for the length of a photo upload, stalling every
# other phone's sync behind it.
def upload_image(
    item_id: uuid.UUID,
    file: UploadFile = File(...),
    note: str = Form(default=""),
    kind: str = Form(default="snag"),
    db: Session = Depends(get_db, scope="function"),
    user: User = Depends(current_user),
) -> dict:
    if db.get(Item, item_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown item")
    if kind not in _KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(_KINDS)}")

    settings = get_settings()
    head = file.file.read(16)
    if pictures.sniff(head) is None:
        raise HTTPException(422, "not a recognised image format")

    # Read with the cap enforced as it goes: the whole of an oversized file is
    # never held, and nothing reaches storage before it is known to fit.
    received = bytearray(head)
    while chunk := file.file.read(64 * 1024):
        received += chunk
        if len(received) > settings.max_upload_bytes:
            raise HTTPException(
                413,
                f"image exceeds {settings.max_upload_bytes // (1024 * 1024)}MB",
            )
    try:
        prepared = pictures.prepare(bytes(received))
    except pictures.Unreadable as error:
        raise HTTPException(422, f"not a readable image: {error}") from None

    image_id = uuid.uuid4()
    size = pictures.store(get_storage(), image_id, prepared)

    image = ItemImage(
        id=image_id,
        item_id=item_id,
        kind=kind,
        filename=(file.filename or "photo")[:255],
        content_type=pictures.STORED_TYPE,
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
    db: Session = Depends(get_db, scope="function"),
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
    db: Session = Depends(get_db, scope="function"),
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


@router.delete("/images/{image_id}")
def delete_image(
    image_id: uuid.UUID,
    db: Session = Depends(get_db, scope="function"),
    admin: User = Depends(require_admin),
) -> dict:
    """Remove a photo: the bytes off storage, then the row.

    Admin-only, like archiving and purging an item. A photo is often the only
    record of what a snag actually looked like, there is no undo, and the
    reasons to delete one -- wrong item, wrong batch, a face in the frame --
    are all judgement calls someone should own.

    Deleting a photo loses no history: `item_images` is not the log. An item's
    icon is simply the newest icon row, so removing one falls back to the
    previous icon, or to none.

    Storage first, database second, matching the item purge: a failure there
    leaves a listed photo whose bytes are gone (which `serve_image` already
    reports honestly) rather than bytes no row will ever name again.
    """
    image = db.get(ItemImage, image_id)
    if image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown image")
    # A work order that printed this picture keeps its line and loses the picture.
    db.execute(
        sa.update(WorkOrderLine)
        .where(WorkOrderLine.image_id == image.id)
        .values(image_id=None)
        .execution_options(synchronize_session=False)
    )
    pictures.remove(get_storage(), image.id)
    db.delete(image)
    db.flush()
    return {"deleted": True, "item_id": str(image.item_id)}


@router.get("/images/{image_id}")
def serve_image(
    image_id: uuid.UUID,
    db: Session = Depends(get_db, scope="function"),
    user: User = Depends(current_user),
):
    return _serve(db, image_id, thumb=False)


@router.get("/images/{image_id}/thumb")
def serve_thumbnail(
    image_id: uuid.UUID,
    db: Session = Depends(get_db, scope="function"),
    user: User = Depends(current_user),
):
    """The small copy, for icons and photo grids. A picture stored before
    thumbnails existed has none until `manage.py compress-images` has run,
    and is served whole in the meantime."""
    return _serve(db, image_id, thumb=True)


def _serve(db: Session, image_id: uuid.UUID, thumb: bool):
    image = db.get(ItemImage, image_id)
    if image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown image")
    storage = get_storage()
    response = storage.response(pictures.thumb_key(image.id), pictures.STORED_TYPE) if thumb else None
    if response is None:
        response = storage.response(pictures.image_key(image.id), image.content_type)
    if response is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file missing from storage")
    if thumb and image.content_type != pictures.STORED_TYPE:
        # The whole picture standing in for a thumbnail not made yet: not to be
        # cached as if it were the thumbnail for good.
        response.headers["Cache-Control"] = "private, no-cache"
        return response
    # An image id names immutable bytes: replacing an icon uploads a new id and
    # the latest wins. So the browser may cache a given URL forever.
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return response
