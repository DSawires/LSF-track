"""Technical-office setup: projects, stages and stations.

There are no shared route templates. An item's sequence of stages is its own,
picked (or copied off a sibling item) when the item is created -- see
app/services/items.py.

Stages and stations are the runtime vocabulary the whole design revolves
around: adding one here is the "no deploy, no migration, no code change"
procedure from CLAUDE.md, so these endpoints exist precisely so that a
non-developer can perform it. Admin-only, because a typo'd behaviour flag
changes how the ledger treats every future event at that stage.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import uuid

from app.db import get_db
from app.deps import current_user, require_admin
from app.models import Item, Project, Stage, Station, User
from app.schemas import (
    ProjectCreate,
    ProjectUpdate,
    StageCreate,
    StageUpdate,
    StationCreate,
    StationUpdate,
)

router = APIRouter(prefix="/api", tags=["office"])


def _stage_payload(stage: Stage) -> dict:
    return {
        "id": str(stage.id),
        "code": stage.code,
        "name": stage.name,
        "sort_order": stage.sort_order,
        "is_active": stage.is_active,
        "requires_station": stage.requires_station,
        "requires_external_po": stage.requires_external_po,
        "allows_partial_qty": stage.allows_partial_qty,
        "is_terminal": stage.is_terminal,
        "max_days_in_state": stage.max_days_in_state,
    }


@router.post("/stages", status_code=status.HTTP_201_CREATED)
def create_stage(
    payload: StageCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    if db.scalars(sa.select(Stage).where(Stage.code == payload.code)).first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"stage {payload.code} already exists")
    stage = Stage(
        code=payload.code,
        name=payload.name.strip(),
        sort_order=payload.sort_order,
        requires_station=payload.requires_station,
        requires_external_po=payload.requires_external_po,
        allows_partial_qty=payload.allows_partial_qty,
        is_terminal=payload.is_terminal,
        max_days_in_state=payload.max_days_in_state,
    )
    db.add(stage)
    db.flush()
    return _stage_payload(stage)


@router.patch("/stages/{stage_id}")
def update_stage(
    stage_id: uuid.UUID,
    payload: StageUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    stage = db.get(Stage, stage_id)
    if stage is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown stage")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(stage, field, value)
    db.flush()
    return _stage_payload(stage)


def _station_payload(station: Station) -> dict:
    return {
        "id": str(station.id),
        "stage_id": str(station.stage_id),
        "code": station.code,
        "name": station.name,
        "sort_order": station.sort_order,
        "is_active": station.is_active,
    }


@router.post("/stations", status_code=status.HTTP_201_CREATED)
def create_station(
    payload: StationCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    if db.get(Stage, payload.stage_id) is None:
        raise HTTPException(422, "unknown stage")
    if db.scalars(sa.select(Station).where(Station.code == payload.code)).first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"station {payload.code} already exists")
    station = Station(
        stage_id=payload.stage_id,
        code=payload.code,
        name=payload.name.strip(),
        sort_order=payload.sort_order,
    )
    db.add(station)
    db.flush()
    return _station_payload(station)


@router.patch("/stations/{station_id}")
def update_station(
    station_id: uuid.UUID,
    payload: StationUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    """Rename, reorder or retire a physical station.

    Neither `code` nor `stage_id` is patchable: events reference the station by
    id and reports read the name off the row, so a rename is safe and needs no
    history rewrite -- but a station that changed stage or code would silently
    re-label work that happened somewhere else. Retire it and add the new one.
    """
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown station")
    fields = payload.model_dump(exclude_unset=True)
    if "name" in fields:
        fields["name"] = fields["name"].strip()
        if not fields["name"]:
            raise HTTPException(422, "station name is required")
    for field, value in fields.items():
        setattr(station, field, value)
    db.flush()
    return _station_payload(station)


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    code = payload.code.strip().upper()
    if not code:
        raise HTTPException(422, "project code is required")
    if db.scalars(sa.select(Project).where(Project.code == code)).first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"project {code} already exists")
    project = Project(code=code, name=payload.name.strip() or code, client=payload.client)
    db.add(project)
    db.flush()
    return {
        "id": str(project.id),
        "code": project.code,
        "name": project.name,
        "client": project.client,
        "is_active": project.is_active,
    }


@router.patch("/projects/{project_id}")
def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    """Rename a project, correct its client, or bring an archived one back.

    The code is not editable: items reference the project by id but people
    reference it by code, in drawings and emails that outlive the app.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown project")
    fields = payload.model_dump(exclude_unset=True)
    if "name" in fields:
        fields["name"] = (fields["name"] or "").strip()
        if not fields["name"]:
            raise HTTPException(422, "project name is required")
    for field, value in fields.items():
        setattr(project, field, value)
    db.flush()
    return {
        "id": str(project.id),
        "code": project.code,
        "name": project.name,
        "client": project.client,
        "is_active": project.is_active,
    }


@router.delete("/projects/{project_id}")
def archive_project(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown project")
    # Only THIS project's items can block it. The message names them, because
    # "2 active items" against a flat item list is how an admin ends up blaming
    # another job's work for a project that will not archive.
    blocking = list(
        db.scalars(
            sa.select(Item.code)
            .where(Item.project_id == project_id, Item.is_active.is_(True))
            .order_by(Item.code)
        )
    )
    if blocking:
        named = ", ".join(blocking[:5]) + (" …" if len(blocking) > 5 else "")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"project has {len(blocking)} active item(s) in it ({named}); "
            "remove or archive those first",
        )
    project.is_active = False
    db.flush()
    return {"archived": True}
