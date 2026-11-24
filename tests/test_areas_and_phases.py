"""Areas and phases: which part of a job, and which phase of it, a batch is for.

An item is project + code + area + phase. The same code can be several batches
in one project, each with its own quantity and its own history -- and every
list and report can be narrowed to one area or one phase. Each project keeps
its own two lists, edited at runtime, so nothing here may depend on what they
contain: every test makes its own.
"""

from __future__ import annotations

import io
import uuid

import sqlalchemy as sa

from app.models import Area, Item, Phase
from app.services import reports

from tests.conftest import days_ago, hours_ago


def _labels(client) -> tuple[dict, dict]:
    ref = client.get("/api/reference").json()
    return ({a["name"]: a for a in ref["areas"]}, {p["name"]: p for p in ref["phases"]})


def _add(client, factory, noun, name, project=None, **fields):
    """Add an area or a phase to a project (the fixture's own by default)."""
    return client.post(f"/api/{noun}", json={
        "project_id": str((project or factory.project).id), "name": name, **fields,
    })


def _new_item(client, factory, code, qty=10, **fields):
    return client.post("/api/items", json={
        "code": code, "project_id": str(factory.project.id), "total_qty": qty, "sample_qty": 0, **fields,
    })


# -- the lists ----------------------------------------------------------------


def test_the_office_adds_renames_reorders_and_retires_areas_and_phases(client, factory):
    factory.db.commit()
    for noun in ("areas", "phases"):
        first = _add(client, factory, noun, "  Tower A  ", sort_order=20)
        assert first.status_code == 201
        assert first.json()["name"] == "Tower A"
        second = _add(client, factory, noun, "Tower B", sort_order=10).json()

        # One name, whatever the capitals.
        assert _add(client, factory, noun, "tower a").status_code == 409
        assert client.patch(f"/api/{noun}/{second['id']}", json={"name": "TOWER A"}).status_code == 409
        assert _add(client, factory, noun, "   ").status_code == 422

        renamed = client.patch(f"/api/{noun}/{first.json()['id']}", json={"name": "North tower"})
        assert renamed.json()["name"] == "North tower"
        retired = client.patch(f"/api/{noun}/{second['id']}", json={"is_active": False})
        assert retired.json()["is_active"] is False

        listed = client.get("/api/reference").json()[noun]
        # In the office's order, retired ones included so old items stay labelled.
        assert [(row["name"], row["is_active"]) for row in listed] == [
            ("Tower B", False), ("North tower", True),
        ]


def test_editing_the_lists_is_an_admin_job(client, factory):
    from fastapi.testclient import TestClient

    from app.main import app

    area = _add(client, factory, "areas", "Spa").json()
    client.post("/api/users", json={
        "username": "floor", "display_name": "Floor", "password": "a-long-password",
    })
    with TestClient(app) as floor:
        floor.post("/api/auth/login", json={"username": "floor", "password": "a-long-password"})
        assert _add(floor, factory, "areas", "Lobby").status_code == 403
        assert floor.patch(f"/api/areas/{area['id']}", json={"name": "x"}).status_code == 403
        assert _add(floor, factory, "phases", "Phase 1").status_code == 403
        assert floor.delete(f"/api/areas/{area['id']}").status_code == 403
        # Reading them is everyone's: the filters are built from the reference payload.
        assert [a["name"] for a in floor.get("/api/reference").json()["areas"]] == ["Spa"]


# -- what an item is ----------------------------------------------------------


