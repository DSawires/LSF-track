from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.deps import current_user
from app.models import Item, ItemImage, Project, RouteTemplate, Stage, User
from app.schemas import ItemCreate, ReleaseRequest, RevisionBumpRequest
from app.services import reports
from app.services.release import ReleaseError, bump_revision, release_item

router = APIRouter(prefix="/api/items", tags=["items"])


def _icon_urls(db: Session, item_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Latest icon-kind image per item; ordering ascending means later uploads
    overwrite earlier ones in the dict."""
    if not item_ids:
        return {}
    icons: dict[uuid.UUID, str] = {}
    for image in db.scalars(
        sa.select(ItemImage)
        .where(ItemImage.kind == "icon", ItemImage.item_id.in_(item_ids))
        .order_by(ItemImage.uploaded_at)
    ):
        icons[image.item_id] = f"/api/images/{image.id}"
    return icons


def _item_payload(item: Item, icon_url: str | None = None) -> dict:
    return {
        "id": str(item.id),
        "icon_url": icon_url,
        "code": item.code,
        "project_id": str(item.project_id),
        "description": item.description,
        "total_qty": item.total_qty,
        "drawing_revision": item.drawing_revision,
        "released_revision": item.released_revision,
        "target_release_date": (
            item.target_release_date.isoformat() if item.target_release_date else None
        ),
        "is_released": item.is_released,
        "released_at": item.released_at.isoformat() if item.released_at else None,
        "route_template_id": str(item.route_template_id) if item.route_template_id else None,
        "steps": [
            {"id": str(s.id), "seq": s.seq, "stage_id": str(s.stage_id)} for s in item.steps
        ],
    }


@router.get("")
def list_items(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    project_id: uuid.UUID | None = None,
    stage_id: uuid.UUID | None = None,
    released: bool | None = None,
    q: str | None = None,
) -> dict:
    query = sa.select(Item).options(selectinload(Item.steps)).order_by(Item.code)
    if project_id is not None:
        query = query.where(Item.project_id == project_id)
    if q:
        needle = f"%{q.strip()}%"
        query = query.where(Item.code.ilike(needle) | Item.description.ilike(needle))
    if released is True:
        query = query.where(Item.released_at.is_not(None))
    elif released is False:
        query = query.where(Item.released_at.is_(None))

    items = list(db.scalars(query))
    released_ids = [item.id for item in items if item.is_released]
    state = reports.item_state(db, released_ids) if released_ids else {}
    icons = _icon_urls(db, [item.id for item in items])

    rows = []
    for item in items:
        item_state = state.get(str(item.id))
        # The stage filter is on *derived* position: an item is "at paint" if any
        # quantity currently rests at a paint step.
        if stage_id is not None:
            if item_state is None:
                continue
            if not any(
                p["stage_id"] == str(stage_id)
                for p in item_state["positions"]
                if p["stage_id"]
            ):
                continue
        rows.append({**_item_payload(item, icons.get(item.id)), "state": item_state})
    return {"items": rows}


@router.get("/{item_id}")
def get_item(
    item_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    item = db.get(Item, item_id, options=[selectinload(Item.steps)])
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown item")
    state = reports.item_state(db, [item.id]) if item.is_released else {}
    icon = _icon_urls(db, [item.id]).get(item.id)
    return {**_item_payload(item, icon), "state": state.get(str(item.id))}


@router.get("/{item_id}/events")
def item_events(
    item_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    limit: int = 20,
) -> dict:
    """Most recent events for an item, newest first.

    This is what the phone shows under "recent entries", and where a correction
    picks the event it supersedes.
    """
    from app.api.events import _event_payload
    from app.models import Event

    if db.get(Item, item_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown item")
    rows = db.scalars(
        sa.select(Event)
        .where(Event.item_id == item_id)
        .order_by(Event.occurred_at.desc(), Event.received_at.desc())
        .limit(min(limit, 100))
    )
    return {"events": [_event_payload(e) for e in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_item(
    payload: ItemCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    if db.get(Project, payload.project_id) is None:
        raise HTTPException(422, "unknown project")
    code = payload.code.strip()
    existing = db.scalars(sa.select(Item).where(Item.code == code)).first()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"item {code} already exists")
    item = Item(
        code=code,
        project_id=payload.project_id,
        description=payload.description.strip(),
        total_qty=payload.total_qty,
        drawing_revision=payload.drawing_revision.strip(),
        target_release_date=payload.target_release_date,
    )
    db.add(item)
    db.flush()
    return _item_payload(item)


@router.post("/{item_id}/release")
def release(
    item_id: uuid.UUID,
    payload: ReleaseRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown item")
    template = db.get(RouteTemplate, payload.route_template_id)
    if template is None or not template.is_published:
        raise HTTPException(422, "unknown route template")
    try:
        release_item(
            db,
            item,
            template,
            user,
            drawing_revision=payload.drawing_revision,
            initial_quantities=payload.initial_quantities,
        )
    except ReleaseError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    db.refresh(item)
    return _item_payload(item)


@router.post("/{item_id}/revision")
def revision(
    item_id: uuid.UUID,
    payload: RevisionBumpRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown item")
    try:
        bump_revision(db, item, payload.drawing_revision.strip(), user)
    except ReleaseError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return _item_payload(item)
