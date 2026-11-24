"""API behaviour: auth, idempotent event posting, batch sync, reports over HTTP."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.db import utcnow
from app.main import app
from app.models import Event
from tests.conftest import hours_ago
from tests.pictures import close, decoded, picture


def _event_body(factory, item, seq, qty, station_code=None, **extra):
    step = next(s for s in item.steps if s.seq == seq)
    return {
        "id": str(extra.pop("event_id", uuid.uuid4())),
        "item_id": str(item.id),
        "item_step_id": str(step.id),
        "event_type_id": str(factory.event_type("move").id),
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
    factory, _stages, item = world
    body = _event_body(factory, item, 10, 50, "carpentry_1")

    first = client.post("/api/events", json=body)
    assert first.status_code == 201
    assert first.json()["created"] is True

    # The response was lost; the phone retries the identical payload.
    second = client.post("/api/events", json=body)
    assert second.status_code == 201
    assert second.json()["created"] is False
    assert second.json()["event"]["id"] == body["id"]

    factory.db.expire_all()
    # Exactly one stored move: the retry created nothing.
    move_count = factory.db.scalar(
        sa.select(sa.func.count()).select_from(Event).where(Event.qty == 50)
    )
    assert move_count == 1


def test_divergent_replay_returns_stored_row(world, client):
    factory, _stages, item = world
    body = _event_body(factory, item, 10, 50, "carpentry_1")
    assert client.post("/api/events", json=body).status_code == 201

    tampered = {**body, "qty": 49}
    response = client.post("/api/events", json=tampered)
    assert response.status_code == 201
    assert response.json()["divergent"] is True
    assert response.json()["event"]["qty"] == 50  # stored row wins


def test_divergence_covers_every_client_field_not_just_the_movement(world, client):
    """A reused UUID with only a different timestamp (or note) is still the
    client bug the divergent flag exists to catch."""
    factory, _stages, item = world
    body = _event_body(factory, item, 10, 50, "carpentry_1")
    assert client.post("/api/events", json=body).status_code == 201

    shifted = {**body, "occurred_at": hours_ago(6).isoformat()}
    assert client.post("/api/events", json=shifted).json()["divergent"] is True

    noted = {**body, "note": "actually the other rack"}
    assert client.post("/api/events", json=noted).json()["divergent"] is True

    verbatim = client.post("/api/events", json=body)
    assert verbatim.json()["divergent"] is False


def test_archiving_an_item_writes_an_audit_event(world, client):
    factory, _stages, item = world
    assert client.post(
        "/api/events", json=_event_body(factory, item, 10, 50, "carpentry_1")
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
    factory, _stages, item = world
    good = _event_body(factory, item, 10, 50, "carpentry_1")
    bad = {**_event_body(factory, item, 10, 10, "carpentry_1"),
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
    factory, _stages, item = world
    entry = _event_body(factory, item, 10, 20, "carpentry_1")
    other = _event_body(factory, item, 10, 20, "carpentry_1")

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


def test_received_at_is_server_set_and_cannot_be_smuggled(world, client):
    from datetime import datetime, timezone

    factory, _stages, item = world
    body = _event_body(factory, item, 10, 50, "carpentry_1")
    body["received_at"] = "1999-01-01T00:00:00+00:00"  # ignored: not a schema field

    response = client.post("/api/events", json=body)
    assert response.status_code == 201
    received = datetime.fromisoformat(response.json()["event"]["received_at"])
    assert abs((datetime.now(timezone.utc) - received).total_seconds()) < 60


def test_batch_applies_in_occurred_at_order(world, client):
    """A queue assembled out of order (retries, interleaved devices) must not
    reject its own internally-consistent sequence: entries are applied by
    occurred_at, so the downstream move validates against the upstream one that
    occurred first, whatever the array order."""
    factory, _stages, item = world
    later = _event_body(factory, item, 20, 50, "paint_1",
        occurred_at=hours_ago(1).isoformat(),
    )
    earlier = _event_body(factory, item, 10, 50, "carpentry_1",
        occurred_at=hours_ago(2).isoformat(),
    )

    response = client.post("/api/events/batch", json={"events": [later, earlier]})
    assert response.status_code == 200
    results = {r["id"]: r for r in response.json()["results"]}
    assert results[earlier["id"]]["status"] == "stored"
    assert results[later["id"]]["status"] == "stored"


def test_over_advance_rejected_over_http_with_reason(world, client):
    factory, _stages, item = world
    body = _event_body(factory, item, 10, item.total_qty + 1, "carpentry_1")
    response = client.post("/api/events", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "qty"
    assert f"only {item.total_qty}" in response.json()["detail"]["reason"]


def test_item_recent_events_endpoint(world, client):
    factory, _stages, item = world
    client.post("/api/events", json=_event_body(factory, item, 10, 50, "carpentry_1"))
    response = client.get(f"/api/items/{item.id}/events")
    assert response.status_code == 200
    events = response.json()["events"]
    assert [e["occurred_at"] for e in events] == sorted(
        (e["occurred_at"] for e in events), reverse=True
    )  # newest first
    assert [e["qty"] for e in events] == [50]  # the logged move, and nothing else:
    # creating the item writes no marker event, so an item's feed is what the
    # floor actually did.


def test_archived_item_rejects_events(client, factory):
    """An item is loggable from the moment it exists; archiving is what closes
    it. The old "not released yet" refusal has nothing left to refuse."""
    item = factory.item("ARCH-1", 10, ["carpentry"])
    item.is_active = False
    factory.db.commit()
    body = {
        "id": str(uuid.uuid4()),
        "item_id": str(item.id),
        "item_step_id": None,
        "event_type_id": str(factory.event_type("move").id),
        "qty": 5,
        "occurred_at": utcnow().isoformat(),
    }
    response = client.post("/api/events", json=body)
    assert response.status_code == 422


def test_item_list_filters_by_derived_stage(world, client):
    factory, _stages, item = world
    client.post(
        "/api/events",
        json=_event_body(factory, item, 10, 50, "carpentry_1"),
    )
    carpentry_id = str(factory.stage("carpentry").id)
    paint_id = str(factory.stage("paint").id)

    at_carpentry = client.get(f"/api/items?stage_id={carpentry_id}").json()["items"]
    at_paint = client.get(f"/api/items?stage_id={paint_id}").json()["items"]
    assert [i["code"] for i in at_carpentry] == ["API-1"]
    assert at_paint == []


def test_creating_an_item_hands_it_to_the_floor_with_nothing_to_set_up(client, factory):
    factory.db.commit()
    created = client.post(
        "/api/items",
        json={
            "code": "REL-1",
            "project_id": str(factory.project.id),
            "description": "creation test",
            "total_qty": 5,
            "drawing_revision": "B",
        },
    )
    assert created.status_code == 201
    body = created.json()
    # No stages of its own, and no sequence to keep in step with.
    assert "steps" not in body

    # Every item starts as its sample: one unit, unless the office says more.
    assert body["sample_qty"] == 1

    def start(qty):
        return client.post(
            "/api/events",
            json={
                "id": str(uuid.uuid4()),
                "item_id": body["id"],
                "stage_id": str(factory.stage("carpentry").id),
                "from_unstarted": True,
                "event_type_id": str(factory.event_type("move").id),
                "station_id": str(factory.station("carpentry_1").id),
                "qty": qty,
                "occurred_at": utcnow().isoformat(),
            },
        )

    # No separate release: the floor can log the sample immediately -- and
    # only the sample, because the batch is not on the floor until it is done.
    assert start(5).status_code == 422
    assert start(1).status_code == 201


def test_a_new_item_needs_only_a_project_a_code_and_a_quantity(client, factory):
    """What the Office tab's "New item" form posts at its barest."""
    factory.db.commit()
    created = client.post(
        "/api/items",
        json={"code": "BARE-1", "project_id": str(factory.project.id), "total_qty": 5},
    )
    assert created.status_code == 201
    body = created.json()
    assert (body["description"], body["drawing_revision"], body["sample_qty"]) == ("", "A", 1)

    # Codes are unique across the factory.
    again = client.post(
        "/api/items",
        json={"code": "BARE-1", "project_id": str(factory.project.id), "total_qty": 5},
    )
    assert again.status_code == 409


