"""The offline cache payload.

The phone must be able to *look things up* with no signal, not just queue writes.
This endpoint is everything needed to render the logging screen from scratch, and
the client stores it in IndexedDB on every successful sync.

The status banner rides along for exactly that reason: a notice worth putting on
the floor's screens should not vanish the moment the floor loses signal.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.deps import current_user
from app.models import (
    Area,
    EventType,
    Phase,
    Project,
    ReasonCode,
    Stage,
    Station,
    User,
)
from app.services.status import current_banner
from app.services.sync import items_token

router = APIRouter(prefix="/api", tags=["reference"])


@router.get("/reference")
def reference(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    stages = list(db.scalars(sa.select(Stage).order_by(Stage.sort_order, Stage.code)))
    stations = list(db.scalars(sa.select(Station).order_by(Station.sort_order, Station.code)))
    event_types = list(db.scalars(sa.select(EventType).order_by(EventType.sort_order)))
    reason_codes = list(db.scalars(sa.select(ReasonCode).order_by(ReasonCode.sort_order)))
    projects = list(db.scalars(sa.select(Project).order_by(Project.code)))

    return {
        "server_time": utcnow().isoformat(),
        # Changes whenever anything the item list is drawn from does. A phone
        # holding the same token skips the /api/items replay on this sync.
        "items_token": items_token(db),
        "banner": current_banner(db),
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
                "max_days_in_state": s.max_days_in_state,
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
                "is_finish": t.is_finish,
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
        # The lists an item is filed under, each row belonging to one project.
        # Inactive rows travel too: an item already in a retired area still
        # has to be labelled.
        "areas": [
            {"id": str(a.id), "project_id": str(a.project_id), "name": a.name,
             "sort_order": a.sort_order, "is_active": a.is_active}
            for a in db.scalars(sa.select(Area).order_by(Area.sort_order, Area.name))
        ],
        "phases": [
            {"id": str(p.id), "project_id": str(p.project_id), "name": p.name,
             "sort_order": p.sort_order, "is_active": p.is_active}
            for p in db.scalars(sa.select(Phase).order_by(Phase.sort_order, Phase.name))
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
    }
