from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

os.environ["LSF_DATABASE_URL"] = "sqlite://"
os.environ["LSF_SECURE_COOKIES"] = "false"
os.environ["LSF_SECRET_KEY"] = "test-secret-key-long-enough-to-pass-the-length-check"

from app import db as app_db  # noqa: E402
from app.db import Base, utcnow  # noqa: E402
from app.models import (  # noqa: E402
    Area,
    Event,
    EventState,
    EventType,
    Item,
    ItemStep,
    Phase,
    Project,
    Stage,
    Station,
    User,
)
from app.schemas import EventCreate  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services.events import record_event  # noqa: E402
from seeds.seed import run as run_seed  # noqa: E402


@pytest.fixture()
def client(factory):
    from fastapi.testclient import TestClient

    from app.main import app

    factory.db.commit()
    with TestClient(app) as test_client:
        test_client.post("/api/auth/login", json={"username": "test", "password": "pw"})
        yield test_client


@pytest.fixture()
def world(factory):
    stages = ["carpentry", "paint", "packing"]
    item = factory.item("API-1", 50, stages)
    factory.db.commit()
    return factory, stages, item


@pytest.fixture(autouse=True)
def _clean_throttle():
    from app.storage import get_storage
    from app.throttle import login_throttle

    login_throttle.reset()
    get_storage.cache_clear()
    yield
    login_throttle.reset()
    get_storage.cache_clear()


# Set LSF_TEST_DATABASE_URL (e.g. postgresql+psycopg://...) to run the suite
# against a real server instead of per-test SQLite files. CI does this: the
# SAVEPOINT race handling, varchar length limits, and UUID behaviour genuinely
# differ between dialects, and production runs on PostgreSQL.
_EXTERNAL_DB = os.environ.get("LSF_TEST_DATABASE_URL", "").strip()


@pytest.fixture()
def db(tmp_path):
    # File-backed rather than :memory:, because the API tests hit the app through
    # TestClient and FastAPI runs sync endpoints in worker threads -- an in-memory
    # SQLite would give each thread its own empty database.
    from app.config import get_settings

    os.environ["LSF_DATABASE_URL"] = _EXTERNAL_DB or f"sqlite:///{tmp_path}/test.sqlite3"
    get_settings.cache_clear()
    app_db.reset_engine()
    engine = app_db.get_engine()
    if _EXTERNAL_DB:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = app_db.get_sessionmaker()()
    try:
        yield session
        session.rollback()
    finally:
        session.close()
        if _EXTERNAL_DB:
            Base.metadata.drop_all(app_db.get_engine())
        app_db.reset_engine()
        get_settings.cache_clear()


@pytest.fixture()
def seeded(db):
    run_seed(db)
    db.commit()
    return db