def test_the_same_code_is_a_separate_batch_per_area_and_phase(client, factory):
    factory.db.commit()
    rooms = _add(client, factory, "areas", "Guest rooms").json()["id"]
    suites = _add(client, factory, "areas", "Suites").json()["id"]
    p1 = _add(client, factory, "phases", "Phase 1").json()["id"]
    p2 = _add(client, factory, "phases", "Phase 2").json()["id"]

    made = [
        _new_item(client, factory, "LF-01", 216, area_id=rooms, phase_id=p1),
        _new_item(client, factory, "LF-01", 68, area_id=rooms, phase_id=p2),   # another phase
        _new_item(client, factory, "LF-01", 13, area_id=suites, phase_id=p1),  # another area
        _new_item(client, factory, "LF-01", 5, area_id=rooms),                 # no phase is its own value
        _new_item(client, factory, "LF-01", 4),                                # and so is neither
    ]
    assert [r.status_code for r in made] == [201] * 5

    # Each identity exists once.
    again = _new_item(client, factory, "LF-01", 1, area_id=rooms, phase_id=p2)
    assert again.status_code == 409
    assert "LF-01 (Guest rooms, Phase 2) already exists in PRJ" in again.json()["detail"]
    assert _new_item(client, factory, "LF-01", 1).status_code == 409

    # Separate batches: logging one moves nothing in the others.
    one, two = made[0].json()["id"], made[1].json()["id"]
    posted = client.post("/api/events", json={
        "id": str(uuid.uuid4()), "item_id": one, "stage_id": str(factory.stage("paint").id),
        "from_unstarted": True, "station_id": str(factory.station("paint_1").id),
        "event_type_id": str(factory.event_type("move").id), "qty": 200,
        "occurred_at": hours_ago(1).isoformat(),
    })
    assert posted.status_code == 201, posted.text
    state = lambda item_id: client.get(f"/api/items/{item_id}").json()["state"]  # noqa: E731
    assert state(one)["unstarted_qty"] == 16
    assert state(two)["unstarted_qty"] == 68

    listed = client.get("/api/items?q=LF-01").json()["items"]
    assert sorted((i["total_qty"], i["area_id"], i["phase_id"]) for i in listed) == sorted([
        (216, rooms, p1), (68, rooms, p2), (13, suites, p1), (5, rooms, None), (4, None, None),
    ])


def test_an_edit_cannot_land_an_item_on_top_of_another_batch(client, factory):
    factory.db.commit()
    spa = _add(client, factory, "areas", "Spa").json()["id"]
    lobby = _add(client, factory, "areas", "Lobby").json()["id"]
    first = _new_item(client, factory, "ARM-1", area_id=spa).json()
    second = _new_item(client, factory, "ARM-1", area_id=lobby).json()

    clash = client.patch(f"/api/items/{second['id']}", json={"area_id": spa})
    assert clash.status_code == 409
    # Clearing the area is a change of identity too, and here it is free.
    cleared = client.patch(f"/api/items/{second['id']}", json={"area_id": None})
    assert cleared.status_code == 200 and cleared.json()["area_id"] is None
    # An edit that leaves the identity alone is not checked against itself.
    assert client.patch(f"/api/items/{first['id']}", json={"description": "x"}).status_code == 200
    assert client.patch(f"/api/items/{first['id']}", json={"area_id": spa}).status_code == 200
    assert client.patch(f"/api/items/{first['id']}", json={"area_id": str(uuid.uuid4())}).status_code == 422


def test_a_retired_area_cannot_be_chosen_but_its_items_stay_editable(client, factory):
    factory.db.commit()
    old = _add(client, factory, "areas", "Old wing").json()["id"]
    item = _new_item(client, factory, "BED-1", area_id=old).json()
    client.patch(f"/api/areas/{old}", json={"is_active": False})

    assert _new_item(client, factory, "BED-2", area_id=old).status_code == 409
    other = _new_item(client, factory, "BED-3").json()
    assert client.patch(f"/api/items/{other['id']}", json={"area_id": old}).status_code == 409
    # Already in it: an unrelated edit, with the area sent along, goes through.
    kept = client.patch(f"/api/items/{item['id']}", json={"description": "moved in", "area_id": old})
    assert kept.status_code == 200


# -- narrowing lists and reports ----------------------------------------------


