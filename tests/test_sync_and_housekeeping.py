"""The change token phones sync against, and the housekeeping around a log
that people are allowed to tidy: voids that show as voided, archives that can
be undone, whole projects leaving the floor at once, flags that age out."""

from __future__ import annotations

import io
import logging
import uuid
from datetime import timedelta

import sqlalchemy as sa

from app.db import utcnow
from app.models import Event, Item, Project
from app.services.derivation import derive
from app.services.sync import items_token

from tests.conftest import days_ago, hours_ago


def _post_move(client, factory, item, qty=1, occurred_at=None, **extra):
    return client.post("/api/events", json={
        "id": str(uuid.uuid4()),
        "item_id": str(item.id),
        "item_step_id": str(item.steps[0].id),
        "station_id": str(factory.station("carpentry_1").id),
        "event_type_id": str(factory.event_type("move").id),
        "qty": qty,
        "occurred_at": (occurred_at or hours_ago(1)).isoformat(),
        **extra,
    })


# -- the change token ---------------------------------------------------------


def test_token_holds_still_on_an_idle_floor_and_moves_with_the_data(world, client):
    """A phone skips the item-list replay while this is unchanged, so it must
    change for everything that list is drawn from -- and for nothing else."""
    factory, _stages, item = world

    first = client.get("/api/reference").json()["items_token"]
    listing = client.get("/api/items").json()
    assert listing["token"] == first
    assert client.get("/api/reference").json()["items_token"] == first  # idle

    seen = {first}

    def changed(label) -> None:
        token = client.get("/api/reference").json()["items_token"]
        assert token not in seen, f"{label} did not change the token"
        seen.add(token)

    assert _post_move(client, factory, item).status_code == 201
    changed("a logged event")

    assert client.patch(f"/api/items/{item.id}", json={"description": "renamed"}).status_code == 200
    changed("an item edit")

    stage_id = factory.stage("carpentry").id
    assert client.patch(f"/api/stages/{stage_id}", json={"max_days_in_state": 9}).status_code == 200
    changed("a stage threshold")

    # A stage added at runtime, and an item that uses it.
    added = client.post("/api/stages", json={"code": "gilding", "name": "Gilding"})
    assert added.status_code == 201
    changed("a new stage")
    created = client.post("/api/items", json={
        "code": "TOK-2", "project_id": str(factory.project.id), "description": "x",
        "total_qty": 3, "drawing_revision": "A", "stage_ids": [added.json()["id"]],
    })
    assert created.status_code == 201, created.text
    changed("a new item")

    # Things the item list is not drawn from leave it alone.
    still = client.get("/api/reference").json()["items_token"]
    assert client.post("/api/status/banner", json={"color": "red", "message": "x"}).status_code == 200
    assert client.get("/api/reference").json()["items_token"] == still


def test_token_changes_when_events_are_purged(world, client):
    factory, _stages, item = world
    factory.log(item, 10, 5, at=hours_ago(2), station_code="carpentry_1")
    factory.db.commit()
    before = items_token(factory.db)
    assert client.delete(f"/api/items/{item.id}?purge=true").json()["deleted"] is True
    factory.db.expire_all()
    assert items_token(factory.db) != before


# -- voids --------------------------------------------------------------------


def test_recent_entries_say_which_ones_are_voided(world, client):
    factory, _stages, item = world
    kept = _post_move(client, factory, item, qty=2, occurred_at=hours_ago(3)).json()["event"]
    wrong = _post_move(client, factory, item, qty=3, occurred_at=hours_ago(2)).json()["event"]
    void = client.post("/api/events", json={
        "id": str(uuid.uuid4()),
        "item_id": str(item.id),
        "event_type_id": str(factory.event_type("correction").id),
        "qty": 0,
        "supersedes_event_id": wrong["id"],
        "occurred_at": hours_ago(1).isoformat(),
    })
    assert void.status_code == 201, void.text

    rows = {e["id"]: e for e in client.get(f"/api/items/{item.id}/events").json()["events"]}
    assert rows[wrong["id"]]["superseded"] is True
    assert rows[kept["id"]]["superseded"] is False
    assert rows[void.json()["event"]["id"]]["superseded"] is False


# -- batches ------------------------------------------------------------------


