"""Technical-office setup: projects, stages and stations.

Items have no stage sequence and no route: units move freely between stages,
so there is nothing here to set up per item -- see app/ledger.py.

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
from app.models import Area, Item, Phase, Project, Stage, Station, User
from app.services.items import archive_item
from app.schemas import (
    LabelCreate,
    LabelUpdate,
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


# -- areas and phases ---------------------------------------------------------
#
# Two lists of the same shape: a name, an order, and whether it is still on
# offer. An item points at one of each (or neither). They are edited here and
# can also be created by an import file that names a new one.


def label_payload(row) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "sort_order": row.sort_order,
        "is_active": row.is_active,
    }


def _name_taken(db: Session, model, name: str, exclude_id: uuid.UUID | None = None) -> bool:
    # Case-insensitive: "Spa" and "spa" in one filter are the same place twice.
    query = sa.select(model.id).where(sa.func.lower(model.name) == name.lower())
    if exclude_id is not None:
        query = query.where(model.id != exclude_id)
    return db.scalars(query.limit(1)).first() is not None


def _create_label(db: Session, model, payload: LabelCreate, noun: str) -> dict:
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, f"the {noun} needs a name")
    if _name_taken(db, model, name):
        raise HTTPException(status.HTTP_409_CONFLICT, f"{noun} '{name}' already exists")
    row = model(name=name, sort_order=payload.sort_order)
    db.add(row)
    db.flush()
    return label_payload(row)


def _update_label(db: Session, model, row_id: uuid.UUID, payload: LabelUpdate, noun: str) -> dict:
    """Rename, reorder or retire. A rename re-labels every item that points at
    the row, which is the point: items reference it by id. Retiring one takes
    it off the pickers; items already in it keep it."""
    row = db.get(model, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown {noun}")
    fields = payload.model_dump(exclude_unset=True)
    if "name" in fields:
        name = (fields["name"] or "").strip()
        if not name:
            raise HTTPException(422, f"the {noun} needs a name")
        if _name_taken(db, model, name, exclude_id=row.id):
            raise HTTPException(status.HTTP_409_CONFLICT, f"{noun} '{name}' already exists")
        row.name = name
    if fields.get("sort_order") is not None:
        row.sort_order = fields["sort_order"]
    if fields.get("is_active") is not None:
        row.is_active = fields["is_active"]
    db.flush()
    return label_payload(row)


@router.post("/areas", status_code=status.HTTP_201_CREATED)
def create_area(
    payload: LabelCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)
) -> dict:
    return _create_label(db, Area, payload, "area")


@router.patch("/areas/{area_id}")
def update_area(
    area_id: uuid.UUID,
    payload: LabelUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    return _update_label(db, Area, area_id, payload, "area")


@router.post("/phases", status_code=status.HTTP_201_CREATED)
def create_phase(
    payload: LabelCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)
) -> dict:
    return _create_label(db, Phase, payload, "phase")


@router.patch("/phases/{phase_id}")
def update_phase(
    phase_id: uuid.UUID,
    payload: LabelUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    return _update_label(db, Phase, phase_id, payload, "phase")


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
    with_items: bool = False,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    """Archive a project.

    `with_items=true` archives its active items along with it, each with its
    own archive event, exactly as archiving them one at a time would. That is
    how a finished job leaves the floor: a delivered hotel is a couple of
    hundred items, and every one left active is replayed on every report and
    every phone's refresh for as long as it stays.

    Without the flag, active items block the archive and nothing is touched.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown project")
    # Only THIS project's items are in play. A refusal names them, because
    # "2 active items" against a flat item list is how an admin ends up blaming
    # another job's work for a project that will not archive.
    active = list(
        db.scalars(
            sa.select(Item)
            .where(Item.project_id == project_id, Item.is_active.is_(True))
            .order_by(Item.code)
        )
    )
    if active and not with_items:
        codes = [item.code for item in active]
        named = ", ".join(codes[:5]) + (" …" if len(codes) > 5 else "")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"project has {len(codes)} active item(s) in it ({named}); "
            "remove or archive those first",
        )
    for item in active:
        archive_item(db, item, admin)
    project.is_active = False
    db.flush()
    return {"archived": True, "archived_items": len(active)}
