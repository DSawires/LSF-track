"""The CLAUDE.md contract: a stage added at runtime changes output, never code.

Every test here inserts a brand-new stage (and in one case a new state) after the
application is fully wired, then exercises the reports and the release path. If a
change to the codebase makes any of these fail, that change hardcoded a stage
assumption and is the thing to fix.
"""

from __future__ import annotations

from app.services import reports
from app.services.derivation import derive
from tests.conftest import days_ago, hours_ago


def test_runtime_stage_flows_through_release_and_reports(factory):
    # The factory adds a "polishing" stage today, with no deploy.
    factory.add_stage("polishing", sort=35, requires_station=False)
    route = factory.route("r2", ["carpentry", "polishing", "packing"])
    item = factory.item("POL-1", 25, route)

    factory.log(item, 10, "completed", 25, at=days_ago(2), station_code="carpentry_1")
    factory.log(item, 20, "in_progress", 25, at=days_ago(1))

    wip = reports.wip_report(factory.db)
    polishing_row = next(
        row for row in wip["stages"] if row["stage"]["code"] == "polishing"
    )
    assert polishing_row["total_qty"] == 25
    assert polishing_row["by_state"]["in_progress"] == 25

    aging = reports.aging_report(factory.db)
    row = next(
        r for r in aging["rows"] if r["stage"] and r["stage"]["code"] == "polishing"
    )
    assert 0.9 < row["days_in_state"] < 1.1


def test_new_template_version_leaves_inflight_items_alone(factory):
    factory.add_stage("sanding", sort=15)
    route_v1 = factory.route("beds", ["carpentry", "packing"], version=1)
    inflight = factory.item("BED-1", 10, route_v1)
    factory.log(inflight, 10, "queued", 10, at=hours_ago(3), station_code="carpentry_1")

    # New version inserts sanding between the existing steps using the seq gaps.
    route_v2 = factory.route("beds", ["carpentry", "sanding", "packing"], version=2)
    fresh = factory.item("BED-2", 10, route_v2)

    assert [s.seq for s in inflight.steps] == [10, 20]
    assert len(fresh.steps) == 3
    # The in-flight item still derives against its snapshot, untouched.
    ledger = derive(factory.db, item_ids=[inflight.id]).ledger_for(inflight.id)
    assert len([p for p in ledger.positions if not p.is_unstarted]) == 2 * 3


def test_wip_report_iterates_states_from_the_table(factory):
    """A fourth state added at runtime appears in every report row."""
    from app.models import EventState

    factory.db.add(
        EventState(code="drying", name="Drying", sort_order=25, is_complete=False)
    )
    factory.db.flush()

    route = factory.route("r3", ["paint"])
    item = factory.item("DRY-1", 8, route)
    factory.log(item, 10, "in_progress", 8, at=hours_ago(4), station_code="paint_1")
    factory.log(item, 10, "drying", 8, at=hours_ago(2), station_code="paint_1")

    wip = reports.wip_report(factory.db)
    paint_row = next(row for row in wip["stages"] if row["stage"]["code"] == "paint")
    assert paint_row["by_state"]["drying"] == 8
    assert "drying" in [s["code"] for s in wip["states"]]


def test_queue_depth_comes_from_state_order_not_name(factory):
    """Rename-proof: queue depth keys off the earliest state by sort_order."""
    route = factory.route("r4", ["carpentry"])
    item = factory.item("Q-1", 12, route)
    factory.log(item, 10, "queued", 12, at=hours_ago(1), station_code="carpentry_1")

    wip = reports.wip_report(factory.db)
    row = next(r for r in wip["stages"] if r["stage"]["code"] == "carpentry")
    assert row["queue_qty"] == 12
    assert wip["queue_state_code"] == "queued"