def test_one_timestamp_without_a_timezone_does_not_take_the_batch_down(world, client):
    factory, _stages, item = world
    base = {
        "item_id": str(item.id),
        "item_step_id": str(item.steps[0].id),
        "station_id": str(factory.station("carpentry_1").id),
        "event_type_id": str(factory.event_type("move").id),
        "qty": 1,
    }
    good, naive = str(uuid.uuid4()), str(uuid.uuid4())
    response = client.post("/api/events/batch", json={"events": [
        {**base, "id": good, "occurred_at": hours_ago(2).isoformat()},
        {**base, "id": naive, "occurred_at": "2026-01-05T08:05:00"},
    ]})
    assert response.status_code == 200, response.text
    results = {r["id"]: r for r in response.json()["results"]}
    assert results[good]["status"] == "stored"
    assert results[naive]["status"] == "rejected"
    assert "timezone" in results[naive]["reason"]


# -- import -------------------------------------------------------------------


def test_items_below_a_run_of_blank_rows_are_not_dropped(client, factory):
    """All-or-nothing has to include "all": rows far down a sheet used to fall
    past a read cap and vanish from the preview with no error."""
    import openpyxl

    factory.db.commit()
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["project", "item", "qty", "stages"])
    sheet.append(["XL", "EARLY-1", 5, "Carpentry"])
    for col, value in enumerate(["XL", "LATE-1", 5, "Carpentry"], start=1):
        sheet.cell(row=2400, column=col, value=value)
    buffer = io.BytesIO()
    book.save(buffer)

    body = client.post(
        "/api/import",
        files={"file": ("job.xlsx", buffer.getvalue(), "application/octet-stream")},
        data={"commit": "true"},
    ).json()
    assert body["errors"] == []
    assert [(i["code"], i["row"]) for i in body["items"]] == [("EARLY-1", 2), ("LATE-1", 2400)]
    factory.db.expire_all()
    assert factory.db.scalars(sa.select(Item).where(Item.code == "LATE-1")).first() is not None


def test_a_sheet_that_runs_on_for_ever_is_refused_not_truncated(client, factory, monkeypatch):
    import openpyxl

    from app.services import imports

    monkeypatch.setattr(imports, "MAX_SCANNED_ROWS", 50)
    factory.db.commit()
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["project", "item", "qty", "stages"])
    sheet.append(["XL", "EARLY-1", 5, "Carpentry"])
    sheet.cell(row=60, column=1, value="XL")
    buffer = io.BytesIO()
    book.save(buffer)

    response = client.post(
        "/api/import",
        files={"file": ("job.xlsx", buffer.getvalue(), "application/octet-stream")},
        data={"commit": "true"},
    )
    assert response.status_code == 422
    assert "row 50" in response.json()["detail"]["reason"]
    assert factory.db.scalars(sa.select(Item).where(Item.code == "EARLY-1")).first() is None


# -- archiving and coming back ------------------------------------------------


def test_a_project_archives_with_its_items_and_each_can_come_back(world, client):
    factory, _stages, item = world
    untouched = factory.item("API-NEW", 4, ["carpentry", "paint"])  # never logged against
    factory.log(item, 10, 5, at=hours_ago(2), station_code="carpentry_1")
    other = Project(code="OTHER", name="Another job")
    factory.db.add(other)
    factory.db.flush()
    bystander = Item(code="BY-1", project_id=other.id, description="x", total_qty=1,
                     sample_qty=0, drawing_revision="A")
    factory.db.add(bystander)
    factory.db.commit()

    archived = client.delete(f"/api/projects/{factory.project.id}?with_items=true")
    assert archived.json() == {"archived": True, "archived_items": 2}

    factory.db.expire_all()
    assert factory.db.get(Item, item.id).is_active is False
    assert factory.db.get(Item, untouched.id).is_active is False
    assert factory.db.get(Item, bystander.id).is_active is True  # another job's work
    # Each archive is a fact in the log, the same as archiving one at a time.
    archive_type = factory.event_type("archive").id
    assert factory.db.scalar(
        sa.select(sa.func.count()).select_from(Event).where(Event.event_type_id == archive_type)
    ) == 2
    assert client.get("/api/items").json()["items"][0]["code"] == "BY-1"

    # An item cannot come back into a project that is still archived...
    refused = client.patch(f"/api/items/{item.id}", json={"is_active": True})
    assert refused.status_code == 409
    assert "reactivate it first" in refused.json()["detail"]

    # ...and once the project is back, it returns with its history intact.
    assert client.patch(
        f"/api/projects/{factory.project.id}", json={"is_active": True}
    ).status_code == 200
    restored = client.patch(f"/api/items/{item.id}", json={"is_active": True})
    assert restored.status_code == 200
    assert restored.json()["is_active"] is True
    state = client.get(f"/api/items/{item.id}").json()["state"]
    assert [(p["stage_code"], p["qty"]) for p in state["positions"] if not p["is_unstarted"]] == [
        ("carpentry", 5)
    ]
    # Loggable again.
    assert _post_move(client, factory, item).status_code == 201


