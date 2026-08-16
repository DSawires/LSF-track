"""A small demo factory: two projects, four items, a week of events.

For trying the app locally. Idempotent: keyed on codes, safe to re-run.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    Event,
    EventState,
    EventType,
    Item,
    Project,
    RouteTemplate,
    RouteTemplateStep,
    Stage,
    User,
)
from app.schemas import EventCreate
from app.security import hash_password
from app.services.events import record_event
from app.services.release import release_item
from seeds.seed import run as run_seed


def run(db: Session) -> None:
    run_seed(db)

    user = db.scalars(sa.select(User).where(User.username == "demo")).first()
    if user is None:
        user = User(
            username="demo",
            display_name="Demo Engineer",
            password_hash=hash_password("demo"),
            is_admin=True,
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
    template = db.scalars(
        sa.select(RouteTemplate).where(
            RouteTemplate.code == "casegoods", RouteTemplate.version == 1
        )
    ).first()
    if template is None:
        template = RouteTemplate(code="casegoods", version=1, name="Casegoods standard")
        db.add(template)
        db.flush()
        for seq, stage_code in [
            (10, "carpentry"),
            (20, "veneer"),
            (30, "paint"),
            (40, "qc"),
            (50, "packing"),
        ]:
            db.add(
                RouteTemplateStep(
                    route_template_id=template.id, seq=seq, stage_id=stage_ids[stage_code]
                )
            )
        db.flush()

    now = utcnow()
    items_spec = [
        ("BST-120", "HOTEL-A", "Bedside table, walnut", 120, 9),
        ("WRD-040", "HOTEL-A", "Wardrobe, 3-door", 40, 6),
        ("DSK-015", "VILLA-B", "Writing desk", 15, 4),
        ("CHR-060", "VILLA-B", "Dining chair", 60, 0),
    ]
    states = {s.code: s for s in db.scalars(sa.select(EventState))}
    move_type = db.scalars(sa.select(EventType).where(EventType.code == "move")).first()
    # First station of each stage, for stages that record one.
    stations_by_stage: dict = {}
    from app.models import Station

    for station in db.scalars(sa.select(Station).order_by(Station.sort_order)):
        stations_by_stage.setdefault(station.stage_id, station.id)

    for code, project_code, description, qty, days_ago in items_spec:
        item = db.scalars(sa.select(Item).where(Item.code == code)).first()
        if item is not None:
            continue
        item = Item(
            code=code,
            project_id=projects[project_code].id,
            description=description,
            total_qty=qty,
            drawing_revision="A",
        )
        db.add(item)
        db.flush()
        if days_ago == 0:
            continue  # left unreleased on purpose
        released = now - timedelta(days=days_ago)
        steps = release_item(db, item, template, user, released_at=released)

        # Walk some quantity down the route, leaving a spread of ages behind.
        moment = released
        moving = qty
        for depth, step in enumerate(steps):
            if moving <= 0:
                break
            for state_code in ("queued", "in_progress", "completed"):
                moment += timedelta(hours=6 + depth * 3)
                if moment >= now:
                    break
                _log(
                    db,
                    user,
                    item,
                    step.id,
                    move_type.id,
                    states[state_code].id,
                    moving,
                    moment,
                    stations_by_stage.get(step.stage_id),
                )
                if state_code == "completed":
                    # Leave roughly a third behind at each stage boundary.
                    moving -= max(moving // 3, 0) if depth < len(steps) - 1 else 0
            if moment >= now:
                break


def _log(db, user, item, step_id, type_id, state_id, qty, moment, station_id=None) -> None:
    if db.scalars(
        sa.select(Event).where(
            Event.item_id == item.id,
            Event.item_step_id == step_id,
            Event.state_id == state_id,
        )
    ).first():
        return
    record_event(
        db,
        EventCreate(
            id=uuid.uuid4(),
            item_id=item.id,
            item_step_id=step_id,
            event_type_id=type_id,
            state_id=state_id,
            qty=qty,
            station_id=station_id,
            occurred_at=moment,
        ),
        user,
    )
