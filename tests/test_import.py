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
        "PRJ,,,TAKEN-1,,5,,,Carpentry\n"                         # row 6: exists already in PRJ
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
    assert header == (
        "project,project name,client,item,description,dimensions,qty,sample qty,revision,"
        "area,phase,picture"
    )
    # The template parses cleanly as an import, with nothing skipped.
    body = _post(client, response.text).json()
    assert body["errors"] == [] and body["notes"] == []


# -- pictures ------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"png-bytes" * 20
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 20
EMF = b"\x01\x00\x00\x00" + b"not-a-picture-we-store" * 10

_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _xlsx(rows, pictures=(), extra_parts=None, drawing_xml=None) -> bytes:
    """A real .xlsx with pictures placed over cells, the way Excel, LibreOffice
    and openpyxl all store them: a drawing part hanging off the sheet, one
    anchor per picture, each pointing at a file under xl/media.

    `pictures` is (spreadsheet row, column index, bytes, one-cell anchor?).
    Built by hand so the test suite needs no imaging library.
    """
    import zipfile

    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    plain = io.BytesIO()
    book.save(plain)
    if not pictures and not extra_parts and drawing_xml is None:
        return plain.getvalue()

    anchors, media_rels, media = [], [], {}
    for n, (row, column, data, one_cell) in enumerate(pictures, start=1):
        media[f"xl/media/image{n}.bin"] = data
        media_rels.append(
            f'<Relationship Id="rId{n}" Type="{_REL}/image" Target="../media/image{n}.bin"/>'
        )
        start = f"<xdr:from><xdr:col>{column}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{row - 1}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        pic = (
            f'<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{n}" name="Picture {n}"/><xdr:cNvPicPr/></xdr:nvPicPr>'
            f'<xdr:blipFill><a:blip r:embed="rId{n}"/></xdr:blipFill><xdr:spPr/></xdr:pic><xdr:clientData/>'
        )
        if one_cell:
            anchors.append(f'<xdr:oneCellAnchor>{start}<xdr:ext cx="1" cy="1"/>{pic}</xdr:oneCellAnchor>')
        else:
            end = f"<xdr:to><xdr:col>{column + 1}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{row}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
            anchors.append(f"<xdr:twoCellAnchor>{start}{end}{pic}</xdr:twoCellAnchor>")
    drawing = drawing_xml or (
        f'<xdr:wsDr xmlns:xdr="{_XDR}" xmlns:a="{_A}" xmlns:r="{_REL}">{"".join(anchors)}</xdr:wsDr>'
    )

    out = io.BytesIO()
    with zipfile.ZipFile(plain) as source, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "xl/worksheets/sheet1.xml" and (pictures or drawing_xml):
                data = data.replace(
                    b"</worksheet>", f'<drawing xmlns:r="{_REL}" r:id="rId1"/></worksheet>'.encode()
                )
            target.writestr(name, data)
        if pictures or drawing_xml:
            target.writestr(
                "xl/worksheets/_rels/sheet1.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{_REL}/drawing" Target="../drawings/drawing1.xml"/></Relationships>',
            )
            target.writestr("xl/drawings/drawing1.xml", drawing)
            target.writestr(
                "xl/drawings/_rels/drawing1.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'{"".join(media_rels)}</Relationships>',
            )
        for name, data in {**media, **(extra_parts or {})}.items():
            target.writestr(name, data)
    return out.getvalue()


ITEM_ROWS = [
    ["project", "item", "qty", "description", "picture"],
    ["PIC", "CHAIR-1", 12, "Dining chair", None],     # row 2
    ["PIC", "TABLE-1", 4, "Dining table", None],      # row 3
    ["PIC", "STOOL-1", 6, "Bar stool", None],         # row 4
]