def test_a_queued_entry_in_the_old_sequence_shape_still_drains(client, factory):
    """A phone that has not reopened the app since sequences were removed may
    be holding entries that name a step. They must go in, not strand in a
    quarantine nobody can resolve -- and the item carries on under the new
    rules from wherever they left it."""
    item = factory.item("OLDQ-1", 10, ["carpentry", "packing"])
    factory.db.commit()

    old_shape = client.post(
        "/api/events",
        json={
            "id": str(uuid.uuid4()),
            "item_id": str(item.id),
            "item_step_id": str(item.steps[0].id),
            "event_type_id": str(factory.event_type("move").id),
            "station_id": str(factory.station("carpentry_1").id),
            "qty": 4,
            "occurred_at": hours_ago(2).isoformat(),
        },
    )
    assert old_shape.status_code == 201, old_shape.text

    # The same units, moved on by an entry in the new shape.
    onward = client.post(
        "/api/events",
        json={
            "id": str(uuid.uuid4()),
            "item_id": str(item.id),
            "stage_id": str(factory.stage("qc").id),
            "from_stage_id": str(factory.stage("carpentry").id),
            "event_type_id": str(factory.event_type("move").id),
            "qty": 3,
            "occurred_at": hours_ago(1).isoformat(),
        },
    )
    assert onward.status_code == 201, onward.text

    state = client.get(f"/api/items/{item.id}").json()["state"]
    assert {p["stage_code"]: p["qty"] for p in state["positions"]} == {
        None: 6, "carpentry": 1, "qc": 3,
    }
    # The sequence itself is gone from the API: nothing to edit, nothing to freeze.
    assert client.put(f"/api/items/{item.id}/steps", json={"stage_ids": []}).status_code in (404, 405)


