"""Units move freely: from any place to any stage, or to Done, in any order.

An item has no stages of its own and no route. An entry says where the units
are now and where they came from, and the log is the only record of the path a
batch actually took. Every test here uses a stage added at runtime alongside
the seeded ones, so nothing about movement can come to depend on which stages
happen to exist.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.models import Event, ReasonCode
from app.schemas import EventCreate
from app.services import reports
from app.services.derivation import derive
from app.services.events import EventRejected, record_event

from tests.conftest import days_ago, hours_ago


def _where(db, item) -> dict[str, int]:
    """{place: qty} for every place holding units, in report order."""
    derivation = derive(db, item_ids=[item.id])
    ledger = derivation.ledger_for(item.id)
    out = {}
    for bucket in ledger.occupied(include_finished=True):
        position = bucket.position
        key = "done" if position.is_done else (
            "unstarted" if position.is_unstarted else derivation.vocab.stages[position.stage_id].code
        )
        out[key] = bucket.qty
    return out


@pytest.fixture()
def shop(factory):
    """A factory with one stage added at runtime, and a 20-piece item."""
    factory.add_stage("gilding", sort=35)
    return factory, factory.item("FREE-1", 20)


def _reason(factory):
    return factory.db.scalars(sa.select(ReasonCode).limit(1)).first()


# -- any stage, any order -----------------------------------------------------


def test_units_go_from_anywhere_to_anywhere(shop):
    factory, item = shop
    assert _where(factory.db, item) == {"unstarted": 20}

    # Straight to paint, never having been near carpentry.
    factory.move(item, "paint", 20, at=days_ago(6), station_code="paint_1")
    # Back to carpentry -- an ordinary move, no rework needed to go "backwards".
    factory.move(item, "carpentry", 8, frm="paint", at=days_ago(5), station_code="carpentry_1")
    # Out to the stage that did not exist when the item was created.
    factory.move(item, "gilding", 5, frm="carpentry", at=days_ago(4))
    # And to Done from wherever they are; nothing has to be "last".
    factory.move(item, None, 5, frm="gilding", at=days_ago(3))
    factory.move(item, None, 2, frm="paint", at=days_ago(2))

    # Stages come back in the factory's own order, not the order visited.
    assert _where(factory.db, item) == {"carpentry": 3, "paint": 10, "done": 7}
    assert list(_where(factory.db, item)) == ["carpentry", "paint", "done"]
    assert derive(factory.db, item_ids=[item.id]).anomalies == []


def test_the_movement_itself_is_kept_on_every_row(shop):
    """Kept for later use: from, to, how many, when, who -- on the entry, so
    the route a batch really took can be read back out of the log."""
    factory, item = shop
    factory.move(item, "paint", 20, at=days_ago(4), station_code="paint_1")
    factory.move(item, "gilding", 6, frm="paint", at=days_ago(3))
    factory.move(item, None, 6, frm="gilding", at=days_ago(2))
    factory.move(item, "paint", 2, frm="done", at=days_ago(1), event_type="rework_return",
                 reason_code_id=_reason(factory).id, station_code="paint_1")

    code = {s.id: s.code for s in derive(factory.db).vocab.stages.values()}
    rows = factory.db.scalars(
        sa.select(Event).where(Event.item_id == item.id).order_by(Event.occurred_at)
    )
    path = [
        (
            "unstarted" if e.from_unstarted else "done" if e.from_done else code[e.from_stage_id],
            code[e.stage_id] if e.stage_id else "done",
            e.qty,
        )
        for e in rows
    ]
    assert path == [
        ("unstarted", "paint", 20),
        ("paint", "gilding", 6),
        ("gilding", "done", 6),
        ("done", "paint", 2),
    ]
    assert all(e.user_id == factory.user.id for e in factory.db.scalars(sa.select(Event)))


# -- the arithmetic -----------------------------------------------------------


def test_moving_more_than_rests_at_the_source_is_a_validation_error(shop):
    """Rule 4. The count is about the place named: 14 elsewhere do not make up
    for paint holding only 6."""
    factory, item = shop
    factory.move(item, "paint", 6, at=days_ago(3), station_code="paint_1")
    with pytest.raises(EventRejected) as refused:
        factory.move(item, "gilding", 9, frm="paint", at=days_ago(2))
    assert "only 6" in refused.value.reason
    assert _where(factory.db, item) == {"unstarted": 14, "paint": 6}


def test_an_entry_must_say_where_its_units_come_from(shop):
    factory, item = shop

    def post(**fields):
        return record_event(
            factory.db,
            EventCreate(
                id=uuid.uuid4(), item_id=item.id, qty=1, occurred_at=hours_ago(1),
                event_type_id=factory.event_type(fields.pop("type", "move")).id, **fields,
            ),
            factory.user,
        )

    gilding, paint = factory.stage("gilding").id, factory.stage("paint").id
    with pytest.raises(EventRejected) as refused:
        post(stage_id=gilding)
    assert refused.value.field == "from_stage_id"

    # One place only.
    with pytest.raises(EventRejected):
        post(stage_id=gilding, from_stage_id=paint, from_unstarted=True)
    # A finish with no source used to mean "the nearest units in the item's
    # sequence". This item has none, so it means nothing.
    with pytest.raises(EventRejected):
        post(type="finish")
    # No stage at all is not a move.
    with pytest.raises(EventRejected):
        post(from_unstarted=True)
    # Done is not a stage: a finish names none.
    with pytest.raises(EventRejected):
        post(type="finish", stage_id=gilding, from_unstarted=True)
    assert _where(factory.db, item) == {"unstarted": 20}


def test_a_move_that_outruns_the_entry_it_depends_on_waits_for_it(shop):
    """Entries sync out of order. A move out of paint that reaches the server
    before the move into paint is refused -- the phone quarantines it with a
    Retry -- and goes in untouched once its partner has landed. In one batch
    the server orders them itself, so a single phone's chain never trips."""
    factory, item = shop
    with pytest.raises(EventRejected):
        factory.move(item, "gilding", 5, frm="paint", at=hours_ago(2))

    factory.move(item, "paint", 5, at=hours_ago(3), station_code="paint_1")
    factory.move(item, "gilding", 5, frm="paint", at=hours_ago(2))  # the retry
    assert _where(factory.db, item) == {"unstarted": 15, "gilding": 5}


