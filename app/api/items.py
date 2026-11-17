from __future__ import annotations

import logging
import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, selectinload

from app.db import get_db, utcnow
from app.deps import current_user, require_admin
from app.models import (
    Event,
    Item,
    ItemImage,
    ItemStep,
    Project,
    User,
)
from app.schemas import ItemCreate, ItemStepsUpdate, ItemUpdate, RevisionBumpRequest
from app.services import reports
from app.services.items import (
    ItemSetupError,
    archive_item,
    assign_steps,
    bump_revision,
    place_initial_quantities,
    references_a_step,
    steps_are_frozen,
)
from app.services.sync import items_token

log = logging.getLogger(__name__)


def _locked_item_ids(db: Session, item_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these items the floor has already logged against, in one query.

    An item is locked once an event points at one of its steps; item-level
    events (a revision bump) leave the sequence editable.
    """
    if not item_ids:
        return set()
    return set(
        db.scalars(
            sa.select(Event.item_id)
            .where(Event.item_id.in_(item_ids), references_a_step())
            .distinct()
        )
    )

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


def _item_payload(item: Item, icon_url: str | None = None, steps_locked: bool = False) -> dict:
    return {
        "id": str(item.id),
        "icon_url": icon_url,
        "is_active": item.is_active,
        "code": item.code,
        "project_id": str(item.project_id),
        "description": item.description,
        "total_qty": item.total_qty,
        "sample_qty": item.sample_qty,
        "drawing_revision": item.drawing_revision,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        # Whether the floor has logged against these stages yet, which is what
        # decides if the sequence can still be corrected.
        "steps_locked": steps_locked,
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
    q: str | None = None,
    include_archived: bool = False,
) -> dict:
    # Taken before the rows are read, never after: a write landing in between
    # then leaves the phone holding newer data under an older token, and its
    # next sync simply fetches again. The other order would let it keep stale
    # data under a token that says it is current.
    token = items_token(db)
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
    items = list(db.scalars(query))
    item_ids = [item.id for item in items]
    state = reports.item_state(db, item_ids) if item_ids else {}
    icons = _icon_urls(db, item_ids)
    locked = _locked_item_ids(db, item_ids)

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
        rows.append({
            **_item_payload(item, icons.get(item.id), steps_locked=item.id in locked),
            "state": item_state,
        })
    return {"items": rows, "token": token}


@router.get("/{item_id}")
def get_item(
    item_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    item = db.get(Item, item_id, options=[selectinload(Item.steps)])
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown item")
    state = reports.item_state(db, [item.id])
    icon = _icon_urls(db, [item.id]).get(item.id)
    return {
        **_item_payload(item, icon, steps_locked=steps_are_frozen(db, item)),
        "state": state.get(str(item.id)),
    }


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
    # Which of these a correction has already superseded. Without it the phone
    # cannot tell a voided entry from a live one: it would list both the same
    # way and offer to void the voided one again.
    superseded = set(
        db.scalars(
            sa.select(Event.supersedes_event_id).where(
                Event.item_id == item_id, Event.supersedes_event_id.is_not(None)
            )
        )
    )
    return {
        "events": [{**_event_payload(e), "superseded": e.id in superseded} for e in rows],
        "total": total,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_item(
    payload: ItemCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Create an item and hand it to the floor in one step.

    There is no separate release: an item exists because someone is building it,
    so it gets its stages here and is loggable immediately -- the sample first,
    the batch once the sample is done. `initial_quantities` is for a batch that
    is already part-built when it reaches the system, and needs `sample_qty` 0.

    Projects normally arrive whole through the file import (app/api/imports.py);
    this is the one-item path underneath it.
    """
    if db.get(Project, payload.project_id) is None:
        raise HTTPException(422, "unknown project")
    code = payload.code.strip()
    existing = db.scalars(sa.select(Item).where(Item.code == code)).first()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"item {code} already exists")

    if not payload.stage_ids:
        raise HTTPException(422, "pick the stages this item goes through")

    item = Item(
        code=code,
        project_id=payload.project_id,
        description=payload.description.strip(),
        total_qty=payload.total_qty,
        sample_qty=payload.sample_qty,
        drawing_revision=payload.drawing_revision.strip(),
    )
    db.add(item)
    db.flush()

    moment = utcnow()
    try:
        steps = assign_steps(db, item, payload.stage_ids, created_at=moment)
        if payload.initial_quantities:
            place_initial_quantities(
                db, item, steps, payload.initial_quantities, user, moment
            )
    except ItemSetupError as exc:
        raise HTTPException(422, str(exc))
    db.refresh(item)
    return _item_payload(item)


@router.put("/{item_id}/steps")
def set_item_steps(
    item_id: uuid.UUID,
    payload: ItemStepsUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Correct the stage sequence of an item nobody has logged against yet.

    Once an event points at one of the steps, the sequence is frozen: the stored
    events mean "this happened at that step", and rewriting the steps under them
    would quietly re-label history. At that point the answer is a correction
    event, or a new item.
    """
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown item")
    if not payload.stage_ids:
        raise HTTPException(422, "an item needs at least one stage")
    try:
        assign_steps(db, item, payload.stage_ids)
    except ItemSetupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    db.refresh(item)
    return _item_payload(item, _icon_urls(db, [item.id]).get(item.id))


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

    `is_active: true` brings an archived item back onto the lists and reports,
    with its history as it was -- archiving is a tap, and a mis-tap must not
    leave "delete for good" as the only button left.
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

    if "is_active" in fields:
        # Only the way back is a field edit. Archiving goes through DELETE,
        # which writes the archive event; a PATCH to false would skip it.
        if fields["is_active"] is not True:
            raise HTTPException(422, "archive an item with DELETE; this only reactivates one")
        if item.is_active:
            del fields["is_active"]
        else:
            project = db.get(Project, fields.get("project_id") or item.project_id)
            if project is not None and not project.is_active:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"project {project.code} is archived; reactivate it first",
                )
            log.info("item %s reactivated by %s", item.code, admin.username)

    if "total_qty" in fields:
        state = reports.item_state(db, [item.id]).get(str(item.id))
        # Batch units only: the sample is not part of total_qty.
        committed = item.total_qty - state["production_unstarted_qty"] if state else 0
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


@router.delete("/{item_id}")
def remove_item(
    item_id: uuid.UUID,
    purge: bool = False,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    """Archive an item with history; hard-delete one that never saw the floor.

    The default is the gentle one: an item that has events is deactivated,
    which removes it from every list and report while its history stays intact
    and derivable, and it can be reactivated (PATCH `is_active`).

    `purge=true` deletes for good: the item, its stages, its photos and every
    event logged against it. Admin-only and irreversible, so it is written to
    the server log with who did it and how much went -- the events table cannot
    record its own deletion.
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
        code = item.code
        _destroy_item(db, item)
        log.warning(
            "item %s (%s) purged by %s: %d event(s) deleted with it",
            code, item_id, admin.username, purged,
        )
        return {"archived": False, "deleted": True, "purged_events": purged}

    if has_events:
        archive_item(db, item, admin)
        return {"archived": True, "deleted": False}

    code = item.code
    _destroy_item(db, item)
    log.info("item %s (%s) deleted by %s; it had no events", code, item_id, admin.username)
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
    except ItemSetupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return _item_payload(item)
