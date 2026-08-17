from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, selectinload

from app.db import get_db, utcnow
from app.deps import current_user, require_admin
from app.models import (
    Event,
    EventType,
    Item,
    ItemImage,
    ItemStep,
    Project,
    RouteTemplate,
    Stage,
    User,
)
from app.schemas import ItemCreate, ItemUpdate, ReleaseRequest, RevisionBumpRequest
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
        "is_active": item.is_active,
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
    include_archived: bool = False,
) -> dict:
    query = sa.select(Item).options(selectinload(Item.steps)).order_by(Item.code)
    if not include_archived:
        query = query.where(Item.is_active.is_(True))
    if project_id is not None:
        query = query.where(Item.project_id == project_id)
    if q:
        # Escape LIKE wildcards so a literal % or _ in the search box matches
        # itself instead of exploding into a scan.
        cleaned = q.strip().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        needle = f"%{cleaned}%"
        query = query.where(
            Item.code.ilike(needle, escape="\\")
            | Item.description.ilike(needle, escape="\\")
        )
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
    offset: int = 0,
) -> dict:
    """Most recent events for an item, newest first, with offset paging so
    older history stays reachable.

    This is what the phone shows under "recent entries", and where a correction
    picks the event it supersedes.
    """
    # Function-local to break the router import cycle (events imports nothing
    # from items, but both are imported by main before either is complete).
    from app.api.events import _event_payload

    if db.get(Item, item_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown item")
    total = db.scalar(
        sa.select(sa.func.count()).select_from(Event).where(Event.item_id == item_id)
    )
    rows = db.scalars(
        sa.select(Event)
        .where(Event.item_id == item_id)
        .order_by(Event.occurred_at.desc(), Event.received_at.desc())
        .offset(max(offset, 0))
        .limit(min(max(limit, 1), 100))
    )
    return {"events": [_event_payload(e) for e in rows], "total": total}


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


@router.patch("/{item_id}")
def update_item(
    item_id: uuid.UUID,
    payload: ItemUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    """Correct an item's office-side facts: code, description, project, batch
    size, target date. Admin-only: a code or batch size is what everyone else
    reads the floor by, and a quiet edit to one re-labels work already logged.
    Logging events, releasing and bumping a revision stay open to any engineer
    -- those append to the log rather than rewriting what it refers to.

    The drawing revision is deliberately not editable here -- a revision is a
    dated fact about what production was told to build, so it moves by a bump
    event, never by a quiet field edit. `total_qty` cannot be cut below what
    the log has already moved out of 'not started': the log wins over a typed
    number, and a negative unstarted count is not a thing that can be true.
    """
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown item")

    fields = payload.model_dump(exclude_unset=True)

    if "code" in fields:
        code = (fields["code"] or "").strip()
        if not code:
            raise HTTPException(422, "item code is required")
        clash = db.scalars(
            sa.select(Item).where(Item.code == code, Item.id != item_id)
        ).first()
        if clash is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, f"item {code} already exists")
        fields["code"] = code

    if "project_id" in fields and db.get(Project, fields["project_id"]) is None:
        raise HTTPException(422, "unknown project")

    if "description" in fields:
        fields["description"] = (fields["description"] or "").strip()

    if "total_qty" in fields:
        state = reports.item_state(db, [item.id]).get(str(item.id))
        committed = item.total_qty - state["unstarted_qty"] if state else 0
        if fields["total_qty"] < committed:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{committed} pcs have already been logged into production; "
                f"the batch cannot be smaller than that",
            )

    for field, value in fields.items():
        setattr(item, field, value)
    db.flush()
    return _item_payload(item, _icon_urls(db, [item.id]).get(item.id))


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


@router.delete("/{item_id}")
def remove_item(
    item_id: uuid.UUID,
    purge: bool = False,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    """Archive an item with history; hard-delete one that never saw the floor.

    The event log is append-only, so an item that has events is never destroyed
    -- it is deactivated, which removes it from every list and report while its
    history stays intact and derivable.

    `purge=true` is the deliberate exception, for items that should never have
    existed at all (a mistyped duplicate, a test batch): it destroys the item's
    events along with it. This is the one operation in the system that erases
    log rows, it is admin-only, and it is irreversible -- archiving is what you
    want for anything that was ever really built.
    """
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown item")

    has_events = (
        db.scalar(
            sa.select(sa.func.count()).select_from(Event).where(Event.item_id == item_id)
        )
        > 0
    )
    if has_events and purge:
        item_events = sa.select(Event.id).where(Event.item_id == item_id)
        purged = db.scalar(
            sa.select(sa.func.count()).select_from(Event).where(Event.item_id == item_id)
        )
        # A correction points at the event it supersedes, so the log is a graph
        # and not a list: deleting a superseded row while its correction still
        # references it trips events_supersedes_event_id_fkey. Cut the links
        # first -- including any reaching in from another item's corrections --
        # and the rows then go in one statement, in whatever order the database
        # likes. `synchronize_session=False` because nothing reads these
        # objects again; the commit at the end of the request expires them.
        db.execute(
            sa.update(Event)
            .where(Event.supersedes_event_id.in_(item_events))
            .values(supersedes_event_id=None)
            .execution_options(synchronize_session=False)
        )
        db.execute(
            sa.delete(Event)
            .where(Event.item_id == item_id)
            .execution_options(synchronize_session=False)
        )
        db.flush()
        _destroy_item(db, item)
        return {"archived": False, "deleted": True, "purged_events": purged}

    if has_events:
        item.is_active = False
        # Archiving removes the item's quantities from WIP; that must be a
        # fact in the log (who, when), not a silent flag flip.
        archive_type = db.scalars(
            sa.select(EventType)
            .where(EventType.is_archive.is_(True), EventType.is_active.is_(True))
            .limit(1)
        ).first()
        if archive_type is not None:
            db.add(
                Event(
                    id=uuid.uuid4(),
                    item_id=item.id,
                    event_type_id=archive_type.id,
                    qty=0,
                    occurred_at=utcnow(),
                    received_at=utcnow(),
                    user_id=admin.id,
                    submitted_by_user_id=admin.id,
                    note="item archived",
                )
            )
        db.flush()
        return {"archived": True, "deleted": False}

    _destroy_item(db, item)
    return {"archived": False, "deleted": True, "purged_events": 0}


def _destroy_item(db: Session, item: Item) -> None:
    """Photos off storage, steps and the row out of the database."""
    from app.storage import get_storage

    storage = get_storage()
    for image in db.scalars(sa.select(ItemImage).where(ItemImage.item_id == item.id)):
        storage.delete(f"images/{image.id}")
        db.delete(image)
    for step in db.scalars(sa.select(ItemStep).where(ItemStep.item_id == item.id)):
        db.delete(step)
    db.delete(item)
    db.flush()


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
