"""The CLAUDE.md contract: a stage added at runtime changes output, never code.

Every test here inserts a brand-new stage (and in one case a new state) after the
application is fully wired, then exercises the reports and the release path. If a
change to the codebase makes any of these fail, that change hardcoded a stage
assumption and is the thing to fix.
"""

from __future__ import annotations

from app.services import reports
from app.services.derivation import derive
from tests.conftest import days_ago, hours_ago


def test_runtime_stage_flows_through_release_and_reports(factory):
    # The factory adds a "polishing" stage today, with no deploy.
    factory.add_stage("polishing", sort=35, requires_station=False)
    route = factory.route("r2", ["carpentry", "polishing", "packing"])
    item = factory.item("POL-1", 25, route)

    factory.log(item, 10, "completed", 25, at=days_ago(2), station_code="carpentry_1")
    factory.log(item, 20, "in_progress", 25, at=days_ago(1))

    wip = reports.wip_report(factory.db)
    polishing_row = next(
        row for row in wip["stages"] if row["stage"]["code"] == "polishing"
    )
    assert polishing_row["total_qty"] == 25
    assert polishing_row["by_state"]["in_progress"] == 25

    aging = reports.aging_report(factory.db)
    row = next(
        r for r in aging["rows"] if r["stage"] and r["stage"]["code"] == "polishing"
    )
    assert 0.9 < row["days_in_state"] < 1.1


def test_new_template_version_leaves_inflight_items_alone(factory):
    factory.add_stage("sanding", sort=15)
    route_v1 = factory.route("beds", ["carpentry", "packing"], version=1)
    inflight = factory.item("BED-1", 10, route_v1)
    factory.log(inflight, 10, "queued", 10, at=hours_ago(3), station_code="carpentry_1")

    # New version inserts sanding between the existing steps using the seq gaps.
    route_v2 = factory.route("beds", ["carpentry", "sanding", "packing"], version=2)
    fresh = factory.item("BED-2", 10, route_v2)

    assert [s.seq for s in inflight.steps] == [10, 20]
    assert len(fresh.steps) == 3
    # The in-flight item still derives against its snapshot, untouched.
    ledger = derive(factory.db, item_ids=[inflight.id]).ledger_for(inflight.id)
    assert len([p for p in ledger.positions if not p.is_unstarted]) == 2 * 3


def test_wip_report_iterates_states_from_the_table(factory):
    """A fourth state added at runtime appears in every report row."""
    from app.models import EventState

    factory.db.add(
        EventState(code="drying", name="Drying", sort_order=25, is_complete=False)
    )
    factory.db.flush()

    route = factory.route("r3", ["paint"])
    item = factory.item("DRY-1", 8, route)
    factory.log(item, 10, "in_progress", 8, at=hours_ago(4), station_code="paint_1")
    factory.log(item, 10, "drying", 8, at=hours_ago(2), station_code="paint_1")

    wip = reports.wip_report(factory.db)
    paint_row = next(row for row in wip["stages"] if row["stage"]["code"] == "paint")
    assert paint_row["by_state"]["drying"] == 8
    assert "drying" in [s["code"] for s in wip["states"]]


def test_queue_depth_comes_from_state_order_not_name(factory):
    """Rename-proof: queue depth keys off the earliest state by sort_order."""
    route = factory.route("r4", ["carpentry"])
    item = factory.item("Q-1", 12, route)
    factory.log(item, 10, "queued", 12, at=hours_ago(1), station_code="carpentry_1")

    wip = reports.wip_report(factory.db)
    row = next(r for r in wip["stages"] if r["stage"]["code"] == "carpentry")
    assert row["queue_qty"] == 12
    assert wip["queue_state_code"] == "queued"


def test_no_stage_codes_hardcoded_outside_seeds_and_tests():
    """Enforce CLAUDE.md rule 2 mechanically: the seeded stage codes must not
    appear as literals anywhere in app/."""
    import pathlib
    import re

    from seeds.seed import STAGES

    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text()
        for code, *_ in STAGES:
            if re.search(rf"[\"']{re.escape(code)}[\"']", text):
                offenders.append(f"{path.name}: '{code}'")
    assert not offenders, f"stage codes hardcoded in app/: {offenders}"
