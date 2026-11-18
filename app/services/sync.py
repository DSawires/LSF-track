"""A cheap answer to "has anything the item list is drawn from changed?"

Every phone refreshes its item list every thirty seconds, and that list is a
replay of the whole log for every active item. On a quiet floor nearly every one
of those replays produces exactly what the phone already holds.

`items_token` fingerprints the inputs of that replay -- the log's size and
newest arrival, the item and step rows, the icons, the stage rows -- with a
handful of narrow queries and no replay at all. It travels in /api/reference;
a phone whose stored token matches skips /api/items entirely.

This is not a projection and stores nothing: it is recomputed from the tables on
every call, so it cannot drift from them. The one thing it cannot see is time
passing (ages on the item cards grow whether or not anyone logs), which is why
the client also refetches on a timer -- see `sync()` in static/js/app.js.
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Event, Item, ItemImage, ItemStep, Stage


def items_token(db: Session) -> str:
    digest = hashlib.sha256()

    def feed(label: str, rows) -> None:
        digest.update(label.encode())
        for row in rows:
            digest.update(repr(tuple(row)).encode())

    # Inserts raise both numbers; a purge lowers the count.
    feed("events", db.execute(sa.select(sa.func.count(Event.id), sa.func.max(Event.received_at))))
    feed(
        "items",
        db.execute(
            sa.select(
                Item.id,
                Item.code,
                Item.project_id,
                Item.area_id,
                Item.phase_id,
                Item.description,
                Item.dimensions,
                Item.is_active,
                Item.total_qty,
                Item.sample_qty,
                Item.drawing_revision,
            ).order_by(Item.id)
        ),
    )
    feed(
        "steps",
        db.execute(
            sa.select(ItemStep.item_id, ItemStep.seq, ItemStep.stage_id, ItemStep.id).order_by(
                ItemStep.item_id, ItemStep.seq
            )
        ),
    )
    feed(
        "icons",
        db.execute(
            sa.select(sa.func.count(ItemImage.id), sa.func.max(ItemImage.uploaded_at)).where(
                ItemImage.kind == "icon"
            )
        ),
    )
    # Whatever is on the stage row: a position's name, and whether it is
    # overdue, are read off it.
    feed(
        "stages",
        db.execute(
            sa.select(
                Stage.id, Stage.code, Stage.name, Stage.sort_order, Stage.max_days_in_state
            ).order_by(Stage.id)
        ),
    )
    return digest.hexdigest()[:20]