class Factory:
    """Builds a small world for a test, in memory, through the real services."""

    def __init__(self, db):
        self.db = db
        self.user = User(
            username="test",
            display_name="Test Engineer",
            password_hash=hash_password("pw"),
            is_admin=True,
        )
        db.add(self.user)
        self.project = Project(code="PRJ", name="Test project")
        db.add(self.project)
        db.flush()

    # -- lookups --------------------------------------------------------------

    def stage(self, code: str) -> Stage:
        return self.db.scalars(sa.select(Stage).where(Stage.code == code)).one()

    def legacy_states(self) -> dict[str, EventState]:
        """The queued / in_progress / completed rows a pre-change database
        holds. Not seeded any more; tests of legacy replay create them."""
        rows = {}
        for code, sort, is_complete, is_initial in [
            ("queued", 10, False, True),
            ("in_progress", 20, False, False),
            ("completed", 30, True, False),
        ]:
            row = self.db.scalars(sa.select(EventState).where(EventState.code == code)).first()
            if row is None:
                row = EventState(
                    code=code, name=code.replace("_", " ").title(), sort_order=sort,
                    is_complete=is_complete, is_initial=is_initial,
                )
                self.db.add(row)
                self.db.flush()
            rows[code] = row
        return rows

    def event_type(self, code: str) -> EventType:
        return self.db.scalars(sa.select(EventType).where(EventType.code == code)).one()

    def station(self, code: str) -> Station:
        return self.db.scalars(sa.select(Station).where(Station.code == code)).one()

    # -- builders -------------------------------------------------------------

    def add_stage(self, code: str, sort: int = 999, **flags) -> Stage:
        """A stage added at runtime, exactly as a factory admin would."""
        stage = Stage(code=code, name=code.title(), sort_order=sort, **flags)
        self.db.add(stage)
        self.db.flush()
        return stage

    def label(self, model, name: str):
        """An area or a phase by name, added the way the office would."""
        row = self.db.scalars(sa.select(model).where(model.name == name)).first()
        if row is None:
            row = model(name=name)
            self.db.add(row)
            self.db.flush()
        return row

    def item(
        self,
        code: str,
        qty: int,
        stage_codes: list[str] | None = None,
        sample_qty: int = 0,
        area: str | None = None,
        phase: str | None = None,
    ) -> Item:
        """An item, live on the floor -- creation is the handoff.

        `area` and `phase` are names; the rows are made on first use.

        Items have no stages of their own; log against one with `move()`.
        `stage_codes` builds a *sequence-era* item instead: the ordered steps an
        item carried before sequences were removed, for the tests that prove
        those rows still replay (and that old queued entries still drain).
        `sample_qty` defaults to 0 here so tests about batch movement start with
        the batch on the floor; the sample flow has tests of its own.
        """
        item = Item(
            code=code,
            project_id=self.project.id,
            area_id=self.label(Area, area).id if area else None,
            phase_id=self.label(Phase, phase).id if phase else None,
            description=f"{code} test batch",
            total_qty=qty,
            sample_qty=sample_qty,
            drawing_revision="A",
        )
        self.db.add(item)
        self.db.flush()
        for position, stage_code in enumerate(stage_codes or [], start=1):
            self.db.add(
                ItemStep(item_id=item.id, seq=position * 10, stage_id=self.stage(stage_code).id)
            )
        self.db.flush()
        self.db.refresh(item)
        return item

    def move(
        self,
        item: Item,
        to: str | None,
        qty: int,
        frm: str | None = None,
        at: datetime | None = None,
        event_type: str | None = None,
        station_code: str | None = None,
        reason_code_id=None,
        event_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> Event:
        """`qty` units go from `frm` to `to`, as the app logs it now.

        Both are stage codes. `frm=None` is "not started", `frm="done"` is
        Done; `to=None` is Done (a finish entry).
        """
        if event_type is None:
            event_type = "move" if to is not None else "finish"
        payload = EventCreate(
            id=event_id or uuid.uuid4(),
            item_id=item.id,
            stage_id=self.stage(to).id if to is not None else None,
            from_stage_id=self.stage(frm).id if frm not in (None, "done") else None,
            from_unstarted=frm is None,
            from_done=frm == "done",
            event_type_id=self.event_type(event_type).id,
            qty=qty,
            station_id=self.station(station_code).id if station_code else None,
            reason_code_id=reason_code_id,
            occurred_at=at or utcnow(),
            user_id=user_id,
        )
        return record_event(self.db, payload, self.user).event

    def log(
        self,
        item: Item,
        step_seq: int | None,
        qty: int,
        at: datetime | None = None,
        event_type: str | None = None,
        station_code: str | None = None,
        reason_code_id=None,
        event_id: uuid.UUID | None = None,
        supersedes: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        legacy_state: str | None = None,
        from_seq: int | None = None,
        from_done: bool = False,
    ) -> Event:
        """A sequence-era entry: `qty` units arrived at step `step_seq` of the
        item's old sequence (`step_seq=None` is Done), drawn from the nearest
        step before it. `legacy_state` writes a row the way the app did before
        states went; `from_seq` / `from_done` name a source step."""
        step = next(s for s in item.steps if s.seq == step_seq) if step_seq is not None else None
        if event_type is None:
            event_type = "move" if step is not None else "finish"
        payload = EventCreate(
            id=event_id or uuid.uuid4(),
            item_id=item.id,
            item_step_id=step.id if step else None,
            event_type_id=self.event_type(event_type).id,
            state_id=self.legacy_states()[legacy_state].id if legacy_state else None,
            qty=qty,
            from_item_step_id=(
                next(s for s in item.steps if s.seq == from_seq).id
                if from_seq is not None
                else None
            ),
            from_done=from_done,
            station_id=self.station(station_code).id if station_code else None,
            reason_code_id=reason_code_id,
            occurred_at=at or utcnow(),
            supersedes_event_id=supersedes,
            user_id=user_id,
        )
        return record_event(self.db, payload, self.user).event


@pytest.fixture()
def factory(seeded):
    return Factory(seeded)


def hours_ago(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def days_ago(days: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)