def _use_tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("LSF_UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.config import get_settings
    from app.storage import get_storage

    get_settings.cache_clear()
    get_storage.cache_clear()
    return get_storage()


def _upload(client, content: bytes, commit: bool = False):
    return client.post(
        "/api/import",
        files={"file": ("job.xlsx", content, "application/octet-stream")},
        data={"commit": "true" if commit else "false"},
    )


def test_a_picture_on_an_items_row_becomes_its_icon(client, factory, tmp_path, monkeypatch):
    storage = _use_tmp_storage(tmp_path, monkeypatch)
    factory.db.commit()
    content = _xlsx(ITEM_ROWS, pictures=[
        (2, 4, PNG, False),    # CHAIR-1: in the picture column
        (3, 6, JPEG, True),    # TABLE-1: two on its row, in any column...
        (3, 1, PNG, False),    # ...and the left-most one wins
        (1, 4, JPEG, False),   # on the header row: belongs to no item
    ])

    preview = _upload(client, content).json()
    assert preview["errors"] == [] and preview["notes"] == []
    assert [(i["code"], i["has_picture"]) for i in preview["items"]] == [
        ("CHAIR-1", True), ("TABLE-1", True), ("STOOL-1", False),
    ]
    # A preview writes nothing -- no rows, and no files either.
    assert storage.list("images/") == []

    assert _upload(client, content, commit=True).json()["committed"] is True

    items = {i["code"]: i for i in client.get("/api/items").json()["items"]}
    assert items["STOOL-1"]["icon_url"] is None
    served = client.get(items["CHAIR-1"]["icon_url"])
    assert served.status_code == 200
    assert (served.content, served.headers["content-type"]) == (PNG, "image/png")
    assert client.get(items["TABLE-1"]["icon_url"]).content == PNG  # the left-most
    assert len(storage.list("images/")) == 2

    # Stored like an icon uploaded by hand: listed on the item, under the
    # importer's name, replaceable and deletable from the item editor.
    listed = client.get(f"/api/items/{items['CHAIR-1']['id']}/images").json()["images"]
    assert [(i["kind"], i["content_type"], i["size_bytes"]) for i in listed] == [
        ("icon", "image/png", len(PNG))
    ]


def test_a_picture_that_cannot_be_used_is_left_out_and_said_so(client, factory, tmp_path, monkeypatch):
    """Pictures ride on the import; they never sink it."""
    from app.services import imports

    storage = _use_tmp_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(imports, "MAX_PICTURE_BYTES", 150)
    factory.db.commit()
    content = _xlsx(ITEM_ROWS, pictures=[
        (2, 4, EMF[:100], False),     # what Excel stores for some pasted drawings
        (3, 4, JPEG, False),          # 204 bytes: over the (patched) cap
        (4, 4, PNG[:120], False),     # fine
    ])

    body = _upload(client, content, commit=True).json()
    assert body["committed"] is True
    assert [i["has_picture"] for i in body["items"]] == [False, False, True]
    assert any("row(s) 2" in n and "left out" in n for n in body["notes"])
    assert any("row(s) 3" in n and "larger than" in n for n in body["notes"])
    assert len(storage.list("images/")) == 1
    assert _item(factory, "CHAIR-1") is not None  # the items all arrived


def test_pictures_placed_inside_cells_are_named_as_unreadable(client, factory, tmp_path, monkeypatch):
    _use_tmp_storage(tmp_path, monkeypatch)
    factory.db.commit()
    content = _xlsx(ITEM_ROWS, extra_parts={"xl/richData/rdrichvalue.xml": b"<rvData/>"})
    body = _upload(client, content, commit=True).json()
    assert body["committed"] is True
    assert any("Place over Cells" in n for n in body["notes"])
    assert not any(i["has_picture"] for i in body["items"])


def test_a_mangled_drawing_part_does_not_stop_the_items(client, factory, tmp_path, monkeypatch):
    _use_tmp_storage(tmp_path, monkeypatch)
    factory.db.commit()
    content = _xlsx(ITEM_ROWS, drawing_xml="<xdr:wsDr><not-closed>")
    body = _upload(client, content, commit=True).json()
    assert body["committed"] is True
    assert any("could not be read" in n for n in body["notes"])
    assert _item(factory, "STOOL-1") is not None


def test_a_failed_import_leaves_no_picture_files_behind(factory, tmp_path, monkeypatch):
    """All-or-nothing reaches the pictures: if the items roll back, the files
    already written for them go too."""
    import pytest

    from app.services.imports import (
        ImportPlan, Picture, PlannedItem, PlannedProject, commit_import,
    )

    storage = _use_tmp_storage(tmp_path, monkeypatch)
    factory.item("TAKEN-1", 5)  # created by someone else between preview and commit
    factory.db.commit()

    plan = ImportPlan()
    plan.projects["PRJ"] = PlannedProject(code="PRJ", name="", client=None, exists=True)
    for row, code in ((2, "FRESH-1"), (3, "TAKEN-1")):
        plan.items.append(PlannedItem(
            row=row, project_code="PRJ", code=code, description="", qty=1, sample_qty=1,
            revision="A", picture=Picture(data=PNG, content_type="image/png"),
        ))

    from app.services.items import ItemSetupError

    with pytest.raises(ItemSetupError):
        commit_import(factory.db, plan, factory.user)
    factory.db.rollback()
    assert storage.list("images/") == []
    assert _item(factory, "FRESH-1") is None
