"""Release: the handoff from the technical office to the floor.

Releasing copies the route template's steps onto the item. From that moment the
item's route is frozen: publishing a new version of the template, or inserting a
stage into it, changes nothing for work already in production. That is the whole
reason a stage can be added on a Tuesday afternoon without anyone holding their
breath.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import Event, EventType, Item, ItemStep, RouteTemplate, RouteTemplateStep, User


class ReleaseError(Exception):
    pass


def release_item(
    db: Session,
    item: Item,
    route_template: RouteTemplate,
    user: User,
    drawing_revision: str | None = None,
    released_at: datetime | None = None,
) -> list[ItemStep]:
    if item.is_released:
        raise ReleaseError(f"{item.code} was already released on {item.released_at:%Y-%m-%d}")

    template_steps = list(
        db.scalars(
            sa.select(RouteTemplateStep)
            .where(RouteTemplateStep.route_template_id == route_template.id)
            .order_by(RouteTemplateStep.seq)
        )
    )
    if not template_steps:
        raise ReleaseError(f"route {route_template.code} v{route_template.version} has no steps")

    revision = drawing_revision or item.drawing_revision
    moment = released_at or utcnow()

    steps = []
    for template_step in template_steps:
        step = ItemStep(
            item_id=item.id,
            seq=template_step.seq,
            stage_id=template_step.stage_id,
            created_at=moment,
        )
        db.add(step)
        steps.append(step)

    item.route_template_id = route_template.id
    item.released_at = moment
    item.released_by_user_id = user.id
    item.released_revision = revision
    item.drawing_revision = revision
    db.flush()

    release_type = _event_type(db, "is_release")
    if release_type is not None:
        db.add(
            Event(
                id=uuid.uuid4(),
                item_id=item.id,
                item_step_id=None,
                event_type_id=release_type.id,
                state_id=None,
                qty=0,
                occurred_at=moment,
                received_at=utcnow(),
                user_id=user.id,
                note=f"released at revision {revision} on route "
                f"{route_template.code} v{route_template.version}",
            )
        )
        db.flush()
    return steps


def bump_revision(db: Session, item: Item, revision: str, user: User) -> Event | None:
    """Issue a drawing revision after release.

    The point of recording it is that anything already in flight was built to the
    old drawing. The quantity affected is derived from the log, not stored.
    """
    if not item.is_released:
        raise ReleaseError("only a released item can have a revision bump")
    if revision == item.drawing_revision:
        raise ReleaseError(f"{item.code} is already at revision {revision}")

    previous = item.drawing_revision
    item.drawing_revision = revision

    bump_type = _event_type(db, "is_revision_bump")
    if bump_type is None:
        db.flush()
        return None
    event = Event(
        id=uuid.uuid4(),
        item_id=item.id,
        item_step_id=None,
        event_type_id=bump_type.id,
        state_id=None,
        qty=0,
        occurred_at=utcnow(),
        received_at=utcnow(),
        user_id=user.id,
        note=f"drawing revision {previous} -> {revision}",
    )
    db.add(event)
    db.flush()
    return event


def _event_type(db: Session, flag: str) -> EventType | None:
    """Find the event type carrying a behaviour flag, rather than naming a code."""
    column = getattr(EventType, flag, None)
    if column is None:
        return None
    return db.scalars(
        sa.select(EventType).where(column.is_(True), EventType.is_active.is_(True)).limit(1)
    ).first()
