"""The offline cache payload.

The phone must be able to *look things up* with no signal, not just queue writes.
This endpoint is everything needed to render the logging screen from scratch, and
the client stores it in IndexedDB on every successful sync.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.deps import current_user
from app.models import (
    EventState,
    EventType,
    Project,
    ReasonCode,
    RouteTemplate,
    RouteTemplateStep,
    Stage,
    Station,
    User,
)

router = APIRouter(prefix="/api", tags=["reference"])


@router.get("/reference")
def reference(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    stages = list(db.scalars(sa.select(Stage).order_by(Stage.sort_order, Stage.code)))
    stations = list(db.scalars(sa.select(Station).order_by(Station.sort_order, Station.code)))
    states = list(db.scalars(sa.select(EventState).order_by(EventState.sort_order)))
    event_types = list(db.scalars(sa.select(EventType).order_by(EventType.sort_order)))
    reason_codes = list(db.scalars(sa.select(ReasonCode).order_by(ReasonCode.sort_order)))
    projects = list(db.scalars(sa.select(Project).order_by(Project.code)))
    templates = list(
        db.scalars(sa.select(RouteTemplate).order_by(RouteTemplate.code, RouteTemplate.version))
    )
    template_steps = list(
        db.scalars(sa.select(RouteTemplateStep).order_by(RouteTemplateStep.seq))
    )

    return {
        "server_time": utcnow().isoformat(),
        "stages": [
            {
                "id": str(s.id),
                "code": s.code,
                "name": s.name,
                "sort_order": s.sort_order,
                "is_active": s.is_active,
                "requires_station": s.requires_station,
                "requires_external_po": s.requires_external_po,
                "allows_partial_qty": s.allows_partial_qty,
                "is_terminal": s.is_terminal,
            }
            for s in stages
        ],
        "stations": [
            {
                "id": str(s.id),
                "stage_id": str(s.stage_id),
                "code": s.code,
                "name": s.name,
                "sort_order": s.sort_order,
                "is_active": s.is_active,
            }
            for s in stations
        ],
        "states": [
            {
                "id": str(s.id),
                "code": s.code,
                "name": s.name,
                "sort_order": s.sort_order,
                "is_complete": s.is_complete,
            }
            for s in states
        ],
        "event_types": [
            {
                "id": str(t.id),
                "code": t.code,
                "name": t.name,
                "sort_order": t.sort_order,
                "is_active": t.is_active,
                "is_correction": t.is_correction,
                "is_rework": t.is_rework,
                "is_release": t.is_release,
                "is_revision_bump": t.is_revision_bump,
                "moves_quantity": t.moves_quantity,
                "requires_item_step": t.requires_item_step,
                "requires_reason_code": t.requires_reason_code,
            }
            for t in event_types
        ],
        "reason_codes": [
            {"id": str(r.id), "code": r.code, "name": r.name, "is_active": r.is_active}
            for r in reason_codes
        ],
        "projects": [
            {
                "id": str(p.id),
                "code": p.code,
                "name": p.name,
                "client": p.client,
                "is_active": p.is_active,
            }
            for p in projects
        ],
        "route_templates": [
            {
                "id": str(t.id),
                "code": t.code,
                "version": t.version,
                "name": t.name,
                "is_published": t.is_published,
                "steps": [
                    {"seq": s.seq, "stage_id": str(s.stage_id)}
                    for s in template_steps
                    if s.route_template_id == t.id
                ],
            }
            for t in templates
        ],
    }
