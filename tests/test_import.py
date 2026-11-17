"""Project import: a spreadsheet in, projects and sample-first items out.

Most files here still carry the `stages` column from before items lost their
sequences. That is deliberate: those files are on people's desktops, and they
must keep importing -- the column is read past, and the preview says so."""

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
    # The old stages column is skipped, and the preview says so.
    assert "stages" not in body["items"][1]
    assert any("stages column" in note for note in body["notes"])
    # A preview writes nothing.
    assert _item(factory, "BST-1") is None

    done = _post(client, text, commit=True)
    assert done.status_code == 200 and done.json()["committed"] is True

    bst = _item(factory, "BST-1")
    assert (bst.total_qty, bst.sample_qty, bst.drawing_revision) == (120, 1, "A")
    assert bst.steps == []  # no sequence is created for anyone any more
    wrd = _item(factory, "WRD-1")
    assert (wrd.sample_qty, wrd.drawing_revision) == (2, "B")

    # On the floor as a sample: only the sample exists yet.
    state = client.get(f"/api/items/{bst.id}").json()["state"]
    assert state["phase"] == "sample"
    assert state["unstarted_qty"] == 1


def test_every_problem_is_reported_by_row_and_nothing_is_written(client, factory):
    factory.item("TAKEN-1", 5)
    factory.db.commit()
    text = HEADER + (
        "JOB-1,,,OK-1,fine,10,,,Carpentry > Packing\n"          # row 2: fine
        "JOB-1,,,BAD-1,,ten,,,Carpentry\n"                       # row 3: qty
        "JOB-1,,,OK-2,,5,,,Carpentry > Gilding\n"                # row 4: fine -- stages are not read
        "JOB-1,,,OK-1,,5,,,Carpentry\n"                          # row 5: duplicate in file
        "JOB-1,,,TAKEN-1,,5,,,Carpentry\n"                       # row 6: exists already
        "JOB-1,,,BAD-3,,5,many,,\n"                              # row 7: sample qty
    )
    preview = _post(client, text).json()
    rows = sorted({e["row"] for e in preview["errors"]})
    assert rows == [3, 5, 6, 7]

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
    body = _post(client, "project,item\nP,I\n").json()
    assert "qty" in body["errors"][0]["message"]


def test_a_file_needs_no_stages_and_its_items_go_anywhere(client, factory):
    """The current format: project, item, qty. The item it creates can be
    logged at a stage that was added to the factory after the import."""
    factory.db.commit()
    body = _post(client, "project,item,qty,sample qty\nART-1,GLD-1,4,0\n", commit=True).json()
    assert body["committed"] is True
    assert body["notes"] == []  # nothing was skipped: there was no stages column

    factory.add_stage("gilding", sort=33)
    factory.db.commit()
    item = _item(factory, "GLD-1")
    factory.move(item, "gilding", 4)
    factory.db.commit()
    state = client.get(f"/api/items/{item.id}").json()["state"]
    assert [(p["stage_code"], p["qty"]) for p in state["positions"]] == [("gilding", 4)]


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
    assert (item.total_qty, item.sample_qty) == (24, 1)


def test_template_is_a_header_and_an_example_that_imports(client, factory):
    factory.db.commit()
    response = client.get("/api/import/template")
    assert response.status_code == 200
    header = response.text.splitlines()[0]
    assert header == "project,project name,client,item,description,qty,sample qty,revision"
    # The template parses cleanly as an import, with nothing skipped.
    body = _post(client, response.text).json()
    assert body["errors"] == [] and body["notes"] == []
