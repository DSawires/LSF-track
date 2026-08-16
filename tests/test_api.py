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

    tampered = {**body, "qty": 49}
    response = client.post("/api/events", json=tampered)
    assert response.status_code == 201
    assert response.json()["divergent"] is True
    assert response.json()["event"]["qty"] == 50  # stored row wins


def test_divergence_covers_every_client_field_not_just_the_movement(world, client):
    """A reused UUID with only a different timestamp (or note) is still the
    client bug the divergent flag exists to catch."""
    factory, _route, item = world
    body = _event_body(factory, item, 10, "queued", 50, "carpentry_1")
    assert client.post("/api/events", json=body).status_code == 201

    shifted = {**body, "occurred_at": hours_ago(6).isoformat()}
    assert client.post("/api/events", json=shifted).json()["divergent"] is True

    noted = {**body, "note": "actually the other rack"}
    assert client.post("/api/events", json=noted).json()["divergent"] is True

    verbatim = client.post("/api/events", json=body)
    assert verbatim.json()["divergent"] is False


def test_archiving_an_item_writes_an_audit_event(world, client):
    factory, _route, item = world
    assert client.post(
        "/api/events", json=_event_body(factory, item, 10, "queued", 50, "carpentry_1")
    ).status_code == 201

    result = client.delete(f"/api/items/{item.id}")
    assert result.json()["archived"] is True

    factory.db.expire_all()
    from app.models import EventType

    archive_type = factory.db.scalars(
        sa.select(EventType).where(EventType.is_archive.is_(True))
    ).one()
    audit = factory.db.scalars(
        sa.select(Event).where(
            Event.item_id == item.id, Event.event_type_id == archive_type.id
        )
    ).all()
    assert len(audit) == 1
    assert audit[0].submitted_by_user_id is not None


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


def test_batch_applies_in_occurred_at_order(world, client):
    """A queue assembled out of order (retries, interleaved devices) must not
    reject its own internally-consistent sequence: entries are applied by
    occurred_at, so the downstream move validates against the upstream one that
    occurred first, whatever the array order."""
    factory, _route, item = world
    later = _event_body(
        factory, item, 20, "queued", 50, "paint_1",
        occurred_at=hours_ago(1).isoformat(),
    )
    earlier = _event_body(
        factory, item, 10, "completed", 50, "carpentry_1",
        occurred_at=hours_ago(2).isoformat(),
    )

    response = client.post("/api/events/batch", json={"events": [later, earlier]})
    assert response.status_code == 200
    results = {r["id"]: r for r in response.json()["results"]}
    assert results[earlier["id"]]["status"] == "stored"
    assert results[later["id"]]["status"] == "stored"


