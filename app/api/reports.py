from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user
from app.models import User
from app.services import reports

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/wip")
def wip(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return reports.wip_report(db)


@router.get("/aging")
def aging(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    limit: int | None = None,
) -> dict:
    return reports.aging_report(db, limit=limit)


@router.get("/exceptions")
def exceptions(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return reports.exceptions_report(db)
