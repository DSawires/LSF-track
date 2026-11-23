from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class EventCreate(BaseModel):
    """A logged event.

    `id` comes from the phone. It is the idempotency key: the same body posted
    twenty times after twenty failed syncs produces one row.
    """

    id: uuid.UUID
    item_id: uuid.UUID
    event_type_id: uuid.UUID
    occurred_at: datetime
    # Where the units are now. Empty on a finish entry (they are Done) and on
    # item-level entries.
    stage_id: uuid.UUID | None = None
    station_id: uuid.UUID | None = None
    # ge=0: negative movement is a rework/correction event, never a negative
    # qty. The ceiling is far beyond any real batch; it exists so a client bug
    # cannot store a number that breaks every report aggregate.
    qty: int = Field(default=0, ge=0, le=1_000_000)
    # Where they came from: exactly one of these on an entry that moves units.
    from_stage_id: uuid.UUID | None = None
    from_unstarted: bool = False
    from_done: bool = False
    # A linked entry: part of a split into parts, an assembly, or a move into
    # another phase. Every row of the group carries the same link_id and is
    # posted in one batch.
    link_id: uuid.UUID | None = None
    from_link: bool = False
    to_link: bool = False
    # Sequence-era shape, accepted so queues filled before items lost their
    # sequences still drain: a step instead of a stage, and an optional source
    # step (or none at all).
    item_step_id: uuid.UUID | None = None
    from_item_step_id: uuid.UUID | None = None
    state_id: uuid.UUID | None = None
    reason_code_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=2000)
    supersedes_event_id: uuid.UUID | None = None
    # Who logged it on the floor, which is not always who is syncing it now.
    user_id: uuid.UUID | None = None


class EventBatch(BaseModel):
    # A phone that has been offline all day drains a few dozen entries; 500 is
    # far beyond any honest queue and small enough to bound request cost.
    events: list[EventCreate] = Field(default_factory=list, max_length=500)


# String bounds mirror the column widths in app/models.py. Without them an
# over-length value is a DataError -> 500 on PostgreSQL -- which the SQLite
# test suite can never catch, so keep the two in sync by hand.


class ItemCreate(BaseModel):
    """A new item, live on the floor the moment it is created.

    It has no stages of its own: it can be logged at any stage, in any order.
    """

    code: str = Field(min_length=1, max_length=64)
    project_id: uuid.UUID
    description: str = Field(default="", max_length=255)
    total_qty: int = Field(gt=0, le=1_000_000)
    # Units built first, before the batch is released. 0 = no sample.
    sample_qty: int = Field(default=1, ge=0, le=1_000)
    drawing_revision: str = Field(default="A", min_length=1, max_length=32)
    # Free text, as the drawing gives them. Printed on work orders.
    dimensions: str | None = Field(default=None, max_length=255)
    # Which part of the job, and which phase of it, this batch is for. Both
    # optional. With project and code they are what the item is: the same code
    # can be created again for another area or phase.
    area_id: uuid.UUID | None = None
    phase_id: uuid.UUID | None = None
    # Where the quantities already are, keyed by stage id -- for entering an
    # item that is mid-production when it reaches the system. Anything not
    # distributed starts as "not started". 200 stages bounds the request.
    initial_quantities: dict[uuid.UUID, int] = Field(default_factory=dict, max_length=200)


class ItemUpdate(BaseModel):
    """Office-side corrections. Not here: `drawing_revision` (a bump is an
    event, not an edit) and anything the ledger derives from the log."""

    code: str | None = Field(default=None, min_length=1, max_length=64)
    project_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=255)
    total_qty: int | None = Field(default=None, gt=0, le=1_000_000)
    dimensions: str | None = Field(default=None, max_length=255)
    # Sent as null to clear; left out to leave alone.
    area_id: uuid.UUID | None = None
    phase_id: uuid.UUID | None = None
    # `true` brings an archived item back. Archiving is DELETE, not this.
    is_active: bool | None = None


class WorkOrderLineIn(BaseModel):
    """One row of a work order as the office wrote it."""

    # The existing line being kept, when re-issuing. Lets a line whose item has
    # since been deleted stay on the document.
    id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    # One of that item's pictures, or null for none.
    image_id: uuid.UUID | None = None
    description: str = Field(default="", max_length=2000)
    description_ar: str = Field(default="", max_length=2000)
    dimensions: str = Field(default="", max_length=255)
    qty: int = Field(ge=1, le=1_000_000)
    # What the line prints for these. Left out (null), each is copied from the
    # item as it stands; sent, it is what this document says, whatever the item
    # says. A blank code is not a code: it falls back to the item's.
    code: str | None = Field(default=None, max_length=64)
    area: str | None = Field(default=None, max_length=120)
    phase: str | None = Field(default=None, max_length=120)
    drawing_revision: str | None = Field(default=None, max_length=32)