def test_one_phones_offline_chain_validates_in_a_single_batch(world, client):
    factory, _stages, _legacy = world
    factory.add_stage("gilding", sort=35)
    item = factory.item("CHAIN-1", 10)
    factory.db.commit()
    move, finish = factory.event_type("move").id, factory.event_type("finish").id
    paint, gilding = factory.stage("paint").id, factory.stage("gilding").id

    def entry(minutes, **fields):
        return {"id": str(uuid.uuid4()), "item_id": str(item.id), "qty": 10,
                "occurred_at": hours_ago(minutes / 60).isoformat(), **fields}

    # Sent newest first, the way an unordered queue might.
    batch = [
        entry(10, event_type_id=str(finish), from_stage_id=str(gilding)),
        entry(20, event_type_id=str(move), stage_id=str(gilding), from_stage_id=str(paint)),
        entry(30, event_type_id=str(move), stage_id=str(paint), from_unstarted=True,
              station_id=str(factory.station("paint_1").id)),
    ]
    results = client.post("/api/events/batch", json={"events": batch}).json()["results"]
    assert [r["status"] for r in results] == ["stored", "stored", "stored"]
    state = client.get(f"/api/items/{item.id}").json()["state"]
    assert (state["done_qty"], state["positions"]) == (10, [])


def test_voiding_a_move_puts_the_units_back_where_they_were(shop):
    factory, item = shop
    factory.move(item, "paint", 20, at=days_ago(3), station_code="paint_1")
    wrong = factory.move(item, "gilding", 12, frm="paint", at=days_ago(2))
    record_event(
        factory.db,
        EventCreate(
            id=uuid.uuid4(), item_id=item.id, qty=0, occurred_at=days_ago(1),
            event_type_id=factory.event_type("correction").id, supersedes_event_id=wrong.id,
        ),
        factory.user,
    )
    assert _where(factory.db, item) == {"paint": 20}


# -- rework, Done, stations ---------------------------------------------------


