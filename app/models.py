from __future__ import annotations

import uuid
from datetime import datetime

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
    # False = a whole-lot stage: a move into it must take everything resting
    # at the place it comes from, never split it.
    allows_partial_qty: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    # Legacy only. It used to mark the stage that closed a sequence; there are
    # no sequences now. It is still read when replaying rows that carry a
    # legacy state: "completed" at a terminal stage was Done.
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
    """Legacy: queued / in_progress / completed.

    Nothing writes a state any more -- a unit is at a stage or it is not. The
    table stays because events logged before that change carry a state_id, and
    the log must keep replaying them: app/ledger.py gives each
    legacy state its own slot inside the step, ordered by `sort_order`, and
    reads `is_complete` at the last step as Done.
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
    # Moves units off the end of the item's sequence into Done. Carries no step:
    # Done is not a stage, it is where every sequence ends.
    is_finish: Mapped[bool] = mapped_column(sa.Boolean, default=False, server_default=sa.false())
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


class Area(Base):
    """Where in a client's job a piece belongs: guest rooms, the spa, the
    all-day dining room. Each project has its own list, which the office edits
    at runtime -- nothing in the application branches on a name, and "Spa" in
    one hotel is not "Spa" in another.
    """

    __tablename__ = "areas"
    __table_args__ = (sa.UniqueConstraint("project_id", "name", name="uq_areas_project_name"),)

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(sa.String(120))
    sort_order: Mapped[int] = mapped_column(sa.Integer, default=0)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)


class Phase(Base):
    """A phase of a job: which delivery a batch is being built for. The same
    kind of list as `areas`, per project, edited in the same place.
    """

    __tablename__ = "phases"
    __table_args__ = (sa.UniqueConstraint("project_id", "name", name="uq_phases_project_name"),)

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(sa.String(120))
    sort_order: Mapped[int] = mapped_column(sa.Integer, default=0)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)


class Item(Base):
    """A batch of identical pieces.

    What makes it *this* batch is four things together: project, code, area and
    phase. The same code can be several items in one project -- the king bed
    base for the phase 1 rooms and for the phase 2 rooms are separate batches,
    each with its own quantity, its own sample and its own history. Area and
    phase are optional; an item with neither is simply "the" item of that code
    in its project. Uniqueness is enforced in app/services/items.py rather
    than by a constraint, because a NULL area or phase is part of the identity
    and a unique index treats NULLs as never equal.

    Deliberately carries no derived production state: no current_stage, no
    current_status, no is_complete. Those come from the event log.

    There is no released/unreleased state either. Creating an item IS the
    handoff to the floor: it is loggable from that moment, at any stage, and
    `created_at` is when production started counting. An item has no stages of
    its own -- no sequence, no route. Where its units have been is whatever
    the log says.

    `drawing_revision` is authored by the technical office rather than derived, and
    every change to it is also written to the log as a revision-bump event.

    Every item starts as a sample: only `sample_qty` units exist on the floor
    until the sample has been logged Done, and at that moment
    `total_qty` -- the production batch, on top of the sample -- is released
    into "not started". The release is derived from the log by the ledger, never
    stored. `sample_qty` 0 means no sample (items that predate the sample flow).
    """

    __tablename__ = "items"

    id: Mapped[uuid.UUID] = _pk()
    code: Mapped[str] = mapped_column(sa.String(64), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("projects.id"), index=True)
    area_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("areas.id"), nullable=True, index=True
    )
    phase_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("phases.id"), nullable=True, index=True
    )
    description: Mapped[str] = mapped_column(sa.String(255))
    # Archived items keep their history but disappear from lists and reports,
    # and can be reactivated. Deleting one for good (an admin purge) takes its
    # events with it.
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, server_default=sa.true())
    total_qty: Mapped[int] = mapped_column(sa.Integer)
    sample_qty: Mapped[int] = mapped_column(sa.Integer, default=1)
    drawing_revision: Mapped[str] = mapped_column(sa.String(32))

    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    project: Mapped[Project] = relationship(back_populates="items")
    steps: Mapped[list[ItemStep]] = relationship(
        back_populates="item", order_by="ItemStep.seq"
    )


class ItemStep(Base):
    """Legacy: one stage in an item's own ordered sequence.

    Items no longer have sequences -- units move freely between stages, and an
    entry names the stage directly (`events.stage_id`). Nothing creates these
    rows any more. They stay because entries logged before that change point
    at one (`events.item_step_id`), and replaying those needs the order they
    were logged against: such a row took its units from the nearest step
    before it. See app/ledger.py.
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
    """The log, and the source of truth: current state is derived from it.

    Rows are never edited. A mistake in one entry is superseded by a correction
    event, which keeps the history of what was believed and when. Rows can be
    deleted, but only whole: an admin purging an item takes its events with it
    (app/api/items.py), and that is logged with who did it.
    """

    __tablename__ = "events"

    # Generated on the client so a retry after a lost response is a no-op.
    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("items.id"), index=True)
    # Where the units are now: a stage. NULL on a finish entry (the units are
    # Done) and on item-level entries (a revision bump, a correction).
    stage_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("stages.id"), nullable=True, index=True
    )
    # Legacy only: rows logged while items had sequences named a step, not a
    # stage. New rows leave it NULL.
    item_step_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("item_steps.id"), nullable=True, index=True
    )
    station_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("stations.id"), nullable=True, index=True
    )
    event_type_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("event_types.id"))
    # Legacy only: rows logged before states were removed. New rows leave it NULL.
    state_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("event_states.id"), nullable=True
    )
    qty: Mapped[int] = mapped_column(sa.Integer, default=0)
    # Where the units came from: a stage, "not started", or Done -- exactly
    # one of the three on every entry that moves units. Together with the
    # target above this is the movement itself, kept on the row: from -> to,
    # how many, when, who. Units move freely, so the log is the only record of
    # the path a batch actually took.
    from_stage_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("stages.id"), nullable=True
    )
    from_unstarted: Mapped[bool] = mapped_column(
        sa.Boolean, default=False, server_default=sa.false()
    )
    from_done: Mapped[bool] = mapped_column(
        sa.Boolean, default=False, server_default=sa.false()
    )
    # Legacy only: a sequence-era rework row naming the step it took from.
    # Rows with no source at all are older still, and replay by taking the
    # nearest units before (or, for rework, after) their step.
    from_item_step_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("item_steps.id"), nullable=True
    )
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

    Insert-only: "who put the factory on red, and when" is worth keeping, and
    an UPDATE would erase it.
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