def test_items_and_reports_narrow_to_an_area_and_a_phase(client, factory):
    a = factory.item("W-1", 10, area="Wing east", phase="First fix")
    b = factory.item("W-1", 20, area="Wing east", phase="Second fix")
    c = factory.item("W-2", 30, area="Wing west", phase="First fix")
    d = factory.item("W-3", 40)  # filed under neither
    factory.add_stage("gilding", sort=35)
    factory.move(a, "gilding", 10, at=days_ago(4))
    factory.move(b, "gilding", 5, at=days_ago(3))
    factory.move(c, "paint", 30, at=days_ago(2), station_code="paint_1")
    factory.db.commit()
    east = str(factory.label(Area, "Wing east").id)
    first = str(factory.label(Phase, "First fix").id)

    codes = lambda query: sorted(  # noqa: E731
        (i["code"], i["total_qty"]) for i in client.get(f"/api/items?{query}").json()["items"]
    )
    assert codes(f"area_id={east}") == [("W-1", 10), ("W-1", 20)]
    assert codes(f"phase_id={first}") == [("W-1", 10), ("W-2", 30)]
    assert codes(f"area_id={east}&phase_id={first}") == [("W-1", 10)]

    wip = lambda query: {  # noqa: E731
        r["stage"]["code"]: r["total_qty"]
        for r in client.get(f"/api/reports/wip?{query}").json()["stages"]
    }
    assert wip("") == {"gilding": 15, "paint": 30}
    assert wip(f"area_id={east}") == {"gilding": 15}
    assert wip(f"phase_id={first}") == {"gilding": 10, "paint": 30}
    assert wip(f"area_id={east}&phase_id={first}") == {"gilding": 10}
    assert client.get(f"/api/reports/wip?area_id={east}").json()["unstarted"]["qty"] == 15

    aging = client.get(f"/api/reports/aging?phase_id={first}").json()["rows"]
    assert [(r["item"]["code"], r["item"]["area"], r["item"]["phase"], r["qty"]) for r in aging] == [
        ("W-1", "Wing east", "First fix", 10),
        ("W-2", "Wing west", "First fix", 30),
    ]
    # A report row names the batch, not just the code: W-1 is two of them.
    everything = reports.aging_report(factory.db)["rows"]
    w1 = sorted((r["item"]["phase"], r["qty"]) for r in everything if r["item"]["code"] == "W-1")
    assert w1 == [("First fix", 10), ("Second fix", 5), ("Second fix", 15)]


def test_an_area_change_reaches_the_phones(client, factory):
    """The item list is only refetched when its token changes, so moving an
    item to another area has to change it."""
    item = factory.item("TOK-A", 5)
    factory.db.commit()
    lobby = _add(client, factory, "areas", "Lobby").json()["id"]
    before = client.get("/api/reference").json()["items_token"]
    assert client.patch(f"/api/items/{item.id}", json={"area_id": lobby}).status_code == 200
    assert client.get("/api/reference").json()["items_token"] != before


# -- the import ---------------------------------------------------------------


def _import(client, text: str, commit: bool = False):
    return client.post(
        "/api/import",
        files={"file": ("job.csv", text.encode(), "application/octet-stream")},
        data={"commit": "true" if commit else "false"},
    )


