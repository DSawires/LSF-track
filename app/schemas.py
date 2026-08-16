from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


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
    qty: int = 0
    reason_code_id: uuid.UUID | None = None
    note: str | None = None
    supersedes_event_id: uuid.UUID | None = None
    # Who logged it on the floor, which is not always who is syncing it now.
    user_id: uuid.UUID | None = None


class EventBatch(BaseModel):
    events: list[EventCreate] = Field(default_factory=list)


class ItemCreate(BaseModel):
    code: str
    project_id: uuid.UUID
    description: str
    total_qty: int = Field(gt=0)
    drawing_revision: str
    target_release_date: date | None = None


class ProjectCreate(BaseModel):
    code: str
    name: str = ""
    client: str | None = None


class RouteTemplateCreate(BaseModel):
    """Steps in order. Posting an existing code creates the next version."""

    code: str
    name: str = ""
    stage_ids: list[uuid.UUID] = Field(default_factory=list)


class ReleaseRequest(BaseModel):
    route_template_id: uuid.UUID
    drawing_revision: str | None = None


class RevisionBumpRequest(BaseModel):
    drawing_revision: str
