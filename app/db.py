from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class UTCDateTime(sa.types.TypeDecorator):
    """Timezone-aware timestamps that survive a round trip on every dialect.

    PostgreSQL keeps the offset; SQLite does not, and hands back naive values.
    Normalising in both directions here means the rest of the codebase can assume
    every datetime it touches is aware and in UTC.
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime reached the database layer")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime}


_engine = None
_SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = get_settings().database_url
        kwargs: dict = {"pool_pre_ping": True, "future": True}
        if url.startswith("sqlite"):
            kwargs.pop("pool_pre_ping")
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = sa.create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            # Foreign keys are off by default on SQLite; the tests rely on them.
            @sa.event.listens_for(_engine, "connect")
            def _fk_on(dbapi_connection, _record):  # pragma: no cover - trivial
                cur = dbapi_connection.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, future=True)
    return _engine


def get_sessionmaker():
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def reset_engine() -> None:
    """Drop the cached engine. Used by the test fixtures."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_db() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