def test_rework_is_a_label_on_a_move_not_a_direction(shop):
    factory, item = shop
    factory.move(item, "paint", 20, at=days_ago(4), station_code="paint_1")
    factory.move(item, "qc", 20, frm="paint", at=days_ago(3))

    # QC sends 5 back to paint as rework: counted, with a reason.
    factory.move(item, "paint", 5, frm="qc", at=days_ago(2), event_type="rework_return",
                 reason_code_id=_reason(factory).id, station_code="paint_1")
    # And 3 more go back as a plain move -- allowed, just not counted as rework.
    factory.move(item, "paint", 3, frm="qc", at=days_ago(1), station_code="paint_1")

    ledger = derive(factory.db, item_ids=[item.id]).ledger_for(item.id)
    paint = next(b for b in ledger.occupied() if b.position.stage_id == factory.stage("paint").id)
    assert (paint.qty, paint.reworked_qty) == (8, 5)

    # Rework still needs its reason.
    with pytest.raises(EventRejected) as refused:
        factory.move(item, "paint", 1, frm="qc", at=hours_ago(1), event_type="rework_return",
                     station_code="paint_1")
    assert refused.value.field == "reason_code_id"


def test_only_rework_takes_units_back_out_of_done(shop):
    factory, item = shop
    factory.move(item, "gilding", 20, at=days_ago(3))
    factory.move(item, None, 20, frm="gilding", at=days_ago(2))
    assert derive(factory.db, item_ids=[item.id]).ledger_for(item.id).completed_qty == 20

    with pytest.raises(EventRejected) as refused:
        factory.move(item, "gilding", 4, frm="done", at=days_ago(1))
    assert refused.value.field == "from_done"

    factory.move(item, "gilding", 4, frm="done", at=days_ago(1), event_type="rework_return",
                 reason_code_id=_reason(factory).id)
    ledger = derive(factory.db, item_ids=[item.id]).ledger_for(item.id)
    assert ledger.completed_qty == 16  # a returned piece is not counted complete twice
    assert _where(factory.db, item) == {"gilding": 4, "done": 16}


def test_a_change_of_station_is_not_a_fresh_arrival(shop):
    """Paint 1 to Paint 2 is a move within one stage: the units have been at
    paint all along, so their age keeps counting."""
    factory, item = shop
    arrived = days_ago(5)
    factory.move(item, "paint", 20, at=arrived, station_code="paint_1")
    factory.move(item, "paint", 8, frm="paint", at=days_ago(1), station_code="paint_2")

    ledger = derive(factory.db, item_ids=[item.id]).ledger_for(item.id)
    (bucket,) = ledger.occupied()
    assert bucket.qty == 20
    assert bucket.oldest_arrived_at == arrived
    stations = {factory.station("paint_1").id: 12, factory.station("paint_2").id: 8}
    assert bucket.by_station == stations

    # Without a station to change, "paint to paint" says nothing.
    with pytest.raises(EventRejected):
        factory.move(item, "gilding", 20, at=hours_ago(3))  # all 20 are at paint
    factory.move(item, "gilding", 5, frm="paint", at=hours_ago(2))
    with pytest.raises(EventRejected) as refused:
        factory.move(item, "gilding", 5, frm="gilding", at=hours_ago(1))
    assert "already at that stage" in refused.value.reason


def test_a_whole_lot_stage_takes_everything_from_where_it_draws(shop):
    """`allows_partial_qty` false, without a sequence to define "upstream": a
    move into the stage may not split the lot it comes from."""
    factory, item = shop
    kiln = factory.add_stage("kiln", sort=37, allows_partial_qty=False)
    factory.move(item, "gilding", 12, at=days_ago(3))

    with pytest.raises(EventRejected) as refused:
        factory.move(item, kiln.code, 5, frm="gilding", at=days_ago(2))
    assert "leave 7 behind" in refused.value.reason

    factory.move(item, kiln.code, 12, frm="gilding", at=days_ago(2))
    assert _where(factory.db, item) == {"unstarted": 8, "kiln": 12}


# -- the sample ---------------------------------------------------------------