def test_stage_and_station_added_through_the_admin_api(world, client):
    """The full CLAUDE.md procedure, end to end, through HTTP: add a stage and
    a station via the admin endpoints, cut a new route version with the stage
    slotted in, release an item, log against it, and see it in the WIP report.
    Zero code changes, zero deploys."""
    factory, _route, _item = world

    stage = client.post("/api/stages", json={
        "code": "glass_shop",
        "name": "Glass shop",
        "sort_order": 45,
        "requires_station": True,
    }).json()
    station = client.post("/api/stations", json={
        "stage_id": stage["id"],
        "code": "glass_shop_1",
        "name": "Glass shop 1",
    })
    assert station.status_code == 201

    carpentry = next(
        s for s in client.get("/api/reference").json()["stages"] if s["code"] == "carpentry"
    )
    route = client.post("/api/routes", json={
        "code": "glazed", "name": "Glazed casegoods",
        "stage_ids": [carpentry["id"], stage["id"]],
    }).json()

    item = client.post("/api/items", json={
        "code": "GLZ-1", "project_id": _project_id(client), "description": "Glazed cabinet",
        "total_qty": 4, "drawing_revision": "A",
    }).json()
    assert client.post(f"/api/items/{item['id']}/release", json={
        "route_template_id": route["id"],
    }).status_code == 200

    steps = client.get(f"/api/items/{item['id']}").json()["steps"]
    glass_step = next(s for s in steps if s["stage_id"] == stage["id"])
    states = client.get("/api/reference").json()["states"]
    queued = next(s for s in states if s["sort_order"] == min(x["sort_order"] for x in states))
    move = next(
        t for t in client.get("/api/reference").json()["event_types"]
        if t["moves_quantity"] and not t["is_rework"] and not t["is_correction"]
    )
    import uuid as _uuid
    from datetime import datetime, timezone
    posted = client.post("/api/events", json={
        "id": str(_uuid.uuid4()),
        "item_id": item["id"],
        "item_step_id": glass_step["id"],
        "station_id": station.json()["id"],
        "event_type_id": move["id"],
        "state_id": queued["id"],
        "qty": 4,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    })
    assert posted.status_code == 201

    wip = client.get("/api/reports/wip").json()
    row = next(r for r in wip["stages"] if r["stage"]["code"] == "glass_shop")
    assert row["total_qty"] == 4


def _project_id(client) -> str:
    return client.get("/api/reference").json()["projects"][0]["id"]


def test_stage_admin_endpoints_require_admin(factory, client):
    """Floor engineers log events; only admins reshape the factory."""
    import sqlalchemy as sa

    from app.models import User
    from app.security import hash_password

    factory.db.add(User(
        username="floor",
        display_name="Floor Engineer",
        password_hash=hash_password("floor-pw"),
        is_admin=False,
    ))
    factory.db.commit()

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as floor:
        assert floor.post(
            "/api/auth/login", json={"username": "floor", "password": "floor-pw"}
        ).status_code == 200
        denied = floor.post("/api/stages", json={"code": "sneaky", "name": "Sneaky"})
        assert denied.status_code == 403
        some_stage = factory.db.scalars(sa.select(type(factory.stage("paint"))).limit(1)).first()
        assert floor.patch(
            f"/api/stages/{some_stage.id}", json={"is_terminal": True}
        ).status_code == 403
        assert floor.post("/api/stations", json={
            "stage_id": str(some_stage.id), "code": "sneaky_1", "name": "Sneaky 1",
        }).status_code == 403


def test_terminal_stage_must_be_last_in_a_route(world, client):
    factory, _route, _item = world
    reference = client.get("/api/reference").json()
    packing = next(s for s in reference["stages"] if s["is_terminal"])
    carpentry = next(s for s in reference["stages"] if s["code"] == "carpentry")

    response = client.post("/api/routes", json={
        "code": "backwards", "name": "Backwards",
        "stage_ids": [packing["id"], carpentry["id"]],
    })
    assert response.status_code == 422
    assert "terminal" in response.json()["detail"]


def test_seed_rerun_does_not_clobber_operator_edits(factory):
    """The entrypoint reruns the seed on every container start; a behaviour
    flag tuned through the admin UI must survive it."""
    from seeds.seed import run as run_seed

    paint = factory.stage("paint")
    paint.requires_station = False
    paint.name = "Paint & lacquer"
    factory.db.flush()

    run_seed(factory.db)
    factory.db.flush()
    factory.db.refresh(paint)
    assert paint.requires_station is False
    assert paint.name == "Paint & lacquer"


def test_whole_batch_stage_rejects_partial_moves(factory):
    """allows_partial_qty=False is enforced, not decorative: a move that leaves
    units behind upstream is rejected."""
    import pytest

    from app.services.events import EventRejected

    factory.add_stage("kiln", sort=25, allows_partial_qty=False)
    route = factory.route("r-kiln", ["carpentry", "kiln"])
    item = factory.item("KILN-1", 20, route)

    factory.log(item, 10, "completed", 20, at=hours_ago(5), station_code="carpentry_1")
    with pytest.raises(EventRejected, match="partial"):
        factory.log(item, 20, "queued", 8, at=hours_ago(2))
    # The whole batch moves fine.
    factory.log(item, 20, "queued", 20, at=hours_ago(1))


def test_no_stage_codes_hardcoded_outside_seeds_and_tests():
    """Enforce CLAUDE.md rule 2 mechanically: the seeded stage codes must not
    appear as literals anywhere in app/."""
    import pathlib
    import re

    from seeds.seed import STAGES

    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text()
        for code, *_ in STAGES:
            if re.search(rf"[\"']{re.escape(code)}[\"']", text):
                offenders.append(f"{path.name}: '{code}'")
    assert not offenders, f"stage codes hardcoded in app/: {offenders}"
