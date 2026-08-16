from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

os.environ["LSF_DATABASE_URL"] = "sqlite://"
os.environ["LSF_SECURE_COOKIES"] = "false"
os.environ["LSF_SECRET_KEY"] = "test-secret"

from app import db as app_db  # noqa: E402
from app.db import Base, utcnow  # noqa: E402
from app.models import (  # noqa: E402
    Event,
    EventState,
    EventType,
    Item,
    Project,
    RouteTemplate,
    RouteTemplateStep,
    Stage,
    Station,
    User,
)
from app.schemas import EventCreate  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services.events import record_event  # noqa: E402
from app.services.release import release_item  # noqa: E402
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
    route = factory.route("api-route", ["carpentry", "paint", "packing"])
    item = factory.item("API-1", 50, route)
    factory.db.commit()
    return factory, route, item


@pytest.fixture(autouse=True)
def _clean_throttle():
    from app.storage import get_storage
    from app.throttle import login_throttle

    login_throttle.reset()
    get_storage.cache_clear()
    yield
    login_throttle.reset()
    get_storage.cache_clear()


@pytest.fixture()
def db(tmp_path):
    # File-backed rather than :memory:, because the API tests hit the app through
    # TestClient and FastAPI runs sync endpoints in worker threads -- an in-memory
    # SQLite would give each thread its own empty database.
    from app.config import get_settings

    os.environ["LSF_DATABASE_URL"] = f"sqlite:///{tmp_path}/test.sqlite3"
    get_settings.cache_clear()
    app_db.reset_engine()
    engine = app_db.get_engine()
    Base.metadata.create_all(engine)
    session = app_db.get_sessionmaker()()
    try:
        yield session
        session.rollback()
    finally:
        session.close()
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

    def state(self, code: str) -> EventState:
        return self.db.scalars(sa.select(EventState).where(EventState.code == code)).one()

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

    def route(self, code: str, stage_codes: list[str], version: int = 1) -> RouteTemplate:
        template = RouteTemplate(code=code, version=version, name=code)
        self.db.add(template)
        self.db.flush()
        for index, stage_code in enumerate(stage_codes):
            self.db.add(
                RouteTemplateStep(
                    route_template_id=template.id,
                    seq=(index + 1) * 10,
                    stage_id=self.stage(stage_code).id,
                )
            )
        self.db.flush()
        return template

    def item(self, code: str, qty: int, route: RouteTemplate | None = None) -> Item:
        item = Item(
            code=code,
            project_id=self.project.id,
            description=f"{code} test batch",
            total_qty=qty,
            drawing_revision="A",
        )
        self.db.add(item)
        self.db.flush()
        if route is not None:
            release_item(self.db, item, route, self.user)
        return item

    def log(
        self,
        item: Item,
        step_seq: int,
        state_code: str,
        qty: int,
        at: datetime | None = None,
        event_type: str = "move",
        station_code: str | None = None,
        reason_code_id=None,
        event_id: uuid.UUID | None = None,
        supersedes: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> Event:
        step = next(s for s in item.steps if s.seq == step_seq)
        payload = EventCreate(
            id=event_id or uuid.uuid4(),
            item_id=item.id,
            item_step_id=step.id,
            event_type_id=self.event_type(event_type).id,
            state_id=self.state(state_code).id,
            qty=qty,
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