def test_a_file_files_items_under_areas_and_phases_and_adds_new_ones(client, factory):
    factory.label(Area, "Guest rooms")
    factory.db.commit()
    text = (
        "project,item,qty,sample qty,area,phase\n"
        "PRJ,LF-01,216,0,guest ROOMS,Phase 1\n"     # an existing area, however it is typed
        "PRJ,LF-01,68,0,Guest rooms,Phase 2\n"      # same code, another phase: another batch
        "PRJ,LF-01,13,0,Suites,phase 1\n"           # a new area; phase 1 again, differently typed
        "PRJ,SP-01,6,0,Spa,\n"                      # no phase
        "PRJ,GEN-1,4,0,,\n"                         # neither
    )
    preview = _import(client, text).json()
    assert preview["errors"] == []
    assert preview["new_areas"] == [{"project": "PRJ", "name": "Suites"}, {"project": "PRJ", "name": "Spa"}]
    assert preview["new_phases"] == [{"project": "PRJ", "name": "Phase 1"}, {"project": "PRJ", "name": "Phase 2"}]
    assert any("New areas for PRJ" in n and "Suites, Spa" in n for n in preview["notes"])
    assert [(i["code"], i["area"], i["phase"]) for i in preview["items"]] == [
        ("LF-01", "Guest rooms", "Phase 1"),   # the list's spelling, not the file's
        ("LF-01", "Guest rooms", "Phase 2"),
        ("LF-01", "Suites", "Phase 1"),        # one Phase 1, first spelling in the file
        ("SP-01", "Spa", None),
        ("GEN-1", None, None),
    ]
    # A preview adds nothing to the lists.
    areas, phases = _labels(client)
    assert (list(areas), list(phases)) == (["Guest rooms"], [])

    assert _import(client, text, commit=True).json()["committed"] is True
    areas, phases = _labels(client)
    assert (sorted(areas), sorted(phases)) == (["Guest rooms", "Spa", "Suites"], ["Phase 1", "Phase 2"])
    rows = factory.db.execute(
        sa.select(Item.code, Item.total_qty, Area.name, Phase.name)
        .outerjoin(Area, Area.id == Item.area_id)
        .outerjoin(Phase, Phase.id == Item.phase_id)
        .where(Item.code.in_(["LF-01", "SP-01", "GEN-1"]))
        .order_by(Item.total_qty)
    ).all()
    assert [tuple(r) for r in rows] == [
        ("GEN-1", 4, None, None), ("SP-01", 6, "Spa", None), ("LF-01", 13, "Suites", "Phase 1"),
        ("LF-01", 68, "Guest rooms", "Phase 2"), ("LF-01", 216, "Guest rooms", "Phase 1"),
    ]


def test_a_file_gives_each_project_its_own_areas(client, factory):
    """One file, two jobs, the same area name in both: two areas, one each. An
    area another project already has is not a match."""
    factory.label(Area, "Lobby")          # PRJ's lobby
    factory.db.commit()
    text = (
        "project,item,qty,area,phase\n"
        "HTL-A,TB-1,5,Lobby,Phase 1\n"
        "HTL-B,TB-1,7,lobby,Phase 1\n"
        "PRJ,TB-1,9,Lobby,\n"
    )
    preview = _import(client, text).json()
    assert preview["errors"] == []
    assert preview["new_areas"] == [{"project": "HTL-A", "name": "Lobby"}, {"project": "HTL-B", "name": "lobby"}]
    assert sum("New area for HTL-" in note for note in preview["notes"]) == 2

    assert _import(client, text, commit=True).json()["committed"] is True
    ref = client.get("/api/reference").json()
    code = {p["id"]: p["code"] for p in ref["projects"]}
    assert sorted((code[a["project_id"]], a["name"]) for a in ref["areas"]) == [
        ("HTL-A", "Lobby"), ("HTL-B", "lobby"), ("PRJ", "Lobby"),
    ]
    # Each item is in its own project's lobby.
    area_project = {a["id"]: a["project_id"] for a in ref["areas"]}
    for item in client.get("/api/items?q=TB-1").json()["items"]:
        assert area_project[item["area_id"]] == item["project_id"]


