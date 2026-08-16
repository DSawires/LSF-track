from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user
from app.models import Event, User
from app.schemas import EventBatch, EventCreate
from app.services.events import EventRejected, record_event

router = APIRouter(prefix="/api/events", tags=["events"])


def _event_payload(event: Event) -> dict:
    return {
        "id": str(event.id),
        "item_id": str(event.item_id),
        "item_step_id": str(event.item_step_id) if event.item_step_id else None,
        "station_id": str(event.station_id) if event.station_id else None,
        "event_type_id": str(event.event_type_id),
        "state_id": str(event.state_id) if event.state_id else None,
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
    results = []
    for entry in sorted(payload.events, key=lambda e: e.occurred_at):
        try:
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
    return {"results": results}
