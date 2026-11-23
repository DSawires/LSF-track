from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user
from app.models import Event, Item, User
from app.schemas import EventBatch, EventCreate
from app.services.events import EventRejected, record_event, record_group

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])


def _event_payload(event: Event) -> dict:
    return {
        "id": str(event.id),
        "item_id": str(event.item_id),
        # Where the units are now (empty on a finish: they are Done), and where
        # they came from. The *_step_id pair is the sequence-era shape.
        "stage_id": str(event.stage_id) if event.stage_id else None,
        "from_stage_id": str(event.from_stage_id) if event.from_stage_id else None,
        "from_unstarted": bool(event.from_unstarted),
        "from_done": bool(event.from_done),
        "item_step_id": str(event.item_step_id) if event.item_step_id else None,
        "from_item_step_id": (
            str(event.from_item_step_id) if event.from_item_step_id else None
        ),
        "station_id": str(event.station_id) if event.station_id else None,
        "event_type_id": str(event.event_type_id),
        "state_id": str(event.state_id) if event.state_id else None,
        # Legacy rows only: the reference payload no longer carries states, so
        # the name travels with the event for the history list to show.
        "state_name": event.state.name if event.state_id and event.state else None,
        "qty": event.qty,
        "reason_code_id": str(event.reason_code_id) if event.reason_code_id else None,
        "occurred_at": event.occurred_at.isoformat(),
        "received_at": event.received_at.isoformat(),
        "user_id": str(event.user_id),
        "submitted_by_user_id": (
            str(event.submitted_by_user_id) if event.submitted_by_user_id else None
        ),
        "note": event.note,
        "supersedes_event_id": (
            str(event.supersedes_event_id) if event.supersedes_event_id else None
        ),
        "link_id": str(event.link_id) if event.link_id else None,
        "from_link": bool(event.from_link),
        "to_link": bool(event.to_link),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def post_event(
    payload: EventCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Idempotent on the client-generated id: replaying is safe and returns 201
    either way, so a client that lost the first response cannot tell the
    difference -- which is the point."""
    try:
        result = record_event(db, payload, user)
    except EventRejected as exc:
        raise HTTPException(
            422,
            {"reason": exc.reason, "field": exc.field},
        )
    return {
        "event": _event_payload(result.event),
        "created": result.created,
        "divergent": result.divergent,
    }


def _occurred_sort_key(entry: EventCreate) -> datetime:
    # An entry whose timestamp has no timezone is refused on its own a few lines
    # down, with a reason. It must not get there by making the sort raise --
    # naive and aware datetimes do not compare -- and taking the whole batch
    # down with a 500.
    moment = entry.occurred_at
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


@router.post("/batch")
def post_batch(
    payload: EventBatch,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Drain a sync queue in one request.

    Per-event outcomes, never all-or-nothing: one malformed entry must not hold
    the rest of the queue hostage on a flaky connection. The client clears each
    queue entry whose id comes back as stored/duplicate, and quarantines rejects.

    Entries are applied in occurred_at order, not arrival order, so an offline
    session's own sequence (complete carpentry, then queue at veneer) validates
    against itself no matter how the queue was assembled. Clients match results
    by id, so the reordering is invisible to them.
    """
    # Lock every item this batch touches, once and in id order, before the
    # first entry: record_event locks each item as it goes, and two phones
    # draining queues that name the same items in different orders would
    # otherwise be able to deadlock each other.
    item_ids = sorted({entry.item_id for entry in payload.events}, key=str)
    if item_ids:
        db.execute(
            sa.select(Item.id).where(Item.id.in_(item_ids)).order_by(Item.id).with_for_update()
        ).all()

    results = []
    # A split, an assembly or a phase move is a group of rows sharing a
    # link_id. It is stored whole or not at all, at the place of its first row.
    groups: dict = {}
    for entry in payload.events:
        if entry.link_id is not None:
            groups.setdefault(entry.link_id, []).append(entry)
    for entry in sorted(payload.events, key=_occurred_sort_key):
        if entry.link_id is not None:
            group = groups.pop(entry.link_id, None)
            if group is not None:
                results.extend(_store_group(db, group, user))
            continue
        try:
            # A SAVEPOINT per entry: an unexpected failure on one event must
            # neither 500 the whole drain (rolling back events the client is
            # about to be told were stored) nor poison the session for the
            # entries after it.
            with db.begin_nested():
                result = record_event(db, entry, user)
            results.append(
                {
                    "id": str(entry.id),
                    "status": "stored" if result.created else "duplicate",
                    "divergent": result.divergent,
                }
            )
        except EventRejected as exc:
            results.append(
                {
                    "id": str(entry.id),
                    "status": "rejected",
                    "reason": exc.reason,
                    "field": exc.field,
                }
            )
        except Exception:
            log.exception("unexpected failure storing event %s in a batch", entry.id)
            # "error", not "rejected": the client keeps the entry pending and
            # retries next sync instead of quarantining it for a server bug.
            results.append(
                {
                    "id": str(entry.id),
                    "status": "error",
                    "reason": "server error storing this entry; it will be retried",
                }
            )
    return {"results": results}


def _store_group(db: Session, group: list[EventCreate], user: User) -> list[dict]:
    try:
        with db.begin_nested():
            written = record_group(db, group, user)
        return [
            {"id": str(row.id), "status": "stored" if result.created else "duplicate",
             "divergent": result.divergent}
            for row, result in zip(group, written)
        ]
    except EventRejected as exc:
        # Every row of the group carries the reason: the phone quarantines
        # them together, and a retry sends them together.
        return [
            {"id": str(row.id), "status": "rejected", "reason": exc.reason, "field": exc.field}
            for row in group
        ]
    except Exception:
        log.exception("unexpected failure storing linked entries %s", group[0].link_id)
        return [
            {"id": str(row.id), "status": "error",
             "reason": "server error storing this entry; it will be retried"}
            for row in group
        ]
