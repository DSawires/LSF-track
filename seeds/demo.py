"""A small demo factory: two projects, four items, a week of events.

For trying the app locally. Idempotent: keyed on codes, safe to re-run.
Refuses to touch a database that already holds real (non-demo) projects, so
a stray LSF_DEMO=true on a production instance cannot mix sample items --
or the demo/demo login -- into factory data.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    EventType,
    Item,
    Project,
    Stage,
    User,
)
from app.schemas import EventCreate
from app.security import hash_password
from app.services.events import record_event
from app.services.items import assign_steps
from seeds.seed import run as run_seed


DEMO_PROJECT_CODES = {"HOTEL-A", "VILLA-B"}


def run(db: Session) -> None:
    existing = {p.code for p in db.scalars(sa.select(Project))}
    if existing - DEMO_PROJECT_CODES:
        print(
            "demo: refusing to load sample data — this database already holds "
            f"real projects ({', '.join(sorted(existing - DEMO_PROJECT_CODES))}). "
            "Unset LSF_DEMO."
        )
        return

    run_seed(db)

    user = db.scalars(sa.select(User).where(User.username == "demo")).first()
    if user is None:
        # A well-known password never gets admin rights: the demo account can
        # log events and browse, and that is all a demo needs.
        user = User(
            username="demo",
            display_name="Demo Engineer",
            password_hash=hash_password("demo"),
            is_admin=False,
        )
        db.add(user)
        db.flush()

    projects = {}
    for code, name, client in [
        ("HOTEL-A", "Grand Palace refit", "Grand Palace Hotels"),
        ("VILLA-B", "Villa Beatrice", "Private client"),
    ]:
        project = db.scalars(sa.select(Project).where(Project.code == code)).first()
        if project is None:
            project = Project(code=code, name=name, client=client)
            db.add(project)
            db.flush()
        projects[code] = project

    stage_ids = {s.code: s.id for s in db.scalars(sa.select(Stage))}
    # The standard casegoods sequence. It is not a template any more -- each
    # demo item simply gets its own copy of it, which is what the app does.
    casegoods = [
        stage_ids[code] for code in ("carpentry", "veneer", "paint", "qc", "packing")
    ]

    now = utcnow()
    items_spec = [
        # code, project, description, batch qty, days ago, sample qty
        ("BST-120", "HOTEL-A", "Bedside table, walnut", 120, 9, 1),
        ("WRD-040", "HOTEL-A", "Wardrobe, 3-door", 40, 6, 1),
        ("DSK-015", "VILLA-B", "Writing desk", 15, 4, 0),
        # 0 days: created just now, so the walk below logs nothing against it --
        # its sample is waiting to start.
        ("CHR-060", "VILLA-B", "Dining chair", 60, 0, 1),
    ]
    move_type = db.scalars(sa.select(EventType).where(EventType.code == "move")).first()
    finish_type = db.scalars(sa.select(EventType).where(EventType.code == "finish")).first()
    # First station of each stage, for stages that record one.
    stations_by_stage: dict = {}
    from app.models import Station

    for station in db.scalars(sa.select(Station).order_by(Station.sort_order)):
        stations_by_stage.setdefault(station.stage_id, station.id)

    for code, project_code, description, qty, days_ago, sample_qty in items_spec:
        item = db.scalars(sa.select(Item).where(Item.code == code)).first()
        if item is not None:
            continue
        started = now - timedelta(days=days_ago)
        item = Item(
            code=code,
            project_id=projects[project_code].id,
            description=description,
            total_qty=qty,
            sample_qty=sample_qty,
            drawing_revision="A",
            created_at=started,
        )
        db.add(item)
        db.flush()
        steps = assign_steps(db, item, casegoods, created_at=started)
        moment = started

        # The sample walks the whole sequence first; the batch exists after.
        if sample_qty:
            for step in steps:
                moment += timedelta(hours=4)
                if moment >= now:
                    break
                _log(db, user, item, step, move_type, sample_qty, moment, stations_by_stage)
            moment += timedelta(hours=4)
            if moment >= now:
                continue
            _log(db, user, item, None, finish_type, sample_qty, moment, stations_by_stage)

        # Walk the batch down the stages, leaving a spread of ages behind.
        moving = qty
        for depth, step in enumerate(steps):
            moment += timedelta(hours=18 + depth * 9)
            if moving <= 0 or moment >= now:
                break
            _log(db, user, item, step, move_type, moving, moment, stations_by_stage)
            # Leave roughly a third behind at each stage.
            moving -= moving // 3


def _log(db, user, item, step, event_type, qty, moment, stations_by_stage) -> None:
    record_event(
        db,
        EventCreate(
            id=uuid.uuid4(),
            item_id=item.id,
            item_step_id=step.id if step else None,
            event_type_id=event_type.id,
            qty=qty,
            station_id=stations_by_stage.get(step.stage_id) if step else None,
            occurred_at=moment,
        ),
        user,
    )
