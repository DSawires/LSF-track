"""The admin status page, and the banner it publishes to everyone else.

Admin-only, both halves. The user list is account activity -- last sign-in,
last logged event, how many events an account has ever carried -- which is how
an admin notices that someone's phone has not reached the server since Tuesday.

The banner goes the other way: an admin raises it here and the floor sees it at
the top of the app. Reading it is not admin-only, and it rides along in
/api/reference so a phone that is offline still shows the notice it last
received rather than silently dropping it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user, require_admin
from app.models import User
from app.schemas import BannerUpdate
from app.services import status as status_service

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("/users")
def user_status(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    return status_service.user_activity(db)


@router.get("/banner")
def read_banner(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    return status_service.current_banner(db)


@router.post("/banner")
def write_banner(
    payload: BannerUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    return status_service.set_banner(db, payload.color, payload.message, admin)
