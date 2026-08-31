from __future__ import annotations

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    username: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(sa.String(128))
    password_hash: Mapped[str] = mapped_column(sa.String(255))
    is_admin: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Stamped by the login endpoint. Not derivable from the log -- a sign-in
    # is not an event about an item -- so it is a column, and the only piece
    # of account state the status page cannot compute.
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _pk()
    code: Mapped[str] = mapped_column(sa.String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(sa.String(160))
    client: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    items: Mapped[list[Item]] = relationship(back_populates="project")


class Stage(Base):
    """A step type. Rows are added at runtime by non-developers.

    Every behavioural difference between stages is a column here. Nothing in the
    application may branch on `code` -- see CLAUDE.md rule 2.
    """

    __tablename__ = "stages"

    id: Mapped[uuid.UUID] = _pk()
    code: Mapped[str] = mapped_column(sa.String(48), unique=True, index=True)
    name: Mapped[str] = mapped_column(sa.String(120))
    sort_order: Mapped[int] = mapped_column(sa.Integer, default=0)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)

    # Behaviour flags. Add columns here rather than branching on the code.
    requires_station: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    requires_external_po: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    allows_partial_qty: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    is_terminal: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    # Days in one state at this stage before the aging report and item cards
    # flag it. NULL = no opinion (outsourced work sits for weeks by design).
    max_days_in_state: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    stations: Mapped[list[Station]] = relationship(back_populates="stage")


class Station(Base):
    __tablename__ = "stations"

    id: Mapped[uuid.UUID] = _pk()
    stage_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("stages.id"), index=True)
    code: Mapped[str] = mapped_column(sa.String(48), unique=True, index=True)
    name: Mapped[str] = mapped_column(sa.String(120))
    sort_order: Mapped[int] = mapped_column(sa.Integer, default=0)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)

    stage: Mapped[Stage] = relationship(back_populates="stations")


class EventState(Base):
    """queued / in_progress / completed, as data.

    `sort_order` defines progression within a step; `is_complete` marks the state
    that hands units on to the next step. Nothing else is assumed about them, so a
    factory can add a fourth state without a deploy.
    """

    __tablename__ = "event_states"

    id: Mapped[uuid.UUID] = _pk()
    code: Mapped[str] = mapped_column(sa.String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(sa.String(64))
    sort_order: Mapped[int] = mapped_column(sa.Integer, default=0)
    is_complete: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    # The state new work enters a step in (the queue). A flag rather than
    # "lowest sort_order", so inserting a state that sorts before the queue
    # cannot silently change what auto-queue and queue-depth mean.
    is_initial: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)


class EventType(Base):
    """Table-driven event vocabulary.

    The derivation in app/ledger.py branches on these flags and never on `code`,
    which is what lets a new event type be added the same way a stage is.
    """

    __tablename__ = "event_types"

    id: Mapped[uuid.UUID] = _pk()
    code: Mapped[str] = mapped_column(sa.String(48), unique=True, index=True)
    name: Mapped[str] = mapped_column(sa.String(120))
    sort_order: Mapped[int] = mapped_column(sa.Integer, default=0)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)

    # Behaviour flags.
    is_correction: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    is_rework: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    # The item-level events the server itself writes. Flagged rather than looked
    # up by code, so the server never depends on a particular spelling.
    # is_release is historical: nothing writes it any more, because creating an
    # item IS the handoff to the floor and `items.created_at` records it. The
    # flag and its seeded row stay so the release events already in the log
    # still resolve to a type -- they move no quantity and need no step, so the
    # ledger replays them as the no-ops they always were.
    is_release: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    is_revision_bump: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    is_archive: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    moves_quantity: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    requires_item_step: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    requires_reason_code: Mapped[bool] = mapped_column(sa.Boolean, default=False)


class ReasonCode(Base):
    __tablename__ = "reason_codes"

    id: Mapped[uuid.UUID] = _pk()
    code: Mapped[str] = mapped_column(sa.String(48), unique=True, index=True)
    name: Mapped[str] = mapped_column(sa.String(160))
    sort_order: Mapped[int] = mapped_column(sa.Integer, default=0)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)


