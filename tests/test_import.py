"""Project import: a spreadsheet in, projects and sample-first items out."""

from __future__ import annotations

import io

import sqlalchemy as sa

from app.models import Item, Project

HEADER = "project,project name,client,item,description,qty,sample qty,revision,stages\n"


def _post(client, text: str, commit: bool = False, filename: str = "job.csv"):
    content = text.encode() if isinstance(text, str) else text
    return client.post(
        "/api/import",
        files={"file": (filename, content, "application/octet-stream")},
        data={"commit": "true" if commit else "false"},
    )


def _item(factory, code):
    factory.db.expire_all()
    return factory.db.scalars(sa.select(Item).where(Item.code == code)).first()


def test_preview_then_commit_creates_the_project_and_its_items(client, factory):
    text = HEADER + (
        "hotel-b,Grand Hotel,Acme,BST-1,Bedside table,120,,A,Carpentry > Paint > Packing\n"
        "HOTEL-B,,,WRD-1,Wardrobe,40,2,B,carpentry>veneer>paint>packing\n"
    )
    preview = _post(client, text)
    assert preview.status_code == 200
    body = preview.json()
    assert body["errors"] == []
    assert body["committed"] is False
    assert body["projects"] == [
        {"code": "HOTEL-B", "name": "Grand Hotel", "client": "Acme", "is_new": True, "item_count": 2}
    ]
    assert body["items"][1]["stages"] == ["Carpentry", "Veneer", "Paint", "Packing"]
    # A preview writes nothing.
    assert _item(factory, "BST-1") is None

    done = _post(client, text, commit=True)
    assert done.status_code == 200 and done.json()["committed"] is True

    bst = _item(factory, "BST-1")
    assert (bst.total_qty, bst.sample_qty, bst.drawing_revision) == (120, 1, "A")
    assert [s.seq for s in bst.steps] == [10, 20, 30]
    wrd = _item(factory, "WRD-1")
    assert (wrd.sample_qty, wrd.drawing_revision, len(wrd.steps)) == (2, "B", 4)

    # On the floor as a sample: only the sample exists yet.
    state = client.get(f"/api/items/{bst.id}").json()["state"]
    assert state["phase"] == "sample"
    assert state["unstarted_qty"] == 1


def test_every_problem_is_reported_by_row_and_nothing_is_written(client, factory):
    factory.item("TAKEN-1", 5, ["carpentry"])
    factory.db.commit()
    text = HEADER + (
        "JOB-1,,,OK-1,fine,10,,,Carpentry > Packing\n"          # row 2: fine
        "JOB-1,,,BAD-1,,ten,,,Carpentry\n"                       # row 3: qty
        "JOB-1,,,BAD-2,,5,,,Carpentry > Gilding\n"               # row 4: unknown stage
        "JOB-1,,,OK-1,,5,,,Carpentry\n"                          # row 5: duplicate in file
        "JOB-1,,,TAKEN-1,,5,,,Carpentry\n"                       # row 6: exists already
        "JOB-1,,,BAD-3,,5,,,Packing > Carpentry\n"               # row 7: terminal not last
    )
    preview = _post(client, text).json()
    rows = sorted({e["row"] for e in preview["errors"]})
    assert rows == [3, 4, 5, 6, 7]
    assert any("Gilding" in e["message"] for e in preview["errors"])

    refused = _post(client, text, commit=True)
    assert refused.status_code == 422
    assert _item(factory, "OK-1") is None
    assert factory.db.scalars(sa.select(Project).where(Project.code == "JOB-1")).first() is None


def test_an_existing_project_is_added_to_not_renamed(client, factory):
    factory.db.commit()
    text = HEADER + "PRJ,Renamed by a file,Someone,ADD-1,,3,0,,Carpentry\n"
    body = _post(client, text, commit=True).json()
    assert body["projects"][0]["is_new"] is False
    factory.db.expire_all()
    assert factory.project.name == "Test project"
    assert _item(factory, "ADD-1").project_id == factory.project.id
    assert _item(factory, "ADD-1").sample_qty == 0


def test_archived_project_is_refused(client, factory):
    factory.project.is_active = False
    factory.db.commit()
    body = _post(client, HEADER + "PRJ,,,X-1,,3,,,Carpentry\n").json()
    assert "archived" in body["errors"][0]["message"]


def test_missing_required_column_is_named(client, factory):
    factory.db.commit()
    body = _post(client, "project,item,qty\nP,I,1\n").json()
    assert "stages" in body["errors"][0]["message"]


def test_a_stage_added_this_morning_is_usable_in_this_afternoons_file(client, factory):
    """CLAUDE.md rule 2, through the import: stages match against the table."""
    factory.add_stage("gilding", sort=33)
    factory.db.commit()
    body = _post(client, HEADER + "ART-1,,,GLD-1,,4,,,Carpentry > Gilding\n", commit=True).json()
    assert body["committed"] is True
    assert body["items"][0]["stages"] == ["Carpentry", "Gilding"]


def test_xlsx_with_loose_headers(client, factory):
    import openpyxl

    factory.db.commit()
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Project Code", "Item Code", "Description", "Quantity", "Sample_Qty", "Stages"])
    sheet.append(["xl-1", "XL-ITEM", "From Excel", 24.0, 1, "Carpentry; Paint; Packing"])
    buffer = io.BytesIO()
    book.save(buffer)

    body = _post(client, buffer.getvalue(), commit=True, filename="job.xlsx").json()
    assert body["errors"] == []
    item = _item(factory, "XL-ITEM")
    assert (item.total_qty, item.sample_qty, len(item.steps)) == (24, 1, 3)


def test_template_lists_the_stages_that_exist_now(client, factory):
    factory.add_stage("gilding", sort=33)
    factory.db.commit()
    response = client.get("/api/import/template")
    assert response.status_code == 200
    assert "Gilding" in response.text
    header = response.text.splitlines()[0]
    assert header.startswith("project,")
    # The template parses cleanly as an import.
    assert _post(client, response.text).json()["errors"] == []
