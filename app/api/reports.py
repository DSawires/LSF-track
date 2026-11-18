from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user
from app.models import User
from app.services import reports

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/wip")
def wip(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    project_id: uuid.UUID | None = None,
    area_id: uuid.UUID | None = None,
    phase_id: uuid.UUID | None = None,
) -> dict:
    return reports.wip_report(db, project_id=project_id, area_id=area_id, phase_id=phase_id)


@router.get("/aging")
def aging(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    limit: int | None = None,
    project_id: uuid.UUID | None = None,
    stage_id: uuid.UUID | None = None,
    min_days: float | None = None,
    area_id: uuid.UUID | None = None,
    phase_id: uuid.UUID | None = None,
) -> dict:
    # Uncapped only in the absence of a client value; a request can't demand
    # every row of an old factory's log in one response.
    capped = min(limit, 500) if limit is not None else 200
    return reports.aging_report(
        db,
        limit=capped,
        project_id=project_id,
        stage_id=stage_id,
        min_days=min_days,
        area_id=area_id,
        phase_id=phase_id,
    )


@router.get("/exceptions")
def exceptions(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return reports.exceptions_report(db)