def test_over_advance_rejected_over_http_with_reason(world, client):
    factory, _route, item = world
    body = _event_body(factory, item, 10, "queued", item.total_qty + 1, "carpentry_1")
    response = client.post("/api/events", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "qty"
    assert f"only {item.total_qty}" in response.json()["detail"]["reason"]


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


def test_image_upload_roundtrip_and_format_sniff(world, client, tmp_path, monkeypatch):
    monkeypatch.setenv("LSF_UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.config import get_settings
    get_settings.cache_clear()

    factory, _route, item = world
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    up = client.post(
        f"/api/items/{item.id}/images",
        files={"file": ("shelf.png", png, "image/png")},
        data={"note": "veneer chip on corner"},
    )
    assert up.status_code == 201
    body = up.json()
    assert body["content_type"] == "image/png"
    assert body["note"] == "veneer chip on corner"

    served = client.get(body["url"])
    assert served.status_code == 200
    assert served.content == png

    listed = client.get(f"/api/items/{item.id}/images").json()["images"]
    assert [i["id"] for i in listed] == [body["id"]]

    # A renamed non-image is refused regardless of its claimed content type.
    fake = client.post(
        f"/api/items/{item.id}/images",
        files={"file": ("evil.png", b"MZ\x90\x00" + b"\x00" * 64, "image/png")},
    )
    assert fake.status_code == 422
    get_settings.cache_clear()


def test_release_with_initial_stage_distribution(client, factory):
    factory.route("dist-route", ["carpentry", "paint", "packing"])
    factory.db.commit()
    template = factory.db.scalars(
        sa.select(RouteTemplate).where(RouteTemplate.code == "dist-route")
    ).one()
    item = client.post(
        "/api/items",
        json={
            "code": "DIST-1", "project_id": str(factory.project.id),
            "description": "already mid-production", "total_qty": 100,
            "drawing_revision": "A",
        },
    ).json()

    over = client.post(
        f"/api/items/{item['id']}/release",
        json={"route_template_id": str(template.id), "initial_quantities": {"10": 80, "20": 30}},
    )
    assert over.status_code == 409  # 110 > 100

    released = client.post(
        f"/api/items/{item['id']}/release",
        json={"route_template_id": str(template.id), "initial_quantities": {"10": 50, "20": 30}},
    )
    assert released.status_code == 200

    state = client.get(f"/api/items/{item['id']}").json()["state"]
    by_pos = {(p["seq"], p["state_code"]): p["qty"] for p in state["positions"]}
    assert by_pos[(10, "queued")] == 50
    assert by_pos[(20, "queued")] == 30
    assert state["unstarted_qty"] == 20


def test_item_search_and_aging_filters(world, client):
    factory, _route, item = world
    client.post("/api/events", json=_event_body(factory, item, 10, "queued", 50, "carpentry_1"))

    found = client.get("/api/items?q=api-1").json()["items"]
    assert [i["code"] for i in found] == ["API-1"]
    assert client.get("/api/items?q=zzz-nope").json()["items"] == []

    stage_id = str(factory.stage("paint").id)
    rows = client.get(f"/api/reports/aging?stage_id={stage_id}").json()["rows"]
    assert rows == []  # nothing at paint yet
    rows = client.get("/api/reports/aging?min_days=9999").json()["rows"]
    assert rows == []
    other_project = client.get(
        f"/api/reports/wip?project_id={uuid.uuid4()}"
    ).json()
    assert other_project["stages"] == []


def test_icon_kind_surfaces_on_item_list(world, client, tmp_path, monkeypatch):
    monkeypatch.setenv("LSF_UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.config import get_settings
    get_settings.cache_clear()

    factory, _route, item = world
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    snag = client.post(
        f"/api/items/{item.id}/images",
        files={"file": ("chip.png", png, "image/png")},
        data={"kind": "snag"},
    )
    icon = client.post(
        f"/api/items/{item.id}/images",
        files={"file": ("front.png", png, "image/png")},
        data={"kind": "icon"},
    )
    assert snag.json()["kind"] == "snag"
    assert icon.status_code == 201

    listed = next(i for i in client.get("/api/items").json()["items"] if i["code"] == "API-1")
    assert listed["icon_url"] == icon.json()["url"]  # icon, not the snag

    bad = client.post(
        f"/api/items/{item.id}/images",
        files={"file": ("x.png", png, "image/png")},
        data={"kind": "banner"},
    )
    assert bad.status_code == 422
    get_settings.cache_clear()


def test_remove_item_archives_with_history_deletes_without(world, client):
    factory, _route, item = world
    client.post("/api/events", json=_event_body(factory, item, 10, "queued", 50, "carpentry_1"))

    # API-1 has events: archived, not deleted; disappears from lists and reports.
    result = client.delete(f"/api/items/{item.id}")
    assert result.json() == {"archived": True, "deleted": False}
    assert client.get("/api/items").json()["items"] == []
    assert client.get("/api/reports/wip").json()["stages"] == []
    archived = client.get("/api/items?include_archived=true").json()["items"]
    assert [i["is_active"] for i in archived] == [False]

    # A never-logged item is hard-deleted.
    fresh = client.post(
        "/api/items",
        json={
            "code": "FRESH-1", "project_id": str(factory.project.id),
            "description": "no events", "total_qty": 5, "drawing_revision": "A",
        },
    ).json()
    assert client.delete(f"/api/items/{fresh['id']}").json()["deleted"] is True
    assert client.get("/api/items?include_archived=true").json()["items"][0]["code"] == "API-1"


def test_remove_project_refused_until_items_gone(world, client):
    factory, _route, item = world
    refused = client.delete(f"/api/projects/{factory.project.id}")
    assert refused.status_code == 409

    client.delete(f"/api/items/{item.id}")  # archives it
    archived = client.delete(f"/api/projects/{factory.project.id}")
    assert archived.json() == {"archived": True}


def test_unpublish_route_version(world, client):
    factory, route, _item = world
    result = client.delete(f"/api/routes/{route.id}")
    assert result.json() == {"unpublished": True}
    listed = client.get("/api/reference").json()["route_templates"]
    assert [t["is_published"] for t in listed if t["id"] == str(route.id)] == [False]
