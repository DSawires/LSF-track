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
    # The same name in Arabic, shown when a phone is set to Arabic. Optional:
    # without it the stage is shown by `name` in both languages.
    name_ar: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
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
    name_ar: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)  # see Stage.name_ar
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

    Parts. An item can be split into parts that go through the factory on
    their own -- a bed into its frame, headboard and four legs -- and come back
    together at assembly. A part is an item in its own right (its own code, its
    own log), with `parent_item_id` naming what it is part of and `per_unit`
    saying how many of it go into one piece. Parts can have parts. Units only
    ever reach a part through a split, and leave it through an assembly: see
    the linked entries on `Event`.

    `sample_item_id` is set on a batch split off into another phase before its
    sample was done: that batch has no sample of its own, and is released the
    moment the other item's sample is.
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
    # As the drawing gives them, free text ("W 600 x D 450 x H 720 mm"). Never
    # parsed. It is what a work order prints in its dimensions column.
    dimensions: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    # Archived items keep their history but disappear from lists and reports,
    # and can be reactivated. Deleting one for good (an admin purge) takes its
    # events with it.
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, server_default=sa.true())
    total_qty: Mapped[int] = mapped_column(sa.Integer)
    sample_qty: Mapped[int] = mapped_column(sa.Integer, default=1)
    drawing_revision: Mapped[str] = mapped_column(sa.String(32))
    parent_item_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("items.id"), nullable=True, index=True
    )
    per_unit: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    sample_item_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("items.id"), nullable=True
    )

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
    # "snag" (a defect photo), "icon" (the item's thumbnail; latest wins), or
    # "document" (a picture put on a work order in place of the icon).
    kind: Mapped[str] = mapped_column(sa.String(16), default="snag", server_default="snag")
    filename: Mapped[str] = mapped_column(sa.String(255))
    content_type: Mapped[str] = mapped_column(sa.String(64))
    size_bytes: Mapped[int] = mapped_column(sa.Integer)
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(default=utcnow)
    note: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)


class WorkOrder(Base):
    """A numbered document releasing a set of one project's items to production.

    A document, not a state. Issuing one changes nothing about an item: it is
    loggable the moment it exists, with or without a work order, and no report
    reads this table. What it holds is what was put on paper -- so its lines
    carry their own copy of each item's text (see `WorkOrderLine`), and editing
    an item afterwards never rewrites a work order already on the floor.

    Editing a work order re-issues it: `revision` goes up and `issued_at` /
    `issued_by_user_id` are stamped again. Earlier revisions are not kept here;
    the printed PDF carries its revision number.
    """

    __tablename__ = "work_orders"

    id: Mapped[uuid.UUID] = _pk()
    # WO-0001, WO-0002, ... across the factory.
    number: Mapped[int] = mapped_column(sa.Integer, unique=True, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(sa.String(160), default="")
    notes: Mapped[str] = mapped_column(sa.Text, default="")
    revision: Mapped[int] = mapped_column(sa.Integer, default=1)
    # Which optional columns the document prints.
    show_picture: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    show_description: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    show_dimensions: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    show_location: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    # What the paper is printed in: 'en', 'ar' or 'both'. A fixed set like the
    # banner's colours -- it is what the renderer can draw, not vocabulary the
    # factory owns -- and nothing but the PDF reads it.
    language: Mapped[str] = mapped_column(sa.String(8), default="en", server_default="en")
    # The Arabic wording, typed or machine-translated and then corrected. Kept
    # beside the English, never instead of it.
    title_ar: Mapped[str] = mapped_column(sa.String(160), default="", server_default="")
    notes_ar: Mapped[str] = mapped_column(sa.Text, default="", server_default="")
    issued_at: Mapped[datetime] = mapped_column(default=utcnow)
    issued_by_user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    lines: Mapped[list[WorkOrderLine]] = relationship(
        order_by="WorkOrderLine.seq", cascade="all, delete-orphan"
    )


class WorkOrderLine(Base):
    """One item on a work order, as it was written there.

    Everything printed is the line's own. Each field starts from the item's --
    code, description, dimensions, quantity, area, phase, drawing revision --
    and every one of them can be changed for this document alone, without
    touching the item. `description_ar` is the description in Arabic.
    `item_id` and `image_id` are kept so the line can be reopened against its
    item and its picture, and both may go: an item deleted for good, or a
    photo removed, leaves the line with its text and no link.
    """

    __tablename__ = "work_order_lines"

    id: Mapped[uuid.UUID] = _pk()
    work_order_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("work_orders.id"), index=True)
    seq: Mapped[int] = mapped_column(sa.Integer)
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("items.id"), nullable=True, index=True
    )
    image_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("item_images.id"), nullable=True, index=True
    )
    code: Mapped[str] = mapped_column(sa.String(64))
    description: Mapped[str] = mapped_column(sa.Text, default="")
    description_ar: Mapped[str] = mapped_column(sa.Text, default="", server_default="")
    dimensions: Mapped[str] = mapped_column(sa.String(255), default="")
    qty: Mapped[int] = mapped_column(sa.Integer)
    area: Mapped[str] = mapped_column(sa.String(120), default="")
    phase: Mapped[str] = mapped_column(sa.String(120), default="")
    drawing_revision: Mapped[str] = mapped_column(sa.String(32), default="")


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
    # Linked entries: units leaving this item for another, or arriving from
    # one. A split into parts, an assembly, or units re-filed into another
    # phase is a group of rows sharing `link_id` -- one per item it touches,
    # each naming its own place and quantity, stored all together or not at
    # all. `to_link` is a destination (the units leave this item);
    # `from_link` is a source (they arrive from the other side of the group).
    link_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True, index=True)
    from_link: Mapped[bool] = mapped_column(sa.Boolean, default=False, server_default=sa.false())
    to_link: Mapped[bool] = mapped_column(sa.Boolean, default=False, server_default=sa.false())
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
