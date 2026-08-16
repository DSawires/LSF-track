from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


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
    item_step_id: uuid.UUID | None = None
    station_id: uuid.UUID | None = None
    state_id: uuid.UUID | None = None
    # ge=0: negative movement is a rework/correction event, never a negative
    # qty. The ceiling is far beyond any real batch; it exists so a client bug
    # cannot store a number that breaks every report aggregate.
    qty: int = Field(default=0, ge=0, le=1_000_000)
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
    code: str = Field(min_length=1, max_length=64)
    project_id: uuid.UUID
    description: str = Field(max_length=255)
    total_qty: int = Field(gt=0, le=1_000_000)
    drawing_revision: str = Field(min_length=1, max_length=32)
    target_release_date: date | None = None


class ProjectCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(default="", max_length=160)
    client: str | None = Field(default=None, max_length=160)


class RouteTemplateCreate(BaseModel):
    """Steps in order. Posting an existing code creates the next version."""

    code: str = Field(min_length=1, max_length=48)
    name: str = Field(default="", max_length=160)
    stage_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class ReleaseRequest(BaseModel):
    route_template_id: uuid.UUID
    drawing_revision: str | None = None
    # Where the quantities already are, keyed by step seq — for onboarding an
    # item that is mid-production when it enters the system. Anything not
    # distributed starts as unstarted.
    initial_quantities: dict[int, int] = Field(default_factory=dict)


class RevisionBumpRequest(BaseModel):
    drawing_revision: str = Field(min_length=1, max_length=32)