def test_create_project_endpoint(client, factory):
    factory.db.commit()
    created = client.post(
        "/api/projects", json={"code": "villa-9", "name": "Villa 9", "client": "ACME"}
    )
    assert created.status_code == 201
    assert created.json()["code"] == "VILLA-9"  # normalised
    assert client.post("/api/projects", json={"code": "VILLA-9"}).status_code == 409


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

    factory, _stages, item = world
    png = picture("PNG", size=(3000, 1500))
    up = client.post(
        f"/api/items/{item.id}/images",
        files={"file": ("shelf.png", png, "image/png")},
        data={"note": "veneer chip on corner"},
    )
    assert up.status_code == 201
    body = up.json()
    # Kept as WebP, never as sent: the display copy at most 2048 px on its
    # long edge, proportions kept, and a thumbnail beside it.
    assert body["content_type"] == "image/webp"
    assert body["note"] == "veneer chip on corner"

    served = client.get(body["url"])
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/webp"
    fmt, size, colour = decoded(served.content)
    assert (fmt, size) == ("WEBP", (2048, 1024)) and close(colour, (200, 40, 40))
    assert body["size_bytes"] == len(served.content)
    thumb = client.get(body["thumb_url"])
    assert thumb.status_code == 200
    assert decoded(thumb.content)[:2] == ("WEBP", (320, 160))
    assert "immutable" in thumb.headers["cache-control"]

    listed = client.get(f"/api/items/{item.id}/images").json()["images"]
    assert [i["id"] for i in listed] == [body["id"]]

    # A renamed non-image is refused regardless of its claimed content type.
    fake = client.post(
        f"/api/items/{item.id}/images",
        files={"file": ("evil.png", b"MZ\x90\x00" + b"\x00" * 64, "image/png")},
    )
    assert fake.status_code == 422
    # So is one that starts like a picture but is not one.
    broken = client.post(
        f"/api/items/{item.id}/images",
        files={"file": ("half.png", png[:40], "image/png")},
    )
    assert broken.status_code == 422
    assert "not a readable image" in broken.json()["detail"]
    get_settings.cache_clear()


