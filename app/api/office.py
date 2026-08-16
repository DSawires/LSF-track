"""Technical-office setup: projects and route templates.

Route templates are append-only in spirit: posting steps under an existing code
creates the *next version* rather than editing the old one, because items already
released hold a snapshot of whatever version they left against, and published
history should stay explainable.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import uuid

from app.db import get_db
from app.deps import current_user, require_admin
from app.models import Item, Project, RouteTemplate, RouteTemplateStep, Stage, User
from app.schemas import ProjectCreate, RouteTemplateCreate

router = APIRouter(prefix="/api", tags=["office"])


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