class WorkOrderSave(BaseModel):
    project_id: uuid.UUID
    title: str = Field(default="", max_length=160)
    notes: str = Field(default="", max_length=4000)
    # English, Arabic, or both on the one sheet.
    language: Literal["en", "ar", "both"] = "en"
    title_ar: str = Field(default="", max_length=160)
    notes_ar: str = Field(default="", max_length=4000)
    # Which optional columns the PDF prints.
    show_picture: bool = True
    show_description: bool = True
    show_dimensions: bool = True
    show_location: bool = True
    lines: list[WorkOrderLineIn] = Field(min_length=1, max_length=500)


class TranslateRequest(BaseModel):
    """Texts to put into Arabic, answered in the same order."""

    texts: list[str] = Field(min_length=1, max_length=600)

    @field_validator("texts")
    @classmethod
    def _each_within_reason(cls, texts: list[str]) -> list[str]:
        for text in texts:
            if len(text) > 4000:
                raise ValueError("one of the texts is longer than 4000 characters")
        return texts


class PartCreate(BaseModel):
    """A part an item is split into. It inherits the item's project, area,
    phase and drawing revision."""

    code: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=255)
    dimensions: str | None = Field(default=None, max_length=255)
    # How many of this part go into one piece: a chair has four legs.
    per_unit: int = Field(default=1, ge=1, le=10_000)


class PartUpdate(BaseModel):
    per_unit: int | None = Field(default=None, ge=1, le=10_000)
    description: str | None = Field(default=None, max_length=255)


class PhaseSplit(BaseModel):
    """Re-file some of a batch into another phase (null: no phase).

    `take` is how many from each place: "unstarted", or a stage id. Before the
    batch is released only "unstarted" means anything: it is the planned
    quantity.
    """

    phase_id: uuid.UUID | None = None
    take: dict[str, int] = Field(min_length=1, max_length=200)


class ItemsAssign(BaseModel):
    """File many items under an area and/or a phase at once.

    Only the fields that are sent are changed: leave `phase_id` out and every
    item keeps the phase it has; send it as null and they all lose it.
    """

    item_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)
    area_id: uuid.UUID | None = None
    phase_id: uuid.UUID | None = None


class LabelCreate(BaseModel):
    """A new area or phase: one more entry in a project's own list."""

    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    sort_order: int = 0


class LabelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    sort_order: int | None = None
    is_active: bool | None = None


class ProjectCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(default="", max_length=160)
    client: str | None = Field(default=None, max_length=160)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    client: str | None = Field(default=None, max_length=160)
    is_active: bool | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=256)
    is_admin: bool = False


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    password: str | None = Field(default=None, min_length=8, max_length=256)
    is_admin: bool | None = None
    is_active: bool | None = None


class StageCreate(BaseModel):
    """A new stage, added at runtime by an admin — no deploy, no migration.

    Behaviour is expressed ONLY through these flags; nothing in the codebase
    branches on the code string.
    """

    code: str = Field(min_length=1, max_length=48, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=120)
    sort_order: int = 0
    requires_station: bool = False
    requires_external_po: bool = False
    allows_partial_qty: bool = True
    is_terminal: bool = False
    max_days_in_state: int | None = Field(default=None, ge=1, le=365)


class StageUpdate(BaseModel):
    """Everything but the code, which is the stage's identity."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    sort_order: int | None = None
    is_active: bool | None = None
    requires_station: bool | None = None
    requires_external_po: bool | None = None
    allows_partial_qty: bool | None = None
    is_terminal: bool | None = None
    # None means "no threshold"; the field must still be distinguishable from
    # "not sent", which exclude_unset handles at the endpoint.
    max_days_in_state: int | None = Field(default=None, ge=1, le=365)


class StationCreate(BaseModel):
    stage_id: uuid.UUID
    code: str = Field(min_length=1, max_length=48, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=120)
    sort_order: int = 0


class StationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    sort_order: int | None = None
    is_active: bool | None = None


class ImageNoteUpdate(BaseModel):
    """The words that go with a snag photo. Written after the shutter, because
    on the floor the photo is taken first and described second."""

    note: str = Field(default="", max_length=255)


class RevisionBumpRequest(BaseModel):
    drawing_revision: str = Field(min_length=1, max_length=32)


class BannerUpdate(BaseModel):
    """The site-wide banner. `neutral` with no message means "show nothing".

    A Literal rather than a lookup table: these four are what the stylesheet
    paints, not vocabulary the factory owns. See app/services/status.py.
    """

    color: Literal["green", "yellow", "red", "neutral"]
    message: str = Field(default="", max_length=200)