def test_deleting_an_image_removes_the_row_and_the_bytes(world, client, tmp_path, monkeypatch):
    """A photo is not the log: it can go, and it takes its file with it."""
    monkeypatch.setenv("LSF_UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.config import get_settings
    get_settings.cache_clear()

    factory, _stages, item = world
    png = picture()
    snag = client.post(
        f"/api/items/{item.id}/images",
        files={"file": ("chip.png", png, "image/png")},
        data={"kind": "snag"},
    ).json()
    on_disk = tmp_path / "uploads" / "images" / snag["id"]
    thumb_on_disk = tmp_path / "uploads" / "images" / f"{snag['id']}.thumb"
    assert on_disk.is_file() and thumb_on_disk.is_file()

    gone = client.delete(f"/api/images/{snag['id']}")
    assert gone.status_code == 200
    assert gone.json()["deleted"] is True
    assert not on_disk.exists() and not thumb_on_disk.exists()
    assert client.get(f"/api/items/{item.id}/images").json()["images"] == []
    assert client.get(snag["url"]).status_code == 404
    # Deleting it twice is a 404, not a 500: the second tap of a slow button.
    assert client.delete(f"/api/images/{snag['id']}").status_code == 404

    # The item and its history are untouched.
    assert client.get(f"/api/items/{item.id}").status_code == 200
    get_settings.cache_clear()


def test_deleting_an_icon_falls_back_to_the_previous_one(world, client, tmp_path, monkeypatch):
    monkeypatch.setenv("LSF_UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.config import get_settings
    get_settings.cache_clear()

    factory, _stages, item = world
    png = picture()
    first = client.post(
        f"/api/items/{item.id}/images",
        files={"file": ("old.png", png, "image/png")},
        data={"kind": "icon"},
    ).json()
    second = client.post(
        f"/api/items/{item.id}/images",
        files={"file": ("new.png", png, "image/png")},
        data={"kind": "icon"},
    ).json()

    def listed():
        return next(i for i in client.get("/api/items").json()["items"] if i["code"] == "API-1")

    assert listed()["icon_url"] == second["url"]

    client.delete(f"/api/images/{second['id']}")
    assert listed()["icon_url"] == first["url"]  # the older icon comes back

    client.delete(f"/api/images/{first['id']}")
    assert listed()["icon_url"] is None
    get_settings.cache_clear()


def test_image_deletion_is_admin_only(world, client, factory, tmp_path, monkeypatch):
    monkeypatch.setenv("LSF_UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.config import get_settings
    get_settings.cache_clear()

    _factory, _stages, item = world
    png = picture()
    snag = client.post(
        f"/api/items/{item.id}/images",
        files={"file": ("chip.png", png, "image/png")},
    ).json()
    client.post("/api/users", json={
        "username": "floorphoto",
        "display_name": "Floor",
        "password": "a-long-password",
    })
    factory.db.commit()

    with TestClient(app) as floor:
        floor.post(
            "/api/auth/login", json={"username": "floorphoto", "password": "a-long-password"}
        )
        # A non-admin can still add photos and describe them -- just not destroy them.
        assert floor.post(
            f"/api/items/{item.id}/images", files={"file": ("f.png", png, "image/png")}
        ).status_code == 201
        assert floor.patch(
            f"/api/images/{snag['id']}", json={"note": "chipped"}
        ).status_code == 200
        assert floor.delete(f"/api/images/{snag['id']}").status_code == 403

    assert client.get(snag["url"]).status_code == 200  # still there
    get_settings.cache_clear()


def test_initial_stage_distribution_at_creation(client, factory):
    """A batch that is already part-built when it reaches the system: the
    quantities are placed as ordinary moves out of "not started", and the rest
    stays there. It has no sample to run, and asking for both is refused."""
    factory.db.commit()
    stage_ids = {s["code"]: s["id"] for s in client.get("/api/reference").json()["stages"]}
    base = {
        "project_id": str(factory.project.id),
        "description": "already mid-production", "total_qty": 100,
        "sample_qty": 0, "drawing_revision": "A",
    }
    at = lambda carpentry, paint: {stage_ids["carpentry"]: carpentry, stage_ids["paint"]: paint}  # noqa: E731

    over = client.post("/api/items", json={**base, "code": "DIST-OVER", "initial_quantities": at(80, 30)})
    assert over.status_code == 422  # 110 > 100
    # The refusal is total: no half-created item is left behind.
    assert client.get("/api/items?q=DIST-OVER").json()["items"] == []

    created = client.post("/api/items", json={**base, "code": "DIST-1", "initial_quantities": at(50, 30)})
    assert created.status_code == 201

    state = client.get(f"/api/items/{created.json()['id']}").json()["state"]
    by_stage = {p["stage_code"]: p["qty"] for p in state["positions"]}
    assert by_stage["carpentry"] == 50
    assert by_stage["paint"] == 30
    assert state["unstarted_qty"] == 20

    with_sample = client.post(
        "/api/items",
        json={**base, "code": "DIST-2", "sample_qty": 1,
              "initial_quantities": {stage_ids["carpentry"]: 5}},
    )
    assert with_sample.status_code == 422
    assert "sample" in with_sample.json()["detail"]


def test_item_search_and_aging_filters(world, client):
    factory, _stages, item = world
    client.post("/api/events", json=_event_body(factory, item, 10, 50, "carpentry_1"))

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

    factory, _stages, item = world
    png = picture()

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
    factory, _stages, item = world
    client.post("/api/events", json=_event_body(factory, item, 10, 50, "carpentry_1"))

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
            "stage_ids": [str(factory.stage("carpentry").id)],
        },
    ).json()
    assert client.delete(f"/api/items/{fresh['id']}").json()["deleted"] is True
    assert client.get("/api/items?include_archived=true").json()["items"][0]["code"] == "API-1"


def test_remove_project_refused_until_items_gone(world, client):
    factory, _stages, item = world
    refused = client.delete(f"/api/projects/{factory.project.id}")
    assert refused.status_code == 409

    client.delete(f"/api/items/{item.id}")  # archives it
    archived = client.delete(f"/api/projects/{factory.project.id}")
    assert archived.json() == {"archived": True, "archived_items": 0}


def test_archiving_a_project_ignores_items_in_other_projects(client, factory):
    """Only a project's own items hold it open. An unrelated job being busy is
    not a reason to refuse, and the refusal names the items that are."""
    from app.models import Project

    empty = Project(code="EMPTY", name="Nothing here")
    factory.db.add(empty)
    factory.db.flush()
    busy = factory.item("BUSY-1", 10)  # active, and in a different project
    factory.db.commit()

    assert client.delete(f"/api/projects/{empty.id}").json() == {"archived": True, "archived_items": 0}

    refused = client.delete(f"/api/projects/{factory.project.id}")
    assert refused.status_code == 409
    assert busy.code in refused.json()["detail"]


def test_item_fields_are_editable(client, factory):
    item = factory.item("EDIT-1", 20)
    factory.db.commit()

    updated = client.patch(f"/api/items/{item.id}", json={
        "code": "EDIT-1-REV", "description": "Renamed batch", "total_qty": 25,
    })
    assert updated.status_code == 200
    assert updated.json()["code"] == "EDIT-1-REV"
    assert updated.json()["total_qty"] == 25

    clash = factory.item("TAKEN-1", 5)
    factory.db.commit()
    assert client.patch(f"/api/items/{item.id}", json={"code": clash.code}).status_code == 409


def test_batch_cannot_shrink_below_what_the_log_moved(world, client):
    """The log wins over a typed number: units already in production are a
    fact, so the batch size cannot be cut underneath them."""
    factory, _stages, item = world
    factory.log(item, 10, 30, at=hours_ago(2), station_code="carpentry_1")
    factory.db.commit()

    refused = client.patch(f"/api/items/{item.id}", json={"total_qty": 10})
    assert refused.status_code == 409
    assert "30" in refused.json()["detail"]
    assert client.patch(f"/api/items/{item.id}", json={"total_qty": 30}).status_code == 200


def test_purge_destroys_an_item_and_its_events(world, client):
    """Deleting for good takes the item's events with it. Archiving stays the
    default, and the purge is written to the server log with who did it."""
    import sqlalchemy as sa

    from app.models import Event, Item

    factory, _stages, item = world
    factory.log(item, 10, 10, at=hours_ago(2), station_code="carpentry_1")
    factory.db.commit()
    item_id = item.id

    archived = client.delete(f"/api/items/{item_id}")
    assert archived.json()["archived"] is True  # default keeps history

    purged = client.delete(f"/api/items/{item_id}?purge=true")
    assert purged.json()["deleted"] is True
    assert purged.json()["purged_events"] > 0

    factory.db.expire_all()
    assert factory.db.get(Item, item_id) is None
    assert factory.db.scalar(
        sa.select(sa.func.count()).select_from(Event).where(Event.item_id == item_id)
    ) == 0


def test_purge_survives_a_correction_chain(world, client):
    """A correction points at the event it supersedes, so an item's log is a
    graph, not a list. Purging must not trip the self-referential foreign key
    -- this is what a real item with a voided entry looks like."""
    import sqlalchemy as sa

    from app.models import Event, Item

    factory, _stages, item = world
    logged = factory.log(item, 10, 10, at=hours_ago(4), station_code="carpentry_1")
    factory.log(item, 10, 10, at=hours_ago(3), station_code="carpentry_1",
                event_type="correction", supersedes=logged.id)
    factory.db.commit()
    item_id = item.id
    before = factory.db.scalar(
        sa.select(sa.func.count()).select_from(Event).where(Event.item_id == item_id)
    )

    purged = client.delete(f"/api/items/{item_id}?purge=true")
    assert purged.status_code == 200, purged.text
    assert purged.json()["purged_events"] == before >= 2

    factory.db.expire_all()
    assert factory.db.get(Item, item_id) is None
    assert factory.db.scalar(
        sa.select(sa.func.count()).select_from(Event).where(Event.item_id == item_id)
    ) == 0


def test_project_rename_and_reactivate(client, factory):
    factory.db.commit()
    project_id = str(factory.project.id)

    renamed = client.patch(f"/api/projects/{project_id}", json={
        "name": "Renamed job", "client": "ACME",
    })
    assert renamed.json()["name"] == "Renamed job"
    assert renamed.json()["client"] == "ACME"
    assert renamed.json()["code"] == factory.project.code  # code is fixed

    assert client.delete(f"/api/projects/{project_id}").json() == {"archived": True, "archived_items": 0}
    assert client.patch(f"/api/projects/{project_id}", json={"is_active": True}).json()["is_active"] is True


def test_item_editing_is_admin_only(client, factory):
    """Floor engineers create items, log entries and bump revisions -- all of
    which append to the log rather than rewriting it. Renaming an item or resizing its batch re-labels what everyone
    else already logged, so it stays with admins."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import User
    from app.security import hash_password

    item = factory.item("LOCKED-1", 10, ["carpentry"])
    factory.db.add(User(
        username="floor2",
        display_name="Floor Engineer",
        password_hash=hash_password("floor-pw"),
        is_admin=False,
    ))
    factory.db.commit()

    with TestClient(app) as floor:
        assert floor.post(
            "/api/auth/login", json={"username": "floor2", "password": "floor-pw"}
        ).status_code == 200
        assert floor.patch(
            f"/api/items/{item.id}", json={"code": "SNEAKY-1"}
        ).status_code == 403

    # the admin session still can
    assert client.patch(f"/api/items/{item.id}", json={"code": "RENAMED-1"}).status_code == 200


def test_an_item_with_photos_can_be_deleted_for_good(world, client, tmp_path, monkeypatch):
    """The photo rows must leave before the item does, or the foreign key
    between them refuses the delete."""
    monkeypatch.setenv("LSF_UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.config import get_settings

    get_settings.cache_clear()
    factory, _stages, item = world
    png = picture()
    for kind in ("icon", "snag"):
        made = client.post(
            f"/api/items/{item.id}/images", files={"file": ("p.png", png, "image/png")}, data={"kind": kind}
        )
        assert made.status_code == 201
    stored = list((tmp_path / "uploads" / "images").iterdir())
    assert len(stored) == 4  # a display copy and a thumbnail each

    gone = client.delete(f"/api/items/{item.id}?purge=true")
    assert gone.status_code == 200 and gone.json()["deleted"] is True
    assert list((tmp_path / "uploads" / "images").iterdir()) == []
