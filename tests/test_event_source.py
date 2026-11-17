"""An entry can say where its units came from.

Rework is the case that needs it. "QC sent 5 back to carpentry" used to take
the 5 from whichever stage was nearest carpentry -- so with a lot resting at
paint, paint lost the units and QC kept them. Totals stayed right; "where is
every item right now" did not.

Every test here runs on a sequence that includes a stage added at runtime, so
nothing about sources can come to depend on which stages exist.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.models import Event, ReasonCode
from app.services.derivation import derive
from app.services.events import EventRejected
from app.services.items import steps_are_frozen

from tests.conftest import days_ago, hours_ago


def _where(db, item) -> dict[str, tuple[int, int]]:
    """{stage code: (qty, reworked qty)} for every place holding units."""
    derivation = derive(db, item_ids=[item.id])
    ledger = derivation.ledger_for(item.id)
    stages = derivation.vocab.stages
    out = {}
    for bucket in ledger.occupied(include_finished=True):
        position = bucket.position
        if position.is_done:
            key = "done"
        elif position.is_unstarted:
            key = "unstarted"
        else:
            key = stages[position.stage_id].code
        out[key] = (bucket.qty, bucket.reworked_qty)
    return out


@pytest.fixture()
def split(factory):
    """20 pcs: 10 resting at paint, 10 at a runtime-added inspection stage."""
    factory.add_stage("inspection", sort=65)
    item = factory.item("SRC-1", 20, ["carpentry", "paint", "inspection", "packing"])
    factory.log(item, 10, 20, at=days_ago(5), station_code="carpentry_1")
    factory.log(item, 20, 20, at=days_ago(4), station_code="paint_1")
    factory.log(item, 30, 10, at=days_ago(3))
    reason = factory.db.scalars(sa.select(ReasonCode).limit(1)).first()
    return factory, item, reason


def test_rework_takes_its_units_from_the_stage_it_names(split):
    factory, item, reason = split
    assert _where(factory.db, item) == {"paint": (10, 0), "inspection": (10, 0)}

    # Inspection rejects 5 of ITS 10 and sends them back to carpentry.
    factory.log(
        item, 10, 5, at=days_ago(2), event_type="rework_return",
        reason_code_id=reason.id, station_code="carpentry_1", from_seq=30,
    )

    assert _where(factory.db, item) == {
        "carpentry": (5, 5),
        "paint": (10, 0),       # untouched: it did not send anything back
        "inspection": (5, 0),
    }


def test_rework_without_a_source_still_takes_the_nearest(split):
    """Rows logged before the column existed carry no source and must replay
    exactly as they always did."""
    factory, item, reason = split
    factory.log(
        item, 10, 5, at=days_ago(2), event_type="rework_return",
        reason_code_id=reason.id, station_code="carpentry_1",
    )
    assert _where(factory.db, item) == {
        "carpentry": (5, 5),
        "paint": (5, 0),
        "inspection": (10, 0),
    }


def test_rework_can_come_back_out_of_done_past_units_still_in_flight(split):
    factory, item, reason = split
    factory.log(item, 40, 10, at=days_ago(2.5))            # inspection -> packing
    factory.log(item, None, 10, at=days_ago(2))            # packing -> Done
    assert _where(factory.db, item) == {"paint": (10, 0), "done": (10, 0)}

    # A finished piece comes back; the lot at paint is not what was returned.
    factory.log(
        item, 10, 3, at=days_ago(1), event_type="rework_return",
        reason_code_id=reason.id, station_code="carpentry_1", from_done=True,
    )

    assert _where(factory.db, item) == {
        "carpentry": (3, 3),
        "paint": (10, 0),
        "done": (7, 0),
    }
    assert derive(factory.db, item_ids=[item.id]).ledger_for(item.id).completed_qty == 7


def test_a_named_source_that_holds_too_few_is_a_validation_error(split):
    """Rule 4 applies to the place named, not to everything downstream: 10 at
    paint do not make up for inspection being 5 short."""
    factory, item, reason = split
    with pytest.raises(EventRejected) as refused:
        factory.log(
            item, 10, 15, at=days_ago(2), event_type="rework_return",
            reason_code_id=reason.id, station_code="carpentry_1", from_seq=30,
        )
    assert "only 10" in refused.value.reason
    assert _where(factory.db, item) == {"paint": (10, 0), "inspection": (10, 0)}


def test_a_source_must_lie_on_the_right_side_of_the_target(split):
    factory, item, reason = split
    # Rework to paint cannot take units from carpentry: that is upstream of it.
    with pytest.raises(EventRejected) as refused:
        factory.log(
            item, 20, 2, at=days_ago(2), event_type="rework_return",
            reason_code_id=reason.id, station_code="paint_1", from_seq=10,
        )
    assert refused.value.field == "from_item_step_id"

    # And an ordinary move cannot take units from further along.
    with pytest.raises(EventRejected):
        factory.log(item, 20, 2, at=days_ago(2), station_code="paint_1", from_seq=30)

    # Only rework reaches back into Done.
    with pytest.raises(EventRejected) as refused:
        factory.log(item, 30, 2, at=days_ago(2), from_done=True)
    assert refused.value.field == "from_done"


def test_a_source_step_must_belong_to_the_item(split):
    factory, item, reason = split
    other = factory.item("SRC-OTHER", 5, ["carpentry", "paint"])
    payload_step = other.steps[1]

    from app.schemas import EventCreate
    from app.services.events import record_event

    with pytest.raises(EventRejected) as refused:
        record_event(
            factory.db,
            EventCreate(
                id=uuid.uuid4(),
                item_id=item.id,
                item_step_id=item.steps[0].id,
                station_id=factory.station("carpentry_1").id,
                event_type_id=factory.event_type("rework_return").id,
                reason_code_id=reason.id,
                qty=1,
                from_item_step_id=payload_step.id,
                occurred_at=hours_ago(1),
            ),
            factory.user,
        )
    assert "different item" in refused.value.reason


def test_a_move_may_name_its_source_too(split):
    """Not only rework: 4 of the 10 at paint go on to inspection, and the entry
    says so. The source is honoured, and it freezes the sequence like any
    other reference to a step."""
    factory, item, _reason = split
    factory.log(item, 30, 4, at=days_ago(2), from_seq=20)
    assert _where(factory.db, item) == {"paint": (6, 0), "inspection": (14, 0)}
    assert steps_are_frozen(factory.db, item)


def test_the_source_travels_in_the_api_and_counts_as_divergence(world, client):
    factory, _stages, item = world
    factory.log(item, 10, 10, at=hours_ago(5), station_code="carpentry_1")
    factory.log(item, 20, 10, at=hours_ago(4), station_code="paint_1")
    factory.db.commit()
    reason = factory.db.scalars(sa.select(ReasonCode).limit(1)).first()

    body = {
        "id": str(uuid.uuid4()),
        "item_id": str(item.id),
        "item_step_id": str(item.steps[0].id),
        "station_id": str(factory.station("carpentry_1").id),
        "event_type_id": str(factory.event_type("rework_return").id),
        "reason_code_id": str(reason.id),
        "qty": 2,
        "from_item_step_id": str(item.steps[1].id),
        "occurred_at": hours_ago(1).isoformat(),
    }
    stored = client.post("/api/events", json=body)
    assert stored.status_code == 201, stored.text
    assert stored.json()["event"]["from_item_step_id"] == str(item.steps[1].id)
    assert stored.json()["event"]["from_done"] is False

    # Same id, different source: the stored row wins and the client is told.
    replay = client.post("/api/events", json={**body, "from_item_step_id": None})
    assert replay.json()["divergent"] is True

    factory.db.expire_all()
    row = factory.db.get(Event, uuid.UUID(body["id"]))
    assert row.from_item_step_id == item.steps[1].id
