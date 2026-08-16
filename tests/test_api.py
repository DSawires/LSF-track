"""API behaviour: auth, idempotent event posting, batch sync, reports over HTTP."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.db import utcnow
from app.main import app
from app.models import Event, RouteTemplate
from tests.conftest import hours_ago


@pytest.fixture()
def client(factory):
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


def _event_body(factory, item, seq, state_code, qty, station_code=None, **extra):
    step = next(s for s in item.steps if s.seq == seq)
    return {
        "id": str(extra.pop("event_id", uuid.uuid4())),
        "item_id": str(item.id),
        "item_step_id": str(step.id),
        "event_type_id": str(factory.event_type("move").id),
        "state_id": str(factory.state(state_code).id),
        "station_id": str(factory.station(station_code).id) if station_code else None,
        "qty": qty,
        "occurred_at": hours_ago(1).isoformat(),
        **extra,
    }


def test_login_bad_password_rejected(factory):
    factory.db.commit()
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login", json={"username": "test", "password": "nope"}
        )
        assert response.status_code == 401
        assert client.get("/api/auth/me").status_code == 401


def test_event_post_is_idempotent(world, client):
    factory, _route, item = world
    body = _event_body(factory, item, 10, "queued", 50, "carpentry_1")

    first = client.post("/api/events", json=body)
    assert first.status_code == 201
    assert first.json()["created"] is True

    # The response was lost; the phone retries the identical payload.
    second = client.post("/api/events", json=body)
    assert second.status_code == 201
    assert second.json()["created"] is False
    assert second.json()["event"]["id"] == body["id"]

    factory.db.expire_all()
    # One release event from the fixture plus exactly one stored move: the retry
    # created nothing.
    move_count = factory.db.scalar(
        sa.select(sa.func.count()).select_from(Event).where(Event.qty == 50)
    )
    assert move_count == 1


def test_divergent_replay_returns_stored_row(world, client):
    factory, _route, item = world
    body = _event_body(factory, item, 10, "queued", 50, "carpentry_1")
    assert client.post("/api/events", json=body).status_code == 201

    tampered = {**body, "qty": 999}
    response = client.post("/api/events", json=tampered)
    assert response.status_code == 201
    assert response.json()["divergent"] is True
    assert response.json()["event"]["qty"] == 50  # stored row wins


def test_batch_sync_reports_per_event_outcomes(world, client):
    factory, _route, item = world
    good = _event_body(factory, item, 10, "queued", 50, "carpentry_1")
    bad = {**_event_body(factory, item, 10, "queued", 10, "carpentry_1"),
           "item_id": str(uuid.uuid4())}

    response = client.post("/api/events/batch", json={"events": [good, bad]})
    assert response.status_code == 200
    results = {r["id"]: r for r in response.json()["results"]}
    assert results[good["id"]]["status"] == "stored"
    assert results[bad["id"]]["status"] == "rejected"

    # Replaying the whole batch (the ack was lost) stores nothing new.
    replay = client.post("/api/events/batch", json={"events": [good, bad]})
    assert {r["status"] for r in replay.json()["results"]} == {"duplicate", "rejected"}


def test_batch_with_internal_duplicate_stores_once_and_acks_both(world, client):
    """The same entry twice in one drain (a client bug) must not lose neighbours."""
    factory, _route, item = world
    entry = _event_body(factory, item, 10, "queued", 50, "carpentry_1")
    other = _event_body(factory, item, 10, "in_progress", 50, "carpentry_1")

    response = client.post("/api/events/batch", json={"events": [entry, entry, other]})
    statuses = [r["status"] for r in response.json()["results"]]
    assert statuses == ["stored", "duplicate", "stored"]

    factory.db.expire_all()
    stored = factory.db.scalars(
        sa.select(Event).where(
            Event.id.in_([uuid.UUID(entry["id"]), uuid.UUID(other["id"])])
        )
    ).all()
    assert len(stored) == 2


def test_item_recent_events_endpoint(world, client):
    factory, _route, item = world
    client.post("/api/events", json=_event_body(factory, item, 10, "queued", 50, "carpentry_1"))
    response = client.get(f"/api/items/{item.id}/events")
    assert response.status_code == 200
    events = response.json()["events"]
    assert [e["occurred_at"] for e in events] == sorted(
        (e["occurred_at"] for e in events), reverse=True
    )  # newest first
    assert any(e["qty"] == 50 for e in events)  # the logged move
    assert any(e["qty"] == 0 for e in events)  # the release marker


def test_unreleased_item_rejects_events(client, factory):
    item = factory.item("UNREL-1", 10)  # no route -> not released
    factory.db.commit()
    body = {
        "id": str(uuid.uuid4()),
        "item_id": str(item.id),
        "item_step_id": None,
        "event_type_id": str(factory.event_type("move").id),
        "state_id": str(factory.state("queued").id),
        "qty": 5,
        "occurred_at": utcnow().isoformat(),
    }
    response = client.post("/api/events", json=body)
    assert response.status_code == 422


def test_item_list_filters_by_derived_stage(world, client):
    factory, _route, item = world
    client.post(
        "/api/events",
        json=_event_body(factory, item, 10, "queued", 50, "carpentry_1"),
    )
    carpentry_id = str(factory.stage("carpentry").id)
    paint_id = str(factory.stage("paint").id)

    at_carpentry = client.get(f"/api/items?stage_id={carpentry_id}").json()["items"]
    at_paint = client.get(f"/api/items?stage_id={paint_id}").json()["items"]
    assert [i["code"] for i in at_carpentry] == ["API-1"]
    assert at_paint == []


def test_release_snapshots_route(client, factory):
    factory.route("rel-route", ["carpentry", "packing"])
    factory.db.commit()
    template = factory.db.scalars(
        sa.select(RouteTemplate).where(RouteTemplate.code == "rel-route")
    ).one()

    created = client.post(
        "/api/items",
        json={
            "code": "REL-1",
            "project_id": str(factory.project.id),
            "description": "release test",
            "total_qty": 5,
            "drawing_revision": "B",
        },
    )
    assert created.status_code == 201
    item_id = created.json()["id"]

    released = client.post(
        f"/api/items/{item_id}/release",
        json={"route_template_id": str(template.id)},
    )
    assert released.status_code == 200
    assert [s["seq"] for s in released.json()["steps"]] == [10, 20]

    # Releasing twice is refused; the snapshot is immutable.
    again = client.post(
        f"/api/items/{item_id}/release",
        json={"route_template_id": str(template.id)},
    )
    assert again.status_code == 409


def test_create_project_endpoint(client, factory):
    factory.db.commit()
    created = client.post(
        "/api/projects", json={"code": "villa-9", "name": "Villa 9", "client": "ACME"}
    )
    assert created.status_code == 201
    assert created.json()["code"] == "VILLA-9"  # normalised
    assert client.post("/api/projects", json={"code": "VILLA-9"}).status_code == 409


def test_create_route_versions_and_release_isolation(client, factory):
    """Posting an existing route code makes the next version; released items
    keep the version they left against."""
    factory.db.commit()
    ref = client.get("/api/reference").json()
    stage_ids = {s["code"]: s["id"] for s in ref["stages"]}

    v1 = client.post(
        "/api/routes",
        json={"code": "Chairs Standard", "name": "Chairs", "stage_ids": [stage_ids["carpentry"], stage_ids["packing"]]},
    ).json()
    assert (v1["code"], v1["version"]) == ("chairs_standard", 1)
    assert [s["seq"] for s in v1["steps"]] == [10, 20]

    item = client.post(
        "/api/items",
        json={
            "code": "CHR-V1", "project_id": str(factory.project.id),
            "description": "against v1", "total_qty": 4, "drawing_revision": "A",
        },
    ).json()
    released = client.post(f"/api/items/{item['id']}/release", json={"route_template_id": v1["id"]})
    v1_steps = [s["stage_id"] for s in released.json()["steps"]]

    v2 = client.post(
        "/api/routes",
        json={"code": "chairs_standard", "stage_ids": [stage_ids["carpentry"], stage_ids["paint"], stage_ids["packing"]]},
    ).json()
    assert v2["version"] == 2
    assert v2["name"] == "Chairs"  # inherited from v1 when omitted

    # The released item still has its v1 snapshot: two steps, no paint.
    fetched = client.get(f"/api/items/{item['id']}").json()
    assert [s["stage_id"] for s in fetched["steps"]] == v1_steps
    assert client.post("/api/routes", json={"code": "empty", "stage_ids": []}).status_code == 422


def test_reference_payload_contains_runtime_added_stage(client, factory):
    factory.add_stage("gilding", sort=99)
    factory.db.commit()
    codes = [s["code"] for s in client.get("/api/reference").json()["stages"]]
    assert "gilding" in codes


def test_reports_require_auth(factory):
    factory.db.commit()
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/reports/wip").status_code == 401
        assert anonymous.get("/api/reports/aging").status_code == 401