def test_a_file_may_not_repeat_or_overwrite_a_batch(client, factory):
    factory.item("LF-01", 10, area="Guest rooms", phase="Phase 1")
    factory.label(Area, "Closed wing").is_active = False
    factory.db.commit()
    text = (
        "project,item,qty,area,phase\n"
        "PRJ,LF-01,5,Guest rooms,Phase 2\n"     # row 2: fine, a new phase of it
        "PRJ,LF-01,5,Guest rooms,Phase 1\n"     # row 3: that batch exists
        "PRJ,LF-09,5,Suites,\n"                 # row 4: fine
        "PRJ,LF-09,7,suites,\n"                 # row 5: the same batch as row 4
        "PRJ,LF-10,5,,\n"                       # row 6: fine
        "PRJ,LF-10,5,,\n"                       # row 7: the same again, with nothing to tell them apart
        "PRJ,LF-11,5,Closed wing,\n"            # row 8: a retired area
        "OTHER,LF-01,5,Guest rooms,Phase 1\n"   # row 9: fine -- another project
    )
    body = _import(client, text).json()
    errors = {e["row"]: e["message"] for e in body["errors"]}
    assert sorted(errors) == [3, 5, 7, 8]
    assert "already exists in PRJ" in errors[3]
    assert "also on row 4" in errors[5]
    assert "different areas or phases" in errors[7]
    assert "retired" in errors[8]
    # Nothing from a refused file reaches the lists.
    assert _import(client, text, commit=True).status_code == 422
    assert "Suites" not in _labels(client)[0]
    assert client.get("/api/reference").json()["projects"][0]["code"] == "PRJ"  # and no OTHER project


