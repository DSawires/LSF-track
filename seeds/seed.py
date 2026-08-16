"""Idempotent seed data.

This file and the tests are the only places stage, station, state and event-type
codes may appear as string literals. Running it twice changes nothing.

Insert-if-missing ONLY, never update: stages and their behaviour flags are
runtime data that the factory tunes through the admin UI, and the entrypoint
re-runs this seed on every container start. An upsert here would silently
revert an operator's edits on each restart -- the exact failure the
"stages are data, not code" rule exists to prevent.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import EventState, EventType, ReasonCode, Stage, Station

STAGES = [
    # code, name, sort, requires_station, requires_external_po, is_terminal
    ("carpentry", "Carpentry", 10, True, False, False),
    ("lipping", "Lipping", 15, False, False, False),
    ("veneer", "Veneer", 20, True, False, False),
    ("paint", "Paint", 30, True, False, False),
    ("upholstery", "Upholstery", 40, True, False, False),
    ("outsourced", "Outsourced", 50, False, True, False),
    ("qc", "Quality Control", 60, False, False, False),
    ("packing", "Packing", 70, False, False, True),
]

STATIONS = [
    # code, stage_code, name, sort
    ("carpentry_1", "carpentry", "Carpentry 1", 10),
    ("carpentry_2", "carpentry", "Carpentry 2", 20),
    ("veneer_1", "veneer", "Veneer 1", 10),
    ("paint_1", "paint", "Paint 1", 10),
    ("paint_2", "paint", "Paint 2", 20),
    ("upholstery_1", "upholstery", "Upholstery 1", 10),
]

STATES = [
    # code, name, sort, is_complete, is_initial
    ("queued", "Queued", 10, False, True),
    ("in_progress", "In progress", 20, False, False),
    ("completed", "Completed", 30, True, False),
]

EVENT_TYPES = [
    # code, name, sort, flags dict
    ("move", "Stage movement", 10, {}),
    (
        "rework_return",
        "Rework return",
        20,
        {"is_rework": True, "requires_reason_code": True},
    ),
    (
        "correction",
        "Correction",
        30,
        {"is_correction": True, "moves_quantity": False, "requires_item_step": False},
    ),
    (
        "release",
        "Released to production",
        40,
        {"is_release": True, "moves_quantity": False, "requires_item_step": False},
    ),
    (
        "revision_bump",
        "Drawing revision bump",
        50,
        {"is_revision_bump": True, "moves_quantity": False, "requires_item_step": False},
    ),
    (
        "archive",
        "Archived",
        60,
        {"is_archive": True, "moves_quantity": False, "requires_item_step": False},
    ),
]

REASON_CODES = [
    ("finish_defect", "Finish defect", 10),
    ("dimension_error", "Dimensional error", 20),
    ("damage_in_transit", "Damaged in transit", 30),
    ("supplier_reject", "Supplier reject", 40),
    ("drawing_change", "Drawing change", 50),
]


def _insert_if_missing(db: Session, model, code: str, values: dict) -> None:
    row = db.scalars(sa.select(model).where(model.code == code)).first()
    if row is None:
        db.add(model(code=code, **values))


def run(db: Session) -> None:
    for code, name, sort, requires_station, external_po, terminal in STAGES:
        _insert_if_missing(
            db,
            Stage,
            code,
            {
                "name": name,
                "sort_order": sort,
                "requires_station": requires_station,
                "requires_external_po": external_po,
                "is_terminal": terminal,
            },
        )
    db.flush()

    stage_ids = {s.code: s.id for s in db.scalars(sa.select(Stage))}
    for code, stage_code, name, sort in STATIONS:
        _insert_if_missing(
            db,
            Station,
            code,
            {"stage_id": stage_ids[stage_code], "name": name, "sort_order": sort},
        )

    for code, name, sort, is_complete, is_initial in STATES:
        _insert_if_missing(
            db,
            EventState,
            code,
            {
                "name": name,
                "sort_order": sort,
                "is_complete": is_complete,
                "is_initial": is_initial,
            },
        )

    for code, name, sort, flags in EVENT_TYPES:
        _insert_if_missing(db, EventType, code, {"name": name, "sort_order": sort, **flags})

    for code, name, sort in REASON_CODES:
        _insert_if_missing(db, ReasonCode, code, {"name": name, "sort_order": sort})

    db.flush()