def test_archiving_is_not_a_field_edit(world, client):
    """PATCH only brings an item back. The way out is DELETE, which writes the
    archive event; a PATCH to false would skip it."""
    factory, _stages, item = world
    refused = client.patch(f"/api/items/{item.id}", json={"is_active": False})
    assert refused.status_code == 422
    factory.db.expire_all()
    assert factory.db.get(Item, item.id).is_active is True


def test_reactivating_an_item_is_admin_only(world, client, factory):
    from fastapi.testclient import TestClient

    from app.main import app

    _factory, _stages, item = world
    factory.log(item, 10, 5, at=hours_ago(2), station_code="carpentry_1")
    factory.db.commit()
    assert client.delete(f"/api/items/{item.id}").json()["archived"] is True
    created = client.post("/api/users", json={
        "username": "floor", "display_name": "Floor", "password": "a-long-password",
    })
    assert created.status_code == 201

    with TestClient(app) as floor:
        floor.post("/api/auth/login", json={"username": "floor", "password": "a-long-password"})
        assert floor.patch(f"/api/items/{item.id}", json={"is_active": True}).status_code == 403


def test_a_purge_is_written_to_the_server_log(world, client, caplog):
    """The events table cannot record its own deletion, so the server log
    does: who, which item, how many entries went with it."""
    factory, _stages, item = world
    factory.log(item, 10, 5, at=hours_ago(2), station_code="carpentry_1")
    factory.db.commit()

    with caplog.at_level(logging.WARNING, logger="app.api.items"):
        assert client.delete(f"/api/items/{item.id}?purge=true").json()["deleted"] is True

    line = next(r.getMessage() for r in caplog.records if "purged" in r.getMessage())
    assert "API-1" in line and "test" in line and "1 event(s)" in line


# -- flags that age out -------------------------------------------------------


def test_clock_flags_age_out_but_structural_ones_do_not(factory):
    item = factory.item("AGE-1", 10, ["carpentry", "paint"])
    move = factory.event_type("move").id
    station = factory.station("carpentry_1").id

    def stored(occurred, received, qty=1, step=0):
        event = Event(
            id=uuid.uuid4(), item_id=item.id, item_step_id=item.steps[step].id,
            station_id=station if step == 0 else None, event_type_id=move, qty=qty,
            occurred_at=occurred, received_at=received,
            user_id=factory.user.id, submitted_by_user_id=factory.user.id,
        )
        factory.db.add(event)
        factory.db.flush()
        return event.id

    # Both synced 20 days late. One reached the server last week, the other
    # two months ago -- old news that nothing can be done about any more.
    recent = stored(days_ago(27), days_ago(7))
    old = stored(days_ago(80), days_ago(60))
    # An over-advance from before the write-time guard, just as old: still
    # wrong today, so still listed.
    over = stored(days_ago(79), days_ago(79), qty=500)

    flags = {(a.code, a.event_id) for a in derive(factory.db, item_ids=[item.id]).anomalies}
    assert ("late_sync", recent) in flags
    assert ("late_sync", old) not in flags
    assert ("over_advance", over) in flags


# -- ops ----------------------------------------------------------------------


def test_backup_status_reads_the_newest_dump_off_storage(client, tmp_path, monkeypatch):
    monkeypatch.setenv("LSF_UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.config import get_settings
    from app.storage import get_storage

    get_settings.cache_clear()
    get_storage.cache_clear()

    empty = client.get("/api/status/backup").json()
    assert empty == {"available": True, "count": 0, "last_key": None, "last_at": None}

    storage = get_storage()
    for stamp in ("20261114-020000Z", "20261116-020000Z", "20261115-020000Z"):
        storage.put(f"backups/lsf-{stamp}.sql.gz", io.BytesIO(b"x" * 600))

    status = client.get("/api/status/backup").json()
    assert status["count"] == 3
    assert status["last_key"] == "backups/lsf-20261116-020000Z.sql.gz"
    assert status["last_at"] == "2026-11-16T02:00:00+00:00"


def test_backup_status_is_admin_only(client, factory):
    from fastapi.testclient import TestClient

    from app.main import app

    client.post("/api/users", json={
        "username": "floor", "display_name": "Floor", "password": "a-long-password",
    })
    with TestClient(app) as floor:
        floor.post("/api/auth/login", json={"username": "floor", "password": "a-long-password"})
        assert floor.get("/api/status/backup").status_code == 403


def test_api_docs_are_not_served_unless_asked_for(client):
    assert client.get("/api/docs").status_code == 404
    assert client.get("/api/openapi.json").status_code == 404
