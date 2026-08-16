"""Admin user management.

Accounts were previously CLI-only (`manage.py create-user`), which meant a
forgotten password needed someone with shell access to the server. Admin-only
throughout; the one rule beyond CRUD is that admins cannot lock themselves
out -- no self-demotion, no self-deactivation. Password changes revoke the
target's existing sessions as a side effect of the fingerprint in the session
token (see app.deps).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_admin
from app.models import User
from app.schemas import UserCreate, UserUpdate
from app.security import hash_password

router = APIRouter(prefix="/api/users", tags=["users"])


def _payload(user: User) -> dict:
    return {
        "id": str(user.id),
        "username": user.username,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
    }


@router.get("")
def list_users(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    rows = db.scalars(sa.select(User).order_by(User.username))
    return {"users": [_payload(u) for u in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    username = payload.username.strip().lower()
    if db.scalars(sa.select(User).where(User.username == username)).first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"user {username} already exists")
    user = User(
        username=username,
        display_name=payload.display_name.strip(),
        password_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
    )
    db.add(user)
    db.flush()
    return _payload(user)


@router.patch("/{user_id}")
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    changes = payload.model_dump(exclude_unset=True)
    if user.id == admin.id and (
        changes.get("is_admin") is False or changes.get("is_active") is False
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "you cannot demote or deactivate your own account",
        )

    if "display_name" in changes and changes["display_name"] is not None:
        user.display_name = changes["display_name"].strip()
    if "is_admin" in changes and changes["is_admin"] is not None:
        user.is_admin = changes["is_admin"]
    if "is_active" in changes and changes["is_active"] is not None:
        user.is_active = changes["is_active"]
    if "password" in changes and changes["password"]:
        # Also revokes the user's existing sessions: the token fingerprint
        # no longer matches the new hash.
        user.password_hash = hash_password(changes["password"])
    db.flush()
    return _payload(user)