class Item(Base):
    """A batch of identical pieces.

    Deliberately carries no derived production state: no current_stage, no
    current_status, no is_complete. Those come from the event log.

    There is no released/unreleased state either. Creating an item IS the
    handoff to the floor: it is given its stages and is loggable from that
    moment, and `created_at` is when production started counting.

    `drawing_revision` is authored by the technical office rather than derived, and
    every change to it is also written to the log as a revision-bump event.
    """

    __tablename__ = "items"

    id: Mapped[uuid.UUID] = _pk()
    code: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("projects.id"), index=True)
    description: Mapped[str] = mapped_column(sa.String(255))
    # Archived items keep their history (the log is append-only) but disappear
    # from lists and reports. Only an item with no events may be hard-deleted.
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, server_default=sa.true())
    total_qty: Mapped[int] = mapped_column(sa.Integer)
    drawing_revision: Mapped[str] = mapped_column(sa.String(32))
    target_release_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    project: Mapped[Project] = relationship(back_populates="items")
    steps: Mapped[list[ItemStep]] = relationship(
        back_populates="item", order_by="ItemStep.seq"
    )


class ItemStep(Base):
    """One stage in this item's own production sequence.

    Chosen per item when the item is created -- either by picking stages, or by
    copying the sequence off a sibling item in the same project. There is no
    shared template behind it: two items only share a sequence because someone
    copied one, and editing one item's stages can never touch another's.

    Rewritable until the floor logs against the item, frozen from then on: once
    events reference a step, the step is what that history means. Seq numbers go
    in tens so a stage can be slotted between two existing ones.
    """

    __tablename__ = "item_steps"
    __table_args__ = (sa.UniqueConstraint("item_id", "seq", name="uq_item_step_seq"),)

    id: Mapped[uuid.UUID] = _pk()
    item_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("items.id"), index=True)
    seq: Mapped[int] = mapped_column(sa.Integer)
    stage_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("stages.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    item: Mapped[Item] = relationship(back_populates="steps")
    stage: Mapped[Stage] = relationship()


class ItemImage(Base):
    """A photo attached to an item. The file lives on disk under LSF_UPLOAD_DIR;
    this row is the authoritative record of what was uploaded and by whom."""

    __tablename__ = "item_images"

    id: Mapped[uuid.UUID] = _pk()
    item_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("items.id"), index=True)
    # "snag" (a defect photo) or "icon" (the item's thumbnail; latest wins).
    kind: Mapped[str] = mapped_column(sa.String(16), default="snag", server_default="snag")
    filename: Mapped[str] = mapped_column(sa.String(255))
    content_type: Mapped[str] = mapped_column(sa.String(64))
    size_bytes: Mapped[int] = mapped_column(sa.Integer)
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(default=utcnow)
    note: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)


class Event(Base):
    """The log. Insert only: no UPDATE, no DELETE, anywhere, ever.

    Mistakes are superseded by a correction event, not edited.
    """

    __tablename__ = "events"

    # Generated on the client so a retry after a lost response is a no-op.
    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("items.id"), index=True)
    item_step_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("item_steps.id"), nullable=True, index=True
    )
    station_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("stations.id"), nullable=True, index=True
    )
    event_type_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("event_types.id"))
    state_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("event_states.id"), nullable=True
    )
    qty: Mapped[int] = mapped_column(sa.Integer, default=0)
    reason_code_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("reason_codes.id"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(index=True)
    received_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    # user_id is the engineer who saw the work happen (client-claimed, for shared
    # floor devices); submitted_by_user_id is the authenticated session that
    # actually posted the row. Nullable only because rows predate the column.
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    supersedes_event_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("events.id"), nullable=True, index=True
    )

    item: Mapped[Item] = relationship()
    event_type: Mapped[EventType] = relationship()
    state: Mapped[EventState | None] = relationship()
    station: Mapped[Station | None] = relationship()
    reason_code: Mapped[ReasonCode | None] = relationship()

    __table_args__ = (
        sa.Index("ix_events_item_occurred", "item_id", "occurred_at"),
        sa.Index("ix_events_station_occurred", "station_id", "occurred_at"),
        sa.Index("ix_events_step_occurred", "item_step_id", "occurred_at"),
    )


class StatusBanner(Base):
    """The site-wide notice an admin raises for the floor. Latest row wins.

    Insert-only, like the event log and for the same reason: "who put the
    factory on red, and when" is worth keeping, and an UPDATE would erase it.
    Clearing the banner is a new row with colour `neutral` and no message.

    `colour` is presentation, not factory vocabulary: it is the four things the
    stylesheet can paint, fixed by the UI rather than owned by the factory, so
    it is deliberately NOT a lookup table. Nothing branches on it server-side.
    """

    __tablename__ = "status_banners"

    id: Mapped[uuid.UUID] = _pk()
    color: Mapped[str] = mapped_column(sa.String(16))
    message: Mapped[str] = mapped_column(sa.String(200), default="")
    set_by_user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"))
    set_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
