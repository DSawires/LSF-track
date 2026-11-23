"""Parts, assembly, and batches split across phases.

A bed is split into its frame, headboard and four legs; each part goes through
the factory on its own and they come back together at assembly. Every one of
those moves is an entry in the log, grouped with the others of the same split
or assembly, stored all together or not at all. The Assembly stage is added at
runtime, as the factory would add it: nothing here needs a stage in code.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.db import utcnow
from app.models import Item
from app.services import reports

from tests.conftest import hours_ago


def _part(client, item, code, per_unit=1, description=""):
    made = client.post(f"/api/items/{item.id}/parts", json={
        "code": code, "per_unit": per_unit, "description": description,
    })
    assert made.status_code == 201, made.text
    return made.json()


def _row(factory, item_id, qty, link, at, *, out=None, to=None, inbound=False, finish=False):
    """One row of a linked group. `out` is the place units leave from (stage
    code, "unstarted" or "done"); `to` is the stage they arrive at (None: not
    started, or Done when `finish`)."""
    row = {
        "id": str(uuid.uuid4()), "item_id": str(item_id), "qty": qty,
        "event_type_id": str(factory.event_type("finish" if finish else "move").id),
        "occurred_at": at.isoformat(), "link_id": str(link),
    }
    if inbound:
        row.update(from_link=True, stage_id=str(factory.stage(to).id) if to else None)
    else:
        row.update(
            to_link=True,
            from_unstarted=out == "unstarted",
            from_done=out == "done",
            from_stage_id=str(factory.stage(out).id) if out not in ("unstarted", "done") else None,
        )
    return row


def _batch(client, rows):
    answer = client.post("/api/events/batch", json={"events": rows})
    assert answer.status_code == 200, answer.text
    return answer.json()["results"]


def _state(factory, item_id):
    return reports.item_state(factory.db, [uuid.UUID(str(item_id))])[str(item_id)]


def _at(state):
    return {p["stage_code"] or "unstarted": p["qty"] for p in state["positions"]}


@pytest.fixture()
def bed(client, factory):
    factory.add_stage("assembly", sort=65)
    bed = factory.item("BED-01", 10)
    factory.db.commit()
    frame = _part(client, bed, "BED-01-FR", 1, "Frame")
    legs = _part(client, bed, "BED-01-LG", 4, "Leg")
    return factory, bed, frame, legs


def test_a_part_is_an_item_of_its_own_filed_with_its_piece(client, bed):
    factory, bed, frame, legs = bed
    assert (frame["parent_item_id"], frame["per_unit"], legs["per_unit"]) == (str(bed.id), 1, 4)
    listed = {row["code"]: row for row in client.get("/api/items").json()["items"]}
    assert listed["BED-01-LG"]["parent_item_id"] == str(bed.id)
    # Nothing of it exists until the piece is split.
    assert listed["BED-01-LG"]["state"]["positions"] == []
    assert listed["BED-01-LG"]["total_qty"] == 0

    # The code is checked like any item's.
    again = client.post(f"/api/items/{bed.id}/parts", json={"code": "BED-01-FR"})
    assert again.status_code == 409
    assert client.post(f"/api/items/{bed.id}/parts", json={"code": "X", "per_unit": 0}).status_code == 422


def test_split_and_assemble_move_units_between_a_piece_and_its_parts(client, bed):
    factory, bed, frame, legs = bed
    factory.move(bed, "carpentry", 10, station_code="carpentry_1", at=hours_ago(10))
    factory.db.commit()

    # 6 beds at carpentry become 6 frames and 24 legs, not started.
    link = uuid.uuid4()
    at = hours_ago(8)
    results = _batch(client, [
        _row(factory, bed.id, 6, link, at, out="carpentry"),
        _row(factory, frame["id"], 6, link, at, inbound=True),
        _row(factory, legs["id"], 24, link, at, inbound=True),
    ])
    assert {r["status"] for r in results} == {"stored"}, results
    assert _at(_state(factory, bed.id)) == {"carpentry": 4}
    assert _state(factory, bed.id)["in_parts_qty"] == 6
    assert _at(_state(factory, frame["id"])) == {"unstarted": 6}
    assert _at(_state(factory, legs["id"])) == {"unstarted": 24}

    # Each part moves on its own: frames to paint, legs to paint and then done.
    factory.move(factory.db.get(Item, uuid.UUID(frame["id"])), "paint", 6, station_code="paint_1", at=hours_ago(6))
    lg = factory.db.get(Item, uuid.UUID(legs["id"]))
    factory.move(lg, "paint", 24, station_code="paint_2", at=hours_ago(6))
    factory.move(lg, None, 20, frm="paint", at=hours_ago(5))
    factory.db.commit()

    # They convene at assembly: 5 beds from 5 frames at paint and 20 finished
    # legs. Each part says where its own units come from.
    link = uuid.uuid4()
    at = hours_ago(4)
    results = _batch(client, [
        _row(factory, frame["id"], 5, link, at, out="paint"),
        _row(factory, legs["id"], 20, link, at, out="done"),
        _row(factory, bed.id, 5, link, at, inbound=True, to="assembly"),
    ])
    assert {r["status"] for r in results} == {"stored"}, results
    state = _state(factory, bed.id)
    assert _at(state) == {"carpentry": 4, "assembly": 5}
    assert state["in_parts_qty"] == 1
    assert _at(_state(factory, frame["id"])) == {"paint": 1}
    assert _at(_state(factory, legs["id"])) == {"paint": 4}
    assert _state(factory, legs["id"])["done_qty"] == 0       # used up, not finished stock

    # The assembled pieces carry on like any others.
    factory.move(bed, None, 5, frm="assembly")
    factory.db.commit()
    assert _state(factory, bed.id)["done_qty"] == 5

    # The history says what happened on each item.
    history = client.get(f"/api/items/{bed.id}/events").json()["events"]
    assert sum(1 for e in history if e["to_link"]) == 1 and sum(1 for e in history if e["from_link"]) == 1


def test_a_split_or_assembly_that_does_not_add_up_is_refused_whole(client, bed):
    factory, bed, frame, legs = bed
    at = utcnow() - timedelta(minutes=5)

    def refused(rows, word):
        results = _batch(client, rows)
        assert {r["status"] for r in results} == {"rejected"}, results
        assert all(word in r["reason"] for r in results), results

    # Wrong count of legs: 3 beds need 12.
    link = uuid.uuid4()
    refused([
        _row(factory, bed.id, 3, link, at, out="unstarted"),
        _row(factory, frame["id"], 3, link, at, inbound=True),
        _row(factory, legs["id"], 10, link, at, inbound=True),
    ], "every part")
    # A part left out.
    link = uuid.uuid4()
    refused([
        _row(factory, bed.id, 3, link, at, out="unstarted"),
        _row(factory, frame["id"], 3, link, at, inbound=True),
    ], "every part")
    # More beds than there are: the whole group goes, not just the bed's row.
    link = uuid.uuid4()
    refused([
        _row(factory, bed.id, 30, link, at, out="unstarted"),
        _row(factory, frame["id"], 30, link, at, inbound=True),
        _row(factory, legs["id"], 120, link, at, inbound=True),
    ], "cannot support")
    # Assembling parts that were never split off.
    link = uuid.uuid4()
    refused([
        _row(factory, frame["id"], 1, link, at, out="unstarted"),
        _row(factory, legs["id"], 4, link, at, out="unstarted"),
        _row(factory, bed.id, 1, link, at, inbound=True, to="assembly"),
    ], "cannot support")
    assert _state(factory, bed.id)["in_parts_qty"] == 0
    assert _at(_state(factory, frame["id"])) == {}

    # A linked row on its own is never stored, whichever way it arrives.
    lone = _row(factory, bed.id, 1, uuid.uuid4(), at, out="unstarted")
    assert client.post("/api/events", json=lone).status_code == 422

    # A good group posted twice is stored once.
    link = uuid.uuid4()
    rows = [
        _row(factory, bed.id, 2, link, at, out="unstarted"),
        _row(factory, frame["id"], 2, link, at, inbound=True),
        _row(factory, legs["id"], 8, link, at, inbound=True),
    ]
    assert {r["status"] for r in _batch(client, rows)} == {"stored"}
    assert {r["status"] for r in _batch(client, rows)} == {"duplicate"}
    assert _state(factory, bed.id)["in_parts_qty"] == 2


def test_the_sample_split_into_parts_is_still_the_sample_when_assembled(client, factory):
    factory.add_stage("assembly", sort=65)
    chair = factory.item("CH-01", 40, sample_qty=1)
    factory.db.commit()
    seat = _part(client, chair, "CH-01-ST")
    at = utcnow() - timedelta(hours=3)

    link = uuid.uuid4()
    assert {r["status"] for r in _batch(client, [
        _row(factory, chair.id, 1, link, at, out="unstarted"),
        _row(factory, seat["id"], 1, link, at, inbound=True, to="upholstery"),
    ])} == {"stored"}
    link = uuid.uuid4()
    at = at + timedelta(hours=1)
    assert {r["status"] for r in _batch(client, [
        _row(factory, seat["id"], 1, link, at, out="upholstery"),
        _row(factory, chair.id, 1, link, at, inbound=True, finish=True),
    ])} == {"stored"}

    # Assembled straight into Done: the sample is done, and the batch is out.
    state = _state(factory, chair.id)
    assert state["phase"] == "production"
    assert _at(state) == {"unstarted": 40}


def test_parts_do_not_change_shape_while_pieces_are_out_in_them(client, bed):
    factory, bed, frame, legs = bed
    at = utcnow() - timedelta(minutes=5)
    link = uuid.uuid4()
    _batch(client, [
        _row(factory, bed.id, 1, link, at, out="unstarted"),
        _row(factory, frame["id"], 1, link, at, inbound=True),
        _row(factory, legs["id"], 4, link, at, inbound=True),
    ])
    assert client.patch(f"/api/items/{legs['id']}/part", json={"per_unit": 3}).status_code == 409
    assert client.post(f"/api/items/{bed.id}/parts", json={"code": "BED-01-HB"}).status_code == 409
    assert client.delete(f"/api/items/{frame['id']}").status_code == 409
    # The name is free to change; the count is not.
    assert client.patch(f"/api/items/{legs['id']}/part", json={"description": "Turned leg"}).status_code == 200

    # A piece with parts is not deleted for good before its parts are.
    assert client.delete(f"/api/items/{bed.id}?purge=true").status_code == 409


def test_parts_follow_their_piece_when_it_is_filed(client, bed):
    factory, bed, frame, legs = bed
    from app.models import Area

    rooms = factory.label(Area, "Guest rooms")
    factory.db.commit()
    assert client.patch(f"/api/items/{bed.id}", json={"area_id": str(rooms.id)}).status_code == 200
    factory.db.expire_all()
    assert factory.db.get(Item, uuid.UUID(frame["id"])).area_id == rooms.id
    # A part is not filed on its own.
    moved = client.patch(f"/api/items/{frame['id']}", json={"area_id": None})
    assert moved.status_code == 409
    alone = client.post("/api/items/assign", json={"item_ids": [frame["id"]], "area_id": None})
    assert alone.status_code == 409

    # Archiving the piece takes its parts off the floor with it, and back.
    factory.move(bed, "carpentry", 1, station_code="carpentry_1")
    factory.db.commit()
    assert client.delete(f"/api/items/{bed.id}").json()["archived"] is True
    codes = {row["code"] for row in client.get("/api/items").json()["items"]}
    assert not codes & {"BED-01", "BED-01-FR", "BED-01-LG"}
    assert client.patch(f"/api/items/{bed.id}", json={"is_active": True}).status_code == 200
    codes = {row["code"] for row in client.get("/api/items").json()["items"]}
    assert {"BED-01", "BED-01-FR", "BED-01-LG"} <= codes


def test_reports_count_parts_where_they_are_with_an_extra_stage(client, bed):
    factory, bed, frame, legs = bed
    factory.add_stage("sanding", sort=12)
    factory.db.commit()
    at = utcnow() - timedelta(hours=2)
    link = uuid.uuid4()
    _batch(client, [
        _row(factory, bed.id, 3, link, at, out="unstarted"),
        _row(factory, frame["id"], 3, link, at, inbound=True, to="sanding"),
        _row(factory, legs["id"], 12, link, at, inbound=True, to="sanding"),
    ])
    wip = client.get("/api/reports/wip").json()
    sanding = next(row for row in wip["stages"] if row["stage"]["code"] == "sanding")
    assert sanding["total_qty"] == 15 and sanding["item_count"] == 2


# -- phases ---------------------------------------------------------------------


def test_a_planned_batch_splits_across_phases_and_waits_on_the_same_sample(client, factory):
    from app.models import Phase

    item = factory.item("LF-01", 216, sample_qty=1, phase="Phase 1")
    two = factory.label(Phase, "Phase 2")
    factory.db.commit()

    split = client.post(f"/api/items/{item.id}/split-phase", json={
        "phase_id": str(two.id), "take": {"unstarted": 66},
    })
    assert split.status_code == 200, split.text
    moved = split.json()["to"]
    assert (split.json()["from"]["total_qty"], moved["total_qty"], moved["sample_item_id"]) == (150, 66, str(item.id))
    # Phase 2 has no sample of its own and nothing on the floor until phase 1's
    # sample is done.
    state = _state(factory, moved["id"])
    assert state["waits_on_sample"] is True and state["positions"] == []

    factory.move(item, None, 1, at=hours_ago(1))
    factory.db.commit()
    state = _state(factory, moved["id"])
    assert state["waits_on_sample"] is False and _at(state) == {"unstarted": 66}
    assert _at(_state(factory, item.id)) == {"unstarted": 150}

    # Moving some back adds to the batch already there.
    back = client.post(f"/api/items/{moved['id']}/split-phase", json={
        "phase_id": str(item.phase_id), "take": {"unstarted": 6},
    }).json()
    assert back["to"]["id"] == str(item.id)


def test_a_released_batch_moves_its_units_into_another_phase_place_by_place(client, factory):
    from app.models import Phase

    item = factory.item("LF-02", 100, phase="Phase 1")
    two = factory.label(Phase, "Phase 2")
    factory.move(item, "carpentry", 40, station_code="carpentry_1", at=hours_ago(30))
    factory.db.commit()

    split = client.post(f"/api/items/{item.id}/split-phase", json={
        "phase_id": str(two.id), "take": {"unstarted": 20, str(factory.stage("carpentry").id): 15},
    })
    assert split.status_code == 200, split.text
    moved = split.json()["to"]
    original = _state(factory, item.id)
    assert _at(original) == {"unstarted": 40, "carpentry": 25}
    assert original["batch_qty"] == 65
    other = _state(factory, moved["id"])
    assert _at(other) == {"unstarted": 20, "carpentry": 15}
    assert other["batch_qty"] == 35
    # Units already at a stage keep their stage, and are not flagged for a
    # missing station: the move says nothing about stations.
    flags = client.get("/api/reports/exceptions").json()["rows"]
    assert not [f for f in flags if f["code"] == "missing_station"]

    too_many = client.post(f"/api/items/{item.id}/split-phase", json={
        "phase_id": str(two.id), "take": {str(factory.stage("carpentry").id): 26},
    })
    assert too_many.status_code == 422 and "only 25" in too_many.json()["detail"]
    same = client.post(f"/api/items/{item.id}/split-phase", json={
        "phase_id": str(item.phase_id), "take": {"unstarted": 1},
    })
    assert same.status_code == 422


def test_items_with_parts_move_between_phases_whole(client, bed):
    factory, bed, frame, legs = bed
    from app.models import Phase

    two = factory.label(Phase, "Phase 2")
    factory.db.commit()
    refused = client.post(f"/api/items/{bed.id}/split-phase", json={
        "phase_id": str(two.id), "take": {"unstarted": 1},
    })
    assert refused.status_code == 409


def test_a_new_item_can_be_split_across_phases_as_it_is_added(client, factory):
    from app.models import Phase

    one, two = factory.label(Phase, "Phase 1"), factory.label(Phase, "Phase 2")
    factory.db.commit()
    body = {"project_id": str(factory.project.id), "code": "LF-09", "total_qty": 216, "sample_qty": 1,
            "phase_quantities": [{"phase_id": str(one.id), "qty": 150}, {"phase_id": str(two.id), "qty": 66}]}
    made = client.post("/api/items", json=body)
    assert made.status_code == 201, made.text
    first, second = made.json()["phases"]
    assert (first["phase_id"], first["total_qty"], first["sample_qty"]) == (str(one.id), 150, 1)
    assert (second["phase_id"], second["total_qty"], second["sample_qty"], second["sample_item_id"]) == (
        str(two.id), 66, 0, first["id"],
    )
    # One sample for both: phase 2 waits until phase 1's sample is done.
    assert _state(factory, second["id"])["waits_on_sample"] is True
    factory.move(factory.db.get(Item, uuid.UUID(first["id"])), None, 1)
    factory.db.commit()
    assert _at(_state(factory, second["id"])) == {"unstarted": 66}

    # Nothing is made when one phase clashes, or the numbers do not add up.
    again = client.post("/api/items", json={**body, "code": "LF-10", "phase_quantities": [
        {"phase_id": str(one.id), "qty": 100}, {"phase_id": None, "qty": 116}]})
    assert again.status_code == 201
    clash = client.post("/api/items", json={**body, "code": "LF-10", "total_qty": 10, "phase_quantities": [
        {"phase_id": str(two.id), "qty": 5}, {"phase_id": str(one.id), "qty": 5}]})
    assert clash.status_code == 409
    codes = [row for row in client.get("/api/items").json()["items"] if row["code"] == "LF-10"]
    assert len(codes) == 2
    wrong_sum = client.post("/api/items", json={**body, "code": "LF-11", "total_qty": 200})
    assert wrong_sum.status_code == 422 and "add up to 216" in wrong_sum.text
    twice = client.post("/api/items", json={**body, "code": "LF-12", "total_qty": 2, "phase_quantities": [
        {"phase_id": str(one.id), "qty": 1}, {"phase_id": str(one.id), "qty": 1}]})
    assert twice.status_code == 422
