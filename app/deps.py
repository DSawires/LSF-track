"""Session handling.

Sessions are deliberately long-lived and sliding. An engineer who starts a shift in
a dead spot at the back of the paint shop must still be able to log events and drain
their queue hours later; being bounced to a login screen with no network is a total
outage for that person.
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.config import get_settings, require_session_key
from app.db import get_db
from app.models import User

COOKIE_NAME = "lsf_session"


def _serializer() -> URLSafeTimedSerializer:
    # Validated here, not at settings load: an unusable key must never reach
    # the signer, but a backup job that imports settings has no business
    # caring about it.
    return URLSafeTimedSerializer(require_session_key(), salt="lsf-session")


def _password_fingerprint(user: User) -> str:
    # Binding the token to the password hash makes sessions revocable: change
    # the password (or reset it after a lost phone) and every token issued
    # before the change dies on its next request. A digest, not the hash
    # itself -- the cookie must not leak offline-crackable material.
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def issue_session(response: Response, user: User) -> None:
    settings = get_settings()
    token = _serializer().dumps({"uid": str(user.id), "pwf": _password_fingerprint(user)})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def current_user(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> User:
    settings = get_settings()
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    try:
        data = _serializer().loads(token, max_age=settings.session_max_age_seconds)
    except SignatureExpired:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
    except BadSignature:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad session")

    user = db.get(User, uuid.UUID(data["uid"]))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown user")
    if data.get("pwf") != _password_fingerprint(user):
        # Password changed since this token was issued; the old session is dead.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session revoked")

    # Slide the expiry on every authenticated request, so an app in daily use never
    # expires out from under someone.
    issue_session(response, user)
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return user
