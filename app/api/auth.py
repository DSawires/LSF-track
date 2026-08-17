from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.deps import clear_session, current_user, issue_session
from app.models import User
from app.schemas import LoginRequest
from app.security import hash_password, verify_password
from app.throttle import login_throttle

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Burned on every login attempt for a nonexistent or inactive user, so an
# unknown username costs the same ~50ms of scrypt as a wrong password.
# Otherwise response timing enumerates valid usernames.
_DUMMY_HASH = hash_password("not-a-real-password")


def _user_payload(user: User) -> dict:
    return {
        "id": str(user.id),
        "username": user.username,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
    }


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    username = payload.username.strip().lower()
    # Real client IP relies on uvicorn's --proxy-headers when behind the compose
    # proxy; the app port is not published, so the header can't be spoofed from
    # outside.
    ip = request.client.host if request.client else "unknown"

    wait = login_throttle.retry_after(username, ip)
    if wait:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"too many attempts; retry in {wait}s",
            headers={"Retry-After": str(wait)},
        )

    user = db.scalars(sa.select(User).where(User.username == username)).first()
    if user is None or not user.is_active:
        verify_password(payload.password, _DUMMY_HASH)
        login_throttle.record_failure(username, ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")
    if not verify_password(payload.password, user.password_hash):
        login_throttle.record_failure(username, ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")
    login_throttle.record_success(username)
    # The status page's one non-derived figure. Written on the way in rather
    # than on every authenticated request: sessions slide for weeks, so
    # "last seen" and "last signed in" are genuinely different facts and this
    # is the one an admin asked for.
    user.last_login_at = utcnow()
    issue_session(response, user)
    return {"user": _user_payload(user)}


@router.post("/logout")
def logout(response: Response) -> dict:
    clear_session(response)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"user": _user_payload(user)}