def test_pictures_and_areas_travel_in_one_xlsx(client, factory, tmp_path, monkeypatch):
    import openpyxl

    monkeypatch.setenv("LSF_UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.config import get_settings

    get_settings.cache_clear()
    factory.db.commit()
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Project", "Item", "Qty", "Zone", "Delivery Phase"])   # loose headers
    sheet.append(["XL", "TB-1", 8, "Roof terrace", "Phase 3"])
    buffer = io.BytesIO()
    book.save(buffer)
    body = client.post(
        "/api/import",
        files={"file": ("job.xlsx", buffer.getvalue(), "application/octet-stream")},
        data={"commit": "true"},
    ).json()
    assert body["committed"] is True
    assert (body["items"][0]["area"], body["items"][0]["phase"]) == ("Roof terrace", "Phase 3")


# -- filing many at once ------------------------------------------------------


def _assign(client, items, **fields):
    return client.post("/api/items/assign", json={"item_ids": [str(i.id) for i in items], **fields})


def test_a_selection_of_items_is_filed_in_one_go(client, factory):
    beds = [factory.item(f"BED-{n}", 10, phase="Phase 1") for n in range(4)]
    chair = factory.item("CHR-1", 10)
    rooms = str(factory.label(Area, "Guest rooms").id)
    two = str(factory.label(Phase, "Phase 2").id)
    factory.db.commit()
    before = client.get("/api/reference").json()["items_token"]

    def filed():
        factory.db.expire_all()
        rows = factory.db.execute(
            sa.select(Item.code, Area.name, Phase.name)
            .outerjoin(Area, Area.id == Item.area_id)
            .outerjoin(Phase, Phase.id == Item.phase_id)
            .order_by(Item.code)
        )
        return {code: (area, phase) for code, area, phase in rows}

    # Only what is sent changes: the area is set, each item's phase is left alone.
    done = _assign(client, beds[:3] + [chair], area_id=rooms)
    assert done.status_code == 200 and done.json() == {"updated": 4, "split": 0}
    assert filed() == {
        "BED-0": ("Guest rooms", "Phase 1"), "BED-1": ("Guest rooms", "Phase 1"),
        "BED-2": ("Guest rooms", "Phase 1"), "BED-3": (None, "Phase 1"),
        "CHR-1": ("Guest rooms", None),
    }
    assert client.get("/api/reference").json()["items_token"] != before  # the phones hear of it

    # Both at once; and null un-files.
    assert _assign(client, [beds[3]], area_id=rooms, phase_id=two).status_code == 200
    assert _assign(client, [chair], area_id=None).status_code == 200
    assert filed()["BED-3"] == ("Guest rooms", "Phase 2")
    assert filed()["CHR-1"] == (None, None)

    # Naming neither is not a request to clear both.
    assert _assign(client, [chair]).status_code == 422


def test_filing_refuses_the_whole_selection_if_two_batches_would_merge(client, factory):
    one = factory.item("LF-01", 216, area="Guest rooms", phase="Phase 1")
    two = factory.item("LF-01", 68, area="Guest rooms", phase="Phase 2")
    other = factory.item("LF-03", 30, phase="Phase 2")
    suites = factory.item("LF-01", 13, area="Suites", phase="Phase 1")   # not ticked
    p1 = str(factory.label(Phase, "Phase 1").id)
    suites_area = str(factory.label(Area, "Suites").id)
    factory.db.commit()

    # Two ticked items collapsing into one identity.
    merged = _assign(client, [one, two, other], phase_id=p1)
    assert merged.status_code == 409
    assert "nothing was changed" in merged.json()["detail"] and "LF-01" in merged.json()["detail"]
    # A ticked item landing on one that was not ticked.
    landed = _assign(client, [one, other], area_id=suites_area)
    assert landed.status_code == 409

    factory.db.expire_all()
    assert factory.db.get(Item, other.id).phase_id == factory.label(Phase, "Phase 2").id  # untouched
    assert factory.db.get(Item, one.id).area_id == factory.label(Area, "Guest rooms").id
    assert suites.id  # still its own batch

    # Swapping phases across a pair is fine when the result is still distinct.
    assert _assign(client, [other], phase_id=p1).status_code == 200


def test_filing_is_an_admin_job_and_checks_what_it_is_given(client, factory):
    from fastapi.testclient import TestClient

    from app.main import app

    item = factory.item("ONE-1", 5)
    retired = factory.label(Area, "Closed wing")
    retired.is_active = False
    factory.db.commit()

    assert _assign(client, [item], area_id=str(retired.id)).status_code == 409
    assert _assign(client, [item], area_id=str(uuid.uuid4())).status_code == 422
    gone = client.post("/api/items/assign", json={
        "item_ids": [str(item.id), str(uuid.uuid4())], "area_id": None,
    })
    assert gone.status_code == 422 and "no longer exist" in gone.json()["detail"]

    client.post("/api/users", json={
        "username": "floor", "display_name": "Floor", "password": "a-long-password",
    })
    with TestClient(app) as floor:
        floor.post("/api/auth/login", json={"username": "floor", "password": "a-long-password"})
        assert floor.post("/api/items/assign", json={"item_ids": [str(item.id)], "area_id": None}).status_code == 403


# -- each project has its own -------------------------------------------------


def _second_project(client, factory):
    from app.models import Project

    other = Project(code="OTHER", name="Another job")
    factory.db.add(other)
    factory.db.commit()
    return other


def test_two_projects_can_each_have_a_spa(client, factory):
    other = _second_project(client, factory)
    mine = _add(client, factory, "areas", "Spa")
    theirs = _add(client, factory, "areas", "spa", project=other)
    assert (mine.status_code, theirs.status_code) == (201, 201)
    assert mine.json()["project_id"] == str(factory.project.id)
    assert theirs.json()["project_id"] == str(other.id)
    # Still one per project, and a rename cannot collide inside its own project.
    assert _add(client, factory, "areas", "SPA").status_code == 409
    lobby = _add(client, factory, "areas", "Lobby", project=other).json()
    assert client.patch(f"/api/areas/{lobby['id']}", json={"name": "Spa"}).status_code == 409
    assert client.patch(f"/api/areas/{mine.json()['id']}", json={"name": "Lobby"}).status_code == 200
    assert _add(client, factory, "areas", "x", project=type("P", (), {"id": uuid.uuid4()})).status_code == 422


def test_an_item_can_only_be_filed_under_its_own_projects_lists(client, factory):
    other = _second_project(client, factory)
    theirs = _add(client, factory, "areas", "Their wing", project=other).json()["id"]
    their_phase = _add(client, factory, "phases", "Their phase", project=other).json()["id"]
    mine = _add(client, factory, "areas", "My wing").json()["id"]

    refused = _new_item(client, factory, "X-1", area_id=theirs)
    assert refused.status_code == 409
    assert "belongs to project OTHER" in refused.json()["detail"]
    item = _new_item(client, factory, "X-1", area_id=mine).json()
    assert client.patch(f"/api/items/{item['id']}", json={"phase_id": their_phase}).status_code == 409
    assert client.post("/api/items/assign", json={"item_ids": [item["id"]], "area_id": theirs}).status_code == 409

    # Moving to another project: its area cannot come along...
    moved = client.patch(f"/api/items/{item['id']}", json={"project_id": str(other.id)})
    assert moved.status_code == 409 and "My wing" in moved.json()["detail"]
    # ...so it is re-filed (or un-filed) in the same step.
    moved = client.patch(
        f"/api/items/{item['id']}", json={"project_id": str(other.id), "area_id": theirs}
    )
    assert moved.status_code == 200
    assert (moved.json()["project_id"], moved.json()["area_id"]) == (str(other.id), theirs)
    back = client.patch(
        f"/api/items/{item['id']}", json={"project_id": str(factory.project.id), "area_id": None}
    )
    assert back.status_code == 200 and back.json()["area_id"] is None


def test_filing_a_selection_stays_inside_one_project(client, factory):
    from app.models import Project

    other = _second_project(client, factory)
    here = factory.item("A-1", 5)
    there = Item(code="A-1", project_id=other.id, description="x", total_qty=5,
                 sample_qty=0, drawing_revision="A")
    factory.db.add(there)
    wing = factory.label(Area, "Wing")
    factory.db.commit()
    assert isinstance(other, Project)

    mixed = client.post("/api/items/assign", json={
        "item_ids": [str(here.id), str(there.id)], "area_id": str(wing.id),
    })
    assert mixed.status_code == 409 and "more than one project" in mixed.json()["detail"]
    # Un-filing has no such limit: "none" belongs to every project.
    cleared = client.post("/api/items/assign", json={
        "item_ids": [str(here.id), str(there.id)], "area_id": None,
    })
    assert cleared.status_code == 200 and cleared.json() == {"updated": 2, "split": 0}


def test_an_unused_area_or_phase_can_be_deleted_and_a_used_one_cannot(client, factory):
    factory.db.commit()
    for noun, field in (("areas", "area_id"), ("phases", "phase_id")):
        typo = _add(client, factory, noun, "Gest rooms").json()["id"]
        used = _add(client, factory, noun, "Guest rooms").json()["id"]
        item = _new_item(client, factory, f"DEL-{noun}", **{field: used}).json()

        assert client.delete(f"/api/{noun}/{typo}").json() == {"deleted": True}
        kept = client.delete(f"/api/{noun}/{used}")
        assert kept.status_code == 409 and "1 item(s)" in kept.json()["detail"]
        # An archived item still counts: reactivated, it would come back un-filed.
        posted = client.post("/api/events", json={
            "id": str(uuid.uuid4()), "item_id": item["id"], "stage_id": str(factory.stage("qc").id),
            "from_unstarted": True, "event_type_id": str(factory.event_type("move").id), "qty": 1,
            "occurred_at": hours_ago(1).isoformat(),
        })
        assert posted.status_code == 201, posted.text
        assert client.delete(f"/api/items/{item['id']}").json()["archived"] is True
        still = client.delete(f"/api/{noun}/{used}")
        assert still.status_code == 409 and "archived ones included" in still.json()["detail"]
        # Once nothing at all is filed under it, it can go.
        assert client.delete(f"/api/items/{item['id']}?purge=true").json()["deleted"] is True
        assert client.delete(f"/api/{noun}/{used}").json() == {"deleted": True}
        assert client.get("/api/reference").json()[noun] == []
        assert client.delete(f"/api/{noun}/{uuid.uuid4()}").status_code == 404