def test_the_sample_takes_whatever_path_it_likes_and_releases_the_batch_at_done(factory):
    factory.add_stage("gilding", sort=35)
    item = factory.item("SMP-1", 30, sample_qty=1)
    assert _where(factory.db, item) == {"unstarted": 1}

    # Only the sample exists: the batch cannot be touched yet.
    with pytest.raises(EventRejected):
        factory.move(item, "carpentry", 5, at=days_ago(4), station_code="carpentry_1")

    factory.move(item, "gilding", 1, at=days_ago(4))
    factory.move(item, "carpentry", 1, frm="gilding", at=days_ago(3), station_code="carpentry_1")
    finished = days_ago(2)
    done = factory.move(item, None, 1, frm="carpentry", at=finished)

    ledger = derive(factory.db, item_ids=[item.id]).ledger_for(item.id)
    assert (ledger.phase, ledger.released_at, ledger.completed_qty) == ("production", finished, 0)
    assert _where(factory.db, item) == {"unstarted": 30, "done": 1}

    # Void the finish and the batch is off the floor again.
    record_event(
        factory.db,
        EventCreate(
            id=uuid.uuid4(), item_id=item.id, qty=0, occurred_at=days_ago(1),
            event_type_id=factory.event_type("correction").id, supersedes_event_id=done.id,
        ),
        factory.user,
    )
    assert _where(factory.db, item) == {"carpentry": 1}


# -- items that still have a sequence in their history ------------------------


def test_an_item_logged_under_a_sequence_carries_on_without_one(factory):
    """What every item on the floor today looks like: steps, and entries that
    named them. Its units are found where those entries left them, the old
    order stops mattering, and each stage is one row however it got there."""
    factory.add_stage("gilding", sort=35)
    item = factory.item("OLD-1", 20, ["carpentry", "paint", "packing"])
    factory.log(item, 10, 20, at=days_ago(6), station_code="carpentry_1")
    factory.log(item, 20, 12, at=days_ago(5), station_code="paint_1")
    assert _where(factory.db, item) == {"carpentry": 8, "paint": 12}

    # "Backwards" past the old order, sideways to a stage it never listed, and
    # Done straight from paint without visiting packing.
    factory.move(item, "carpentry", 2, frm="paint", at=days_ago(4), station_code="carpentry_1")
    factory.move(item, "gilding", 6, frm="carpentry", at=days_ago(3))
    factory.move(item, None, 10, frm="paint", at=days_ago(2))

    assert _where(factory.db, item) == {"carpentry": 4, "gilding": 6, "done": 10}
    state = reports.item_state(factory.db, [item.id])[str(item.id)]
    assert [p["stage_code"] for p in state["positions"]] == ["carpentry", "gilding"]
    assert derive(factory.db, item_ids=[item.id]).anomalies == []


# -- reports and the API ------------------------------------------------------


def test_reports_follow_free_movement_through_a_runtime_stage(shop):
    factory, item = shop
    other = factory.item("FREE-2", 10)
    factory.move(item, "gilding", 12, at=days_ago(6))
    factory.move(other, "paint", 10, at=days_ago(4), station_code="paint_2")
    factory.move(other, "gilding", 4, frm="paint", at=days_ago(1))

    wip = reports.wip_report(factory.db)
    rows = {r["stage"]["code"]: r for r in wip["stages"]}
    assert (rows["gilding"]["total_qty"], rows["gilding"]["item_count"]) == (16, 2)
    assert rows["paint"]["total_qty"] == 6
    assert wip["unstarted"] == {"qty": 8, "item_count": 1}

    aging = reports.aging_report(factory.db, stage_id=factory.stage("gilding").id)
    assert [(r["item"]["code"], r["qty"], round(r["days_in_state"])) for r in aging["rows"]] == [
        ("FREE-1", 12, 6), ("FREE-2", 4, 1),
    ]


def test_recent_entries_name_from_and_to_whichever_shape_the_row_has(world, client):
    factory, _stages, legacy = world  # a sequence-era item: carpentry, paint, packing
    factory.log(legacy, 10, 6, at=hours_ago(5), station_code="carpentry_1")
    factory.move(legacy, "qc", 4, frm="carpentry", at=hours_ago(4))
    factory.move(legacy, None, 1, frm="qc", at=hours_ago(3))
    factory.db.commit()

    events = client.get(f"/api/items/{legacy.id}/events").json()["events"]
    code = {s["id"]: s["code"] for s in client.get("/api/reference").json()["stages"]}
    seen = [
        (
            "unstarted" if e["from_unstarted"] else code.get(e["from_stage_id"]),
            code.get(e["to_stage_id"]),
            e["qty"],
        )
        for e in reversed(events)
    ]
    assert seen == [
        (None, "carpentry", 6),   # sequence-era row: a step resolved to its stage, no source
        ("carpentry", "qc", 4),
        ("qc", None, 1),          # a finish: to Done
    ]
