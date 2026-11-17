"""The two v1 reports, plus the exceptions list.

Both reports iterate over whatever is in `stages`. Neither knows the name of a
single stage. Inserting a stage at runtime changes their output and
nothing else -- there is a test that does exactly that.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.ledger import Bucket
from app.models import Item
from app.services.derivation import Derivation, derive, reference_maps

UNSTARTED_LABEL = "not started"


def _stage_payload(stage) -> dict:
    return {
        "id": str(stage.id),
        "code": stage.code,
        "name": stage.name,
        "sort_order": stage.sort_order,
        "requires_station": stage.requires_station,
        "requires_external_po": stage.requires_external_po,
        "allows_partial_qty": stage.allows_partial_qty,
        "is_terminal": stage.is_terminal,
        "max_days_in_state": stage.max_days_in_state,
    }


def _days_since(moment: datetime, now: datetime) -> float:
    return round((now - moment).total_seconds() / 86400.0, 2)


def _overdue(stage, oldest_arrived_at: datetime, now: datetime) -> bool:
    if stage is None or stage.max_days_in_state is None:
        return False
    return _days_since(oldest_arrived_at, now) >= stage.max_days_in_state


def _bucket_rows(
    derivation: Derivation,
    include_unstarted: bool,
) -> Iterator[tuple]:
    for item_id, ledger in derivation.ledgers.items():
        item = derivation.items[item_id]
        for bucket in ledger.occupied(include_unstarted=include_unstarted):
            yield item, bucket


def _derive_scope(db: Session, project_id: uuid.UUID | None) -> Derivation:
    """Filter BEFORE deriving: replaying every project's events to answer a
    one-project question does not scale with the log."""
    if project_id is None:
        return derive(db)
    item_ids = list(db.scalars(sa.select(Item.id).where(Item.project_id == project_id)))
    return derive(db, item_ids=item_ids)


def wip_report(
    db: Session, now: datetime | None = None, project_id: uuid.UUID | None = None
) -> dict:
    """Work in progress by stage and station.

    A unit is at a stage or it is not, so a stage's depth is simply what rests
    there. Units with no station yet are the ones waiting for a free booth.
    Sample units are counted in, and also shown separately, because a sample
    holding up a whole batch is worth seeing on its own.
    """
    now = now or utcnow()
    derivation = _derive_scope(db, project_id)
    refs = reference_maps(db)

    stage_rows: dict[uuid.UUID, dict] = {}
    unstarted_qty = 0
    unstarted_items = 0

    for item, bucket in _bucket_rows(derivation, include_unstarted=True):
        position = bucket.position
        if position.is_unstarted:
            unstarted_qty += bucket.qty
            unstarted_items += 1
            continue

        stage = refs["stages"].get(position.stage_id)
        if stage is None:
            continue
        row = stage_rows.setdefault(
            stage.id,
            {
                "stage": _stage_payload(stage),
                "total_qty": 0,
                "sample_qty": 0,
                "reworked_qty": 0,
                "item_ids": set(),
                "oldest_arrived_at": bucket.oldest_arrived_at,
                "stations": {},
            },
        )
        row["total_qty"] += bucket.qty
        row["sample_qty"] += bucket.sample_qty
        row["reworked_qty"] += bucket.reworked_qty
        row["item_ids"].add(item.id)
        if bucket.oldest_arrived_at < row["oldest_arrived_at"]:
            row["oldest_arrived_at"] = bucket.oldest_arrived_at

        for station_id, qty in bucket.by_station.items():
            station = refs["stations"].get(station_id) if station_id else None
            key = str(station.id) if station else "unassigned"
            entry = row["stations"].setdefault(
                key,
                {
                    "station": (
                        {"id": str(station.id), "code": station.code, "name": station.name}
                        if station
                        else None
                    ),
                    "sort_order": station.sort_order if station else 9999,
                    "total_qty": 0,
                },
            )
            entry["total_qty"] += qty

    stages_out = []
    for row in sorted(
        stage_rows.values(), key=lambda r: (r["stage"]["sort_order"], r["stage"]["code"])
    ):
        stations = sorted(row["stations"].values(), key=lambda s: (s["sort_order"], s["total_qty"]))
        stages_out.append(
            {
                "stage": row["stage"],
                "total_qty": row["total_qty"],
                "sample_qty": row["sample_qty"],
                "reworked_qty": row["reworked_qty"],
                "item_count": len(row["item_ids"]),
                "oldest_days": _days_since(row["oldest_arrived_at"], now),
                "stations": [
                    {k: v for k, v in station.items() if k != "sort_order"}
                    for station in stations
                ],
            }
        )

    return {
        "generated_at": now.isoformat(),
        "stages": stages_out,
        "unstarted": {"qty": unstarted_qty, "item_count": unstarted_items},
        "totals": {
            "qty_in_progress": sum(row["total_qty"] for row in stages_out),
            "items": len(derivation.ledgers),
        },
    }


def aging_report(
    db: Session,
    now: datetime | None = None,
    limit: int | None = None,
    project_id: uuid.UUID | None = None,
    stage_id: uuid.UUID | None = None,
    min_days: float | None = None,
) -> dict:
    """Days at the current stage, longest first.

    One row per item *and stage*, because a batch splits: 40 of the 120 can be in
    paint while 50 wait at QC, and those two facts have different ages. Age is taken
    from the oldest unit still resting there, FIFO, so a partial advance does not
    reset the clock on the units left behind.
    """
    now = now or utcnow()
    derivation = _derive_scope(db, project_id)
    refs = reference_maps(db)
    rows: list[dict] = []

    for item, bucket in _bucket_rows(derivation, include_unstarted=True):
        position = bucket.position
        if stage_id is not None and position.stage_id != stage_id:
            continue
        stage = refs["stages"].get(position.stage_id) if position.stage_id else None
        stations = [
            refs["stations"].get(station_id)
            for station_id in bucket.by_station
            if station_id is not None
        ]
        rows.append(
            {
                "item": {
                    "id": str(item.id),
                    "code": item.code,
                    "description": item.description,
                    "total_qty": item.total_qty,
                    "project_id": str(item.project_id),
                },
                "stage": _stage_payload(stage) if stage else None,
                "label": stage.name if stage else UNSTARTED_LABEL,
                "qty": bucket.qty,
                "sample_qty": bucket.sample_qty,
                "reworked_qty": bucket.reworked_qty,
                "stations": [
                    {"id": str(s.id), "code": s.code, "name": s.name} for s in stations if s
                ],
                "since": bucket.oldest_arrived_at.isoformat(),
                "days_in_state": _days_since(bucket.oldest_arrived_at, now),
                # Judged against the STAGE's threshold, not one global number:
                # outsourced rests for weeks by design. No threshold, no flag.
                "overdue": _overdue(stage, bucket.oldest_arrived_at, now),
                "is_unstarted": position.is_unstarted,
            }
        )

    if min_days is not None:
        rows = [row for row in rows if row["days_in_state"] >= min_days]
    rows.sort(key=lambda r: r["days_in_state"], reverse=True)
    if limit is not None:
        rows = rows[:limit]
    return {"generated_at": now.isoformat(), "rows": rows}


def item_state(db: Session, item_ids: list[uuid.UUID], now: datetime | None = None) -> dict:
    """Where each item is, for the item list and the logging screen."""
    now = now or utcnow()
    derivation = derive(db, item_ids=item_ids)
    refs = reference_maps(db)
    out: dict[str, dict] = {}

    for item_id, ledger in derivation.ledgers.items():
        buckets: list[Bucket] = ledger.occupied(include_unstarted=True)
        positions = []
        for bucket in buckets:
            position = bucket.position
            stage = refs["stages"].get(position.stage_id) if position.stage_id else None
            positions.append(
                {
                    # A place is "not started" or a stage; there is at most
                    # one row per stage, whatever the item's history.
                    "stage_id": str(stage.id) if stage else None,
                    "stage_code": stage.code if stage else None,
                    "stage_name": stage.name if stage else UNSTARTED_LABEL,
                    "qty": bucket.qty,
                    "sample_qty": bucket.sample_qty,
                    "reworked_qty": bucket.reworked_qty,
                    "station_ids": [str(sid) for sid in bucket.by_station if sid is not None],
                    "since": bucket.oldest_arrived_at.isoformat(),
                    "days_in_state": _days_since(bucket.oldest_arrived_at, now),
                    "overdue": _overdue(stage, bucket.oldest_arrived_at, now),
                    "is_unstarted": position.is_unstarted,
                }
            )
        out[str(item_id)] = {
            "positions": positions,
            # "sample" until the sample reaches Done; the batch exists from then.
            "phase": ledger.phase,
            "sample_qty": ledger.sample_qty,
            "sample_done_at": (
                ledger.released_at.isoformat()
                if ledger.released_at and ledger.sample_qty
                else None
            ),
            "completed_qty": ledger.completed_qty,
            "done_qty": ledger.done_qty,
            "unstarted_qty": ledger.unstarted_qty,
            "production_unstarted_qty": ledger.production_unstarted_qty,
            "event_count": ledger.applied_count,
            "last_event_at": (
                ledger.last_event_at.isoformat() if ledger.last_event_at else None
            ),
        }
    return out


def exceptions_report(db: Session) -> dict:
    """Everything the stored log asserts that does not add up.

    Over-advancing writes are rejected at the API since the write-time guard,
    so this mostly surfaces clock drift, late syncs, competing corrections,
    and rows that predate the guard. Fixes are correction events; an entry is
    never edited to make the numbers add up.
    """
    derivation = derive(db)
    rows = []
    for anomaly in derivation.anomalies:
        item = derivation.items.get(anomaly.item_id)
        rows.append(
            {
                "code": anomaly.code,
                "event_id": str(anomaly.event_id) if anomaly.event_id else None,
                "item_id": str(anomaly.item_id),
                "item_code": item.code if item else None,
                "detail": anomaly.detail,
                "occurred_at": anomaly.occurred_at.isoformat() if anomaly.occurred_at else None,
            }
        )
    rows.sort(key=lambda r: (r["occurred_at"] or "", r["code"]), reverse=True)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["code"]] = counts.get(row["code"], 0) + 1
    return {"generated_at": utcnow().isoformat(), "counts": counts, "rows": rows}
