"""What the server refuses, whatever sends it.

The phone already keeps most of these from being asked for: it offers no Void
on one row of a split, no quantity box on a part. But the log is the source of
truth, and "the screen did not offer it" is not a rule the log can rely on --
an old page, a second client or a hand-written request reaches the same
endpoints. Each of these is checked where the entry is stored.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Phase, User
from app.security import hash_password
from tests.conftest import hours_ago
from tests.test_parts import _at, _batch, _part, _row, _state
from tests.test_work_orders import _issue, _line


def _engineer(factory):
    """A signed-in client for someone who is not an admin."""
    factory.db.add(User(
        username="eng", display_name="Engineer", password_hash=hash_password("pw12345678"),
        is_admin=False,
    ))
    factory.db.commit()
    client = TestClient(app)
    client.__enter__()
    assert client.post(
        "/api/auth/login", json={"username": "eng", "password": "pw12345678"}
    ).status_code == 200
    return client


def test_one_row_of_a_split_cannot_be_voided_on_its_own(client, factory):
    bed = factory.item("BED-01", 10)
    factory.db.commit()
    frame = _part(client, bed, "BED-01-FR", 1)
    factory.move(bed, "carpentry", 10, station_code="carpentry_1", at=hours_ago(10))
    factory.db.commit()
    link, at = uuid.uuid4(), hours_ago(8)
    out_row = _row(factory, bed.id, 6, link, at, out="carpentry")
    in_row = _row(factory, frame["id"], 6, link, at, inbound=True)
    assert {r["status"] for r in _batch(client, [out_row, in_row])} == {"stored"}

    for row in (out_row, in_row):
        void = client.post("/api/events", json={
            "id": str(uuid.uuid4()), "item_id": row["item_id"],
            "event_type_id": str(factory.event_type("correction").id),
            "supersedes_event_id": row["id"], "occurred_at": hours_ago(1).isoformat(),
        })
        assert void.status_code == 422, void.text
        assert "cannot be voided on its own" in void.json()["detail"]["reason"]

    # Six beds are still out as six frames: nothing came back, nothing doubled.
    assert _at(_state(factory, bed.id)) == {"carpentry": 4}
    assert _state(factory, bed.id)["in_parts_qty"] == 6
    assert _at(_state(factory, frame["id"])) == {"unstarted": 6}

    # An ordinary entry is voided as it always was.
    plain = factory.move(bed, "paint", 4, frm="carpentry", station_code="paint_1", at=hours_ago(6))
    factory.db.commit()
    assert client.post("/api/events", json={
        "id": str(uuid.uuid4()), "item_id": str(bed.id),
        "event_type_id": str(factory.event_type("correction").id),
        "supersedes_event_id": str(plain.id), "occurred_at": hours_ago(1).isoformat(),
    }).status_code == 201


def test_a_part_cannot_be_given_a_quantity_of_its_own(client, factory):
    bed = factory.item("BED-02", 10)
    factory.db.commit()
    frame = _part(client, bed, "BED-02-FR", 1)

    refused = client.patch(f"/api/items/{frame['id']}", json={"total_qty": 7})
    assert refused.status_code == 409, refused.text
    assert "no quantity of its own" in refused.json()["detail"]
    assert _at(_state(factory, frame["id"])) == {}

    # Its other facts are still editable, and so is the piece's own quantity.
    assert client.patch(f"/api/items/{frame['id']}", json={"description": "Frame"}).status_code == 200
    assert client.patch(f"/api/items/{bed.id}", json={"total_qty": 12}).status_code == 200


def test_moving_units_between_phases_is_an_admins_and_keeps_them_where_they_are(client, factory):
    a = factory.item("TR-1", 20, phase="Phase 1")
    b = factory.item("TR-1", 5, phase="Phase 2")
    factory.move(a, "paint", 8, station_code="paint_1", at=hours_ago(5))
    factory.db.commit()
    engineer = _engineer(factory)

    # The office endpoint is admin-only, and the log does not offer a way round it.
    link, at = uuid.uuid4(), hours_ago(1)
    results = _batch(engineer, [
        _row(factory, a.id, 3, link, at, out="unstarted"),
        _row(factory, b.id, 3, link, at, inbound=True),
    ])
    assert {r["status"] for r in results} == {"rejected"}, results
    assert all("by an admin" in r["reason"] for r in results)

    # For an admin, a hand-posted pair must still leave the units where they
    # are: not started stays not started, paint stays paint.
    for bad_arrival in (
        dict(inbound=True, to="paint"),            # not started -> paint
        dict(inbound=True, finish=True),           # not started -> Done
    ):
        link = uuid.uuid4()
        results = _batch(client, [
            _row(factory, a.id, 3, link, at, out="unstarted"),
            _row(factory, b.id, 3, link, at, **bad_arrival),
        ])
        assert {r["status"] for r in results} == {"rejected"}, results
        assert all("stay at the place they are at" in r["reason"] for r in results)
    assert (_state(factory, a.id)["batch_qty"], _state(factory, b.id)["batch_qty"]) == (20, 5)

    # The real thing, both ways it is done, still works.
    moved = client.post(f"/api/items/{a.id}/split-phase", json={
        "phase_id": str(b.phase_id), "take": {"unstarted": 3, str(factory.stage("paint").id): 2},
    })
    assert moved.status_code == 200, moved.text
    assert _at(_state(factory, a.id)) == {"unstarted": 9, "paint": 6}
    assert _at(_state(factory, b.id)) == {"unstarted": 8, "paint": 2}


def test_a_batch_others_wait_on_is_not_deleted_from_under_them(client, factory):
    one, two = factory.label(Phase, "Phase 1"), factory.label(Phase, "Phase 2")
    factory.db.commit()
    made = client.post("/api/items", json={
        "code": "KB-1", "project_id": str(factory.project.id), "total_qty": 100, "sample_qty": 1,
        "phase_quantities": [{"phase_id": str(one.id), "qty": 60}, {"phase_id": str(two.id), "qty": 40}],
    })
    assert made.status_code == 201, made.text
    first, second = (row["id"] for row in made.json()["phases"])

    # Nothing has been logged yet, so a plain DELETE would remove the row --
    # and the second phase's batch is released by this one's sample.
    refused = client.delete(f"/api/items/{first}")
    assert refused.status_code == 409, refused.text
    assert "waits on this item's sample" in refused.json()["detail"]

    # The waiting batch goes first; then this one can.
    assert client.delete(f"/api/items/{second}").json()["deleted"] is True
    assert client.delete(f"/api/items/{first}").json()["deleted"] is True


@pytest.mark.parametrize("code", ["archive", "revision_bump", "release"])
def test_entries_the_server_writes_cannot_be_posted(client, factory, code):
    item = factory.item("SRV-1", 10)
    factory.db.commit()
    posted = client.post("/api/events", json={
        "id": str(uuid.uuid4()), "item_id": str(item.id),
        "event_type_id": str(factory.event_type(code).id),
        "occurred_at": hours_ago(1).isoformat(), "note": "item archived",
    })
    assert posted.status_code == 422, posted.text
    assert "written by the server" in posted.json()["detail"]["reason"]
    assert _state(factory, item.id)["event_count"] == 0


def test_the_server_still_writes_its_own_entries(client, factory):
    """The refusal above is on what comes in, not on what the server records."""
    item = factory.item("SRV-2", 10)
    factory.move(item, "carpentry", 4, station_code="carpentry_1", at=hours_ago(3))
    factory.db.commit()
    assert client.post(f"/api/items/{item.id}/revision", json={"drawing_revision": "B"}).status_code == 200
    assert client.delete(f"/api/items/{item.id}").json() == {"archived": True, "deleted": False}
    kinds = {e["event_type_id"] for e in client.get(f"/api/items/{item.id}/events").json()["events"]}
    assert {str(factory.event_type("revision_bump").id), str(factory.event_type("archive").id)} <= kinds


def test_a_line_as_long_as_the_form_allows_still_prints(client, factory):
    """2,000 characters in each language is what a line may hold; a row that
    tall does not fit on one page, and used to make the PDF fail every time."""
    item = factory.item("TALL-1", 5)
    factory.db.commit()
    english = "Solid oak frame with mortise and tenon joints throughout. " * 34   # ~1,970
    arabic = "إطار من خشب البلوط المصمت مع وصلات نقر ولسان. " * 43               # ~1,980
    for language, fields in (
        ("en", {"description": english}),
        ("both", {"description": english, "description_ar": arabic}),
        ("en", {"description": "one\n" * 120}),
    ):
        made = _issue(client, factory, [_line(item, **fields)], language=language)
        assert made.status_code == 201, made.text
        pdf = client.get(f"/api/work-orders/{made.json()['id']}/pdf")
        assert pdf.status_code == 200, pdf.text[:200]
        assert pdf.content.startswith(b"%PDF")
