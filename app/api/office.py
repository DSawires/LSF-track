"""Technical-office setup: projects, route templates, stages and stations.

Route templates are append-only in spirit: posting steps under an existing code
creates the *next version* rather than editing the old one, because items already
released hold a snapshot of whatever version they left against, and published
history should stay explainable.

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
from app.models import Item, Project, RouteTemplate, RouteTemplateStep, Stage, Station, User
from app.schemas import (
    ProjectCreate,
    RouteTemplateCreate,
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


@router.delete("/projects/{project_id}")
def archive_project(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown project")
    active_items = db.scalar(
        sa.select(sa.func.count())
        .select_from(Item)
        .where(Item.project_id == project_id, Item.is_active.is_(True))
    )
    if active_items:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"project has {active_items} active item(s); remove or archive them first",
        )
    project.is_active = False
    db.flush()
    return {"archived": True}


@router.delete("/routes/{route_template_id}")
def unpublish_route(
    route_template_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    """Unpublishing hides a route version from the release picker. Items already
    released against it are untouched -- their steps are a snapshot."""
    template = db.get(RouteTemplate, route_template_id)
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown route template")
    template.is_published = False
    db.flush()
    return {"unpublished": True}


@router.post("/routes", status_code=status.HTTP_201_CREATED)
def create_route_template(
    payload: RouteTemplateCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    code = payload.code.strip().lower().replace(" ", "_")
    if not code:
        raise HTTPException(422, "route code is required")
    if not payload.stage_ids:
        raise HTTPException(422, "a route needs at least one stage")

    stages = {s.id: s for s in db.scalars(sa.select(Stage))}
    for stage_id in payload.stage_ids:
        if stage_id not in stages:
            raise HTTPException(422, f"unknown stage {stage_id}")
    # A terminal stage completes units for good (the ledger counts them
    # finished there); anywhere but last, the steps after it would silently
    # never see the work.
    for stage_id in payload.stage_ids[:-1]:
        if stages[stage_id].is_terminal:
            raise HTTPException(
                422,
                f"stage '{stages[stage_id].name}' is terminal and must be the last step",
            )

    latest = db.scalars(
        sa.select(RouteTemplate)
        .where(RouteTemplate.code == code)
        .order_by(RouteTemplate.version.desc())
        .limit(1)
    ).first()
    version = (latest.version + 1) if latest else 1

    template = RouteTemplate(
        code=code,
        version=version,
        name=payload.name.strip() or (latest.name if latest else code),
    )
    db.add(template)
    db.flush()

    # Gaps of 10 so a stage can later be slotted between two steps by a new
    # version without renumbering everything.
    for position, stage_id in enumerate(payload.stage_ids, start=1):
        db.add(
            RouteTemplateStep(
                route_template_id=template.id, seq=position * 10, stage_id=stage_id
            )
        )
    db.flush()

    return {
        "id": str(template.id),
        "code": template.code,
        "version": template.version,
        "name": template.name,
        "is_published": template.is_published,
        "steps": [
            {"seq": (i + 1) * 10, "stage_id": str(stage_id)}
            for i, stage_id in enumerate(payload.stage_ids)
        ],
    }
