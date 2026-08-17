"""Account status: who is using the system, and the banner they see.

Two things an admin cannot get from the production reports. The reports answer
"where is the work"; this answers "is everyone's phone actually reaching the
server", which on a factory floor with patchy coverage is a different question.

Everything here except `last_login_at` is derived from the event log, so it
carries no state of its own to fall out of date.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Event, StatusBanner, User

# The four the stylesheet can paint. Fixed by the UI, unlike stages and reason
# codes -- a fifth colour is a frontend change, so there is nothing to gain
# from making this a table the factory owns.
BANNER_COLORS = ("green", "yellow", "red", "neutral")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def user_activity(db: Session) -> dict:
    """Per-account activity, newest first.

    "Actions" counts events *credited* to the user (`user_id`) rather than
    events posted from their session (`submitted_by_user_id`): on a shared
    floor phone the credited user is the engineer who saw the work happen,
    which is the person an admin is asking about. Server-written events
    (release, revision bump, archive) are their author's actions too and are
    counted the same way.

    Activity time is `occurred_at`, like every other report -- device time,
    what happened on the floor. A clock skewed far enough to distort this row
    already shows up in the exceptions view.
    """
    counts = {
        user_id: (count, last)
        for user_id, count, last in db.execute(
            sa.select(
                Event.user_id,
                sa.func.count(Event.id),
                sa.func.max(Event.occurred_at),
            ).group_by(Event.user_id)
        )
    }

    rows = []
    for user in db.scalars(sa.select(User).order_by(User.username)):
        actions, last_event_at = counts.get(user.id, (0, None))
        rows.append(
            {
                "id": str(user.id),
                "username": user.username,
                "display_name": user.display_name,
                "is_admin": user.is_admin,
                "is_active": user.is_active,
                "last_login_at": _iso(user.last_login_at),
                "last_event_at": _iso(last_event_at),
                "actions": actions,
            }
        )

    # Most recently active first: the point of the page is spotting the
    # account that has gone quiet, and that is the row at the bottom.
    rows.sort(key=lambda r: (r["last_event_at"] or "", r["last_login_at"] or ""), reverse=True)
    return {"users": rows}


def current_banner(db: Session) -> dict:
    """The live banner. Always a dict -- an empty log means neutral and silent."""
    row = db.scalars(
        sa.select(StatusBanner)
        .order_by(StatusBanner.set_at.desc(), StatusBanner.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return {"color": "neutral", "message": "", "set_at": None, "set_by": None}
    setter = db.get(User, row.set_by_user_id)
    return {
        "color": row.color,
        "message": row.message,
        "set_at": _iso(row.set_at),
        "set_by": setter.display_name if setter else None,
    }


def set_banner(db: Session, color: str, message: str, user: User) -> dict:
    """Raise (or clear) the banner. A new row every time; nothing is edited."""
    db.add(
        StatusBanner(
            color=color,
            message=message.strip(),
            set_by_user_id=user.id,
        )
    )
    db.flush()
    return current_banner(db)
