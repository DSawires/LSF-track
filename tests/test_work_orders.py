"""Work orders: numbered documents releasing a project's items to production.

A work order is paper, not state. These tests hold it to that: it keeps what
was written on it whatever happens to the items afterwards, it survives its
items and pictures being deleted, and issuing one changes nothing a report
reads.
"""

from __future__ import annotations

import io
import uuid

import pytest
import sqlalchemy as sa

from app.models import Area, Item, ItemImage, Phase, Project, WorkOrder, WorkOrderLine
from app.services import reports
from app.services.work_order_pdf import render

from tests.conftest import hours_ago


def _png(colour=(170, 136, 70), size=(120, 80)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def _use_tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("LSF_UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.config import get_settings
    from app.storage import get_storage

    get_settings.cache_clear()
    get_storage.cache_clear()


def _upload(client, item, kind="icon", data=None):
    made = client.post(
        f"/api/items/{item.id}/images",
        files={"file": ("p.png", data or _png(), "image/png")},
        data={"kind": kind},
    )
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _line(item, **fields):
    return {"item_id": str(item.id), "qty": item.total_qty, **fields}


def _issue(client, factory, lines, **fields):
    return client.post("/api/work-orders", json={
        "project_id": str(factory.project.id), "lines": lines, **fields,
    })


@pytest.fixture()
def job(factory):
    bed = factory.item("LF-01", 216, area="Guest rooms", phase="Phase 1")
    bed.description, bed.dimensions = "King bed base", "2.0 x 1.8 m"
    bench = factory.item("LF-03", 216, area="Guest rooms", phase="Phase 1")
    bench.description = "King room bench"
    table = factory.item("SP-03", 17)   # filed under nothing
    factory.db.commit()
    return factory, bed, bench, table


# -- issuing ------------------------------------------------------------------


def test_a_work_order_is_numbered_and_copies_what_it_prints(client, job):
    factory, bed, bench, table = job
    made = _issue(client, factory, [
        _line(bed, description="King bed base, black lacquer legs", dimensions="2000 x 1800 mm", qty=100),
        _line(table),
    ], title="  First release ", notes="Finishes per approved sample.")
    assert made.status_code == 201, made.text
    body = made.json()
    assert (body["label"], body["number"], body["revision"]) == ("WO-0001", 1, 1)
    assert (body["title"], body["issued_by"], body["line_count"], body["total_qty"]) == (
        "First release", "Test Engineer", 2, 117,
    )
    first, second = body["lines"]
    # What the office typed for this document, and a copy of everything else.
    assert (first["code"], first["description"], first["dimensions"], first["qty"]) == (
        "LF-01", "King bed base, black lacquer legs", "2000 x 1800 mm", 100,
    )
    assert (first["area"], first["phase"], first["drawing_revision"]) == ("Guest rooms", "Phase 1", "A")
    assert (second["code"], second["area"], second["phase"], second["qty"]) == ("SP-03", "", "", 17)

    # Numbers run on, and the list is newest first, without the lines.
    assert _issue(client, factory, [_line(bench)]).json()["label"] == "WO-0002"
    listed = client.get(f"/api/work-orders?project_id={factory.project.id}").json()["work_orders"]
    assert [(w["label"], w["line_count"]) for w in listed] == [("WO-0002", 1), ("WO-0001", 2)]
    assert "lines" not in listed[0]
    assert client.get(f"/api/work-orders/{body['id']}").json()["lines"][0]["code"] == "LF-01"


def test_a_work_order_is_paper_not_state(client, job):
    """Issuing one changes nothing a report reads, and nothing about what can
    be logged: an item was loggable before it and is loggable after."""
    factory, bed, _bench, _table = job
    before = (reports.wip_report(factory.db), client.get("/api/reference").json()["items_token"])
    assert _issue(client, factory, [_line(bed)]).status_code == 201
    factory.db.expire_all()
    after = reports.wip_report(factory.db)
    assert {k: after[k] for k in ("stages", "unstarted", "totals")} == {
        k: before[0][k] for k in ("stages", "unstarted", "totals")
    }
    assert client.get("/api/reference").json()["items_token"] == before[1]
    posted = client.post("/api/events", json={
        "id": str(uuid.uuid4()), "item_id": str(bed.id), "stage_id": str(factory.stage("qc").id),
        "from_unstarted": True, "event_type_id": str(factory.event_type("move").id), "qty": 5,
        "occurred_at": hours_ago(1).isoformat(),
    })
    assert posted.status_code == 201


def test_later_edits_to_an_item_do_not_rewrite_an_issued_work_order(client, job):
    factory, bed, _bench, _table = job
    made = _issue(client, factory, [_line(bed, description="As issued", dimensions="as issued")]).json()
    client.patch(f"/api/items/{bed.id}", json={
        "code": "LF-01X", "description": "Changed later", "dimensions": "changed", "area_id": None,
    })
    client.post(f"/api/items/{bed.id}/revision", json={"drawing_revision": "B"})

    line = client.get(f"/api/work-orders/{made['id']}").json()["lines"][0]
    assert (line["code"], line["description"], line["dimensions"], line["area"], line["drawing_revision"]) == (
        "LF-01", "As issued", "as issued", "Guest rooms", "A",
    )


def test_editing_re_issues_it_as_the_next_revision(client, job):
    factory, bed, bench, _table = job
    made = _issue(client, factory, [_line(bed)], title="Release").json()
    client.patch(f"/api/items/{bed.id}", json={"code": "LF-01X"})

    again = client.put(f"/api/work-orders/{made['id']}", json={
        "project_id": str(factory.project.id), "title": "Release, corrected", "show_dimensions": False,
        "lines": [_line(bench, qty=10), _line(bed, qty=50)],
    })
    assert again.status_code == 200, again.text
    body = again.json()
    assert (body["label"], body["revision"], body["title"], body["show_dimensions"]) == (
        "WO-0001", 2, "Release, corrected", False,
    )
    # Re-issued from the items as they are now, in the order sent.
    assert [(line["code"], line["qty"]) for line in body["lines"]] == [("LF-03", 10), ("LF-01X", 50)]
    assert factory.db.scalar(sa.select(sa.func.count()).select_from(WorkOrderLine)) == 2
    assert client.get("/api/work-orders").json()["work_orders"][0]["revision"] == 2


def test_what_a_work_order_refuses(client, job, factory):
    factory, bed, bench, _table = job
    other = Project(code="OTHER", name="Another job")
    factory.db.add(other)
    factory.db.flush()
    theirs = Item(code="X-1", project_id=other.id, description="", total_qty=3, sample_qty=0, drawing_revision="A")
    factory.db.add(theirs)
    factory.db.commit()

    assert _issue(client, factory, []).status_code == 422                                # nothing on it
    assert _issue(client, factory, [_line(bed, qty=0)]).status_code == 422               # nothing released
    assert _issue(client, factory, [{"item_id": str(uuid.uuid4()), "qty": 1}]).status_code == 422
    assert _issue(client, factory, [{"qty": 1}]).status_code == 422                      # a line with no item
    twice = _issue(client, factory, [_line(bed), _line(bed)])
    assert twice.status_code == 409 and "twice" in twice.json()["detail"]
    mixed = _issue(client, factory, [_line(bed), _line(theirs)])
    assert mixed.status_code == 409 and "another project" in mixed.json()["detail"]
    # Not a work order for a job that is closed.
    client.post("/api/items/assign", json={"item_ids": [str(bed.id)], "area_id": None})
    for item in (bed, bench, _table):
        client.delete(f"/api/items/{item.id}")
    assert client.delete(f"/api/projects/{factory.project.id}").json()["archived"] is True
    assert client.post("/api/work-orders", json={
        "project_id": str(factory.project.id), "lines": [_line(bed)],
    }).status_code == 409
    assert client.get("/api/work-orders").json()["work_orders"] == []


def test_any_engineer_issues_one_and_only_an_admin_deletes_one(client, job):
    from fastapi.testclient import TestClient

    from app.main import app

    factory, bed, bench, _table = job
    client.post("/api/users", json={
        "username": "floor", "display_name": "Floor Engineer", "password": "a-long-password",
    })
    with TestClient(app) as floor:
        floor.post("/api/auth/login", json={"username": "floor", "password": "a-long-password"})
        made = _issue(floor, factory, [_line(bed)])
        assert made.status_code == 201 and made.json()["issued_by"] == "Floor Engineer"
        wo = made.json()["id"]
        assert floor.get(f"/api/work-orders/{wo}/pdf").status_code == 200
        assert floor.delete(f"/api/work-orders/{wo}").status_code == 403

    assert client.delete(f"/api/work-orders/{wo}").json() == {"deleted": True}
    assert client.get(f"/api/work-orders/{wo}").status_code == 404
    assert factory.db.scalar(sa.select(sa.func.count()).select_from(WorkOrderLine)) == 0
    factory.db.expire_all()
    assert factory.db.get(Item, bed.id) is not None      # the item is untouched


# -- pictures -----------------------------------------------------------------


def test_a_line_carries_one_of_its_items_pictures(client, job, tmp_path, monkeypatch):
    _use_tmp_storage(tmp_path, monkeypatch)
    factory, bed, bench, _table = job
    icon = _upload(client, bed)
    drawing = _upload(client, bed, kind="document")      # a picture for the work order only
    theirs = _upload(client, bench)

    # A document picture is not the item's icon.
    assert client.get(f"/api/items/{bed.id}").json()["icon_url"] == f"/api/images/{icon}"

    made = _issue(client, factory, [_line(bed, image_id=drawing), _line(bench)]).json()
    assert [line["image_url"] for line in made["lines"]] == [f"/api/images/{drawing}", None]
    wrong = _issue(client, factory, [_line(bed, image_id=theirs)])
    assert wrong.status_code == 422 and "not one of LF-01's" in wrong.json()["detail"]

    # Deleting the picture leaves the line, without it.
    assert client.delete(f"/api/images/{drawing}").json()["deleted"] is True
    assert client.get(f"/api/work-orders/{made['id']}").json()["lines"][0]["image_id"] is None


def test_a_work_order_outlives_an_item_deleted_for_good(client, job, tmp_path, monkeypatch):
    _use_tmp_storage(tmp_path, monkeypatch)
    factory, bed, bench, _table = job
    picture = _upload(client, bed)
    made = _issue(client, factory, [
        _line(bed, image_id=picture, description="King bed base"), _line(bench),
    ]).json()

    assert client.delete(f"/api/items/{bed.id}?purge=true").json()["deleted"] is True
    kept = client.get(f"/api/work-orders/{made['id']}").json()["lines"][0]
    assert (kept["code"], kept["description"], kept["item_id"], kept["image_id"]) == (
        "LF-01", "King bed base", None, None,
    )
    assert client.get(f"/api/work-orders/{made['id']}/pdf").status_code == 200

    # It can still be re-issued: the orphaned line is sent back by its own id.
    again = client.put(f"/api/work-orders/{made['id']}", json={
        "project_id": str(factory.project.id),
        "lines": [{"id": kept["id"], "qty": 12, "description": "King bed base (deleted item)"},
                  _line(bench)],
    })
    assert again.status_code == 200, again.text
    assert [(line["code"], line["qty"]) for line in again.json()["lines"]] == [("LF-01", 12), ("LF-03", 216)]
    # But a line cannot be conjured up with no item behind it.
    fresh = client.put(f"/api/work-orders/{made['id']}", json={
        "project_id": str(factory.project.id), "lines": [{"id": str(uuid.uuid4()), "qty": 1}],
    })
    assert fresh.status_code == 422


# -- the PDF ------------------------------------------------------------------


def _pdf_text(factory, work_order_id) -> bytes:
    factory.db.expire_all()
    work_order = factory.db.get(WorkOrder, uuid.UUID(work_order_id))
    return render(factory.db, work_order, compress=False)


def test_the_pdf_downloads_under_its_number_and_revision(client, job):
    factory, bed, _bench, _table = job
    made = _issue(client, factory, [_line(bed)]).json()
    response = client.get(made["pdf_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="WO-0001-rev1.pdf"'
    assert response.headers["cache-control"] == "no-store"
    assert response.content[:5] == b"%PDF-" and len(response.content) > 3000
    assert client.get(f"/api/work-orders/{uuid.uuid4()}/pdf").status_code == 404


def test_the_pdf_prints_the_logo_the_lines_and_the_columns_asked_for(client, job, tmp_path, monkeypatch):
    _use_tmp_storage(tmp_path, monkeypatch)
    factory, bed, bench, _table = job
    picture = _upload(client, bed)
    made = _issue(client, factory, [
        _line(bed, image_id=picture, description="King bed base", dimensions="2000 x 1800 mm", qty=42),
        _line(bench, description="Bench"),
    ], title="Guest rooms release", notes="Pack per room set.").json()

    pdf = _pdf_text(factory, made["id"])
    for text in (b"WORK ORDER", b"WO-0001", b"Rev 1", b"PRJ", b"Test Engineer", b"LF-01", b"LF-03",
                 b"2000 x 1800 mm", b"Guest rooms", b"Phase 1", b"DIMENSIONS", b"PICTURE", b"Page 1 of 1"):
        assert text in pdf, text
    # The mark on the page, and the one line picture: two images in the file.
    assert pdf.count(b"/Subtype /Image") >= 2

    # Columns can be left off; the others still print.
    hidden = client.put(f"/api/work-orders/{made['id']}", json={
        "project_id": str(factory.project.id), "show_picture": False, "show_dimensions": False,
        "show_location": False,
        "lines": [_line(bed, image_id=picture, dimensions="2000 x 1800 mm"), _line(bench)],
    }).json()
    pdf = _pdf_text(factory, hidden["id"])
    assert b"Rev 2" in pdf and b"DESCRIPTION" in pdf
    for gone in (b"DIMENSIONS", b"PICTURE", b"AREA / PHASE", b"2000 x 1800 mm"):
        assert gone not in pdf, gone


def test_a_bad_picture_or_odd_text_never_stops_a_work_order(client, job, tmp_path, monkeypatch):
    _use_tmp_storage(tmp_path, monkeypatch)
    factory, bed, bench, _table = job
    # Uploads are decoded now, so a broken picture can only be one already in
    # storage -- an original from before compression that never converted.
    broken = _upload(client, bed)
    vanished = _upload(client, bench)
    import io as _io

    from app.storage import get_storage

    get_storage().put(f"images/{broken}", _io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"not really a png" * 8))
    made = _issue(client, factory, [
        _line(bed, image_id=broken, description="طاولة جانبية <b>& co</b> → 600×450", dimensions="Ø 450"),
        _line(bench, image_id=vanished),
    ], notes="Line one\nLine two").json()
    # The file behind the second picture goes missing.
    from app.storage import get_storage

    get_storage().delete(f"images/{vanished}")

    pdf = _pdf_text(factory, made["id"])
    assert pdf[:5] == b"%PDF-"
    assert b"& co" in pdf            # markup characters are printed, not interpreted
    assert b"Line two" in pdf


def test_a_long_work_order_runs_over_pages_with_its_header_repeated(client, factory):
    items = [factory.item(f"LONG-{n:03d}", 5) for n in range(60)]
    factory.db.commit()
    made = _issue(client, factory, [_line(i, description=f"Item number {n}") for n, i in enumerate(items)]).json()
    pdf = _pdf_text(factory, made["id"])
    pages = pdf.count(b"WORK ORDER")                   # the page header, once a page
    assert pages >= 2
    # The table header repeats on every page the table reaches (the closing
    # signatures may take a last page of their own).
    assert 2 <= pdf.count(b"DESCRIPTION") <= pages
    assert f"Page {pages} of {pages}".encode() in pdf
    assert b"LONG-059" in pdf


# -- dimensions on the item ---------------------------------------------------


def test_an_item_carries_its_dimensions(client, factory):
    factory.db.commit()
    made = client.post("/api/items", json={
        "code": "DIM-1", "project_id": str(factory.project.id), "total_qty": 4,
        "dimensions": "  W 600 x D 450 x H 720 mm ",
    })
    assert made.status_code == 201 and made.json()["dimensions"] == "W 600 x D 450 x H 720 mm"
    before = client.get("/api/reference").json()["items_token"]
    edited = client.patch(f"/api/items/{made.json()['id']}", json={"dimensions": "W 650 mm"})
    assert edited.json()["dimensions"] == "W 650 mm"
    assert client.get("/api/reference").json()["items_token"] != before   # the phones hear of it
    assert client.patch(f"/api/items/{made.json()['id']}", json={"dimensions": ""}).json()["dimensions"] == ""

    text = "project,item,qty,dimensions\nPRJ,DIM-2,3,D450 x H600 mm\nPRJ,DIM-3,3,\n"
    body = client.post(
        "/api/import", files={"file": ("j.csv", text.encode(), "application/octet-stream")},
        data={"commit": "true"},
    ).json()
    assert [(i["code"], i["dimensions"]) for i in body["items"]] == [("DIM-2", "D450 x H600 mm"), ("DIM-3", "")]
    listed = {i["code"]: i["dimensions"] for i in client.get("/api/items?q=DIM-").json()["items"]}
    assert listed == {"DIM-1": "", "DIM-2": "D450 x H600 mm", "DIM-3": ""}


# -- every field on a line is the work order's own ------------------------------


def test_every_field_on_a_line_can_be_written_for_the_document(client, job):
    factory, bed, bench, table = job
    made = _issue(client, factory, [
        _line(bed, code="LF-01/A", area="Suites", phase="Mock-up", drawing_revision="C",
              description="King bed base", dimensions="2000 x 1800 mm", qty=4),
        # Blanked on purpose: this line prints no area and no phase.
        _line(bench, area="", phase=""),
        # Left out: copied from the item as it stands. A blank code is no code.
        _line(table, code="   "),
    ])
    assert made.status_code == 201, made.text
    first, second, third = made.json()["lines"]
    assert (first["code"], first["area"], first["phase"], first["drawing_revision"]) == (
        "LF-01/A", "Suites", "Mock-up", "C",
    )
    assert (second["code"], second["area"], second["phase"], second["drawing_revision"]) == ("LF-03", "", "", "A")
    assert (third["code"], third["drawing_revision"]) == ("SP-03", "A")

    # The item itself is untouched: it is the paper that says something else.
    factory.db.expire_all()
    assert (factory.db.get(Item, bed.id).code, factory.db.get(Item, bed.id).drawing_revision) == ("LF-01", "A")
    # And a line is still found by its item, whatever code it prints.
    again = _issue(client, factory, [_line(bed, code="X"), _line(bed, code="Y")])
    assert again.status_code == 409 and "twice" in again.json()["detail"]

    # What was written survives a re-issue that sends it back, as the editor does.
    kept = client.put(f"/api/work-orders/{made.json()['id']}", json={
        "project_id": str(factory.project.id),
        "lines": [{**_line(bed), "id": first["id"], "code": first["code"], "area": first["area"],
                   "phase": first["phase"], "drawing_revision": first["drawing_revision"]}],
    }).json()
    assert (kept["revision"], kept["lines"][0]["code"], kept["lines"][0]["area"]) == (2, "LF-01/A", "Suites")

    pdf = _pdf_text(factory, kept["id"])
    assert b"LF-01/A" in pdf and b"Suites" in pdf and b"Mock-up" in pdf


# -- Arabic -------------------------------------------------------------------


def test_a_work_order_keeps_its_arabic_beside_the_english(client, job):
    factory, bed, _bench, _table = job
    made = _issue(client, factory, [
        _line(bed, description="King bed base", description_ar=" قاعدة سرير كينج "),
    ], title="First release", title_ar="الإصدار الأول", notes_ar="حسب العينة المعتمدة", language="both")
    assert made.status_code == 201, made.text
    body = made.json()
    assert (body["language"], body["title"], body["title_ar"], body["notes_ar"]) == (
        "both", "First release", "الإصدار الأول", "حسب العينة المعتمدة",
    )
    assert body["lines"][0]["description_ar"] == "قاعدة سرير كينج"
    assert body["lines"][0]["description"] == "King bed base"

    # English unless asked otherwise, and only the three ways the sheet can print.
    assert _issue(client, factory, [_line(bed)]).json()["language"] == "en"
    assert _issue(client, factory, [_line(bed)], language="fr").status_code == 422


def test_arabic_is_shaped_ordered_and_wrapped_before_it_is_drawn():
    from app.services import arabic

    assert arabic.has_arabic("خشب") and not arabic.has_arabic("Oak veneer 18 mm")
    # Shaped: the letters are joined forms, not the isolated ones that were typed.
    shown = arabic.visual("خشب")
    assert shown != "خشب" and all(0xFE70 <= ord(char) <= 0xFEFF for char in shown)
    # Ordered: the sentence starts at the right, and the Latin inside it still
    # reads left to right.
    mixed = arabic.visual("خشب MDF 18 مم")
    assert "MDF 18" in mixed and mixed.index("MDF") > 0 and not mixed.startswith("MDF")
    # An English line that merely mentions an Arabic word stays left to right.
    assert arabic.visual("Oak veneer خشب").startswith("Oak veneer")
    assert "?" not in arabic.visual("الله لا إله إلا")      # every ligature it makes, the font has

    text = "طاولة جانبية بسطح رخام وأرجل معدنية مطلية باللون الذهبي مع درج واحد بمجرى هادئ الغلق"
    lines = arabic.wrap(text, arabic.REGULAR, 8.5, 120)
    assert len(lines) > 2
    assert all(arabic.width_of(line, arabic.REGULAR, 8.5) <= 120 for line in lines)
    # Nothing lost in the wrapping: the same letters, spaces aside.
    assert sorted("".join(lines).replace(" ", "")) == sorted(arabic.visual(text).replace(" ", ""))
    # Line breaks someone typed are kept.
    assert len(arabic.wrap("سطر أول\nسطر ثان", arabic.REGULAR, 8.5, None)) == 2


@pytest.mark.parametrize("language", ["ar", "both"])
def test_the_pdf_prints_in_arabic(client, job, language):
    factory, bed, bench, _table = job
    made = _issue(client, factory, [
        _line(bed, description="King bed base, oak veneer", description_ar="قاعدة سرير كينج بقشرة بلوط",
              dimensions="2000 x 1800 mm"),
        _line(bench, description="Bench"),         # no Arabic written: the English stands in
    ], title="First release", title_ar="الإصدار الأول", notes="Pack per room set.",
        notes_ar="التعبئة حسب طقم كل غرفة.", language=language).json()

    pdf = _pdf_text(factory, made["id"])
    assert pdf[:5] == b"%PDF-"
    assert b"IBMPlexSansArabic" in pdf                 # the Arabic face is embedded in the file
    # Codes, numbers and dimensions are printed as written in every language.
    for text in (b"WO-0001", b"LF-01", b"LF-03", b"2000 x 1800 mm", b"Bench"):
        assert text in pdf, text
    if language == "ar":
        # An Arabic sheet has Arabic captions, and the English wording gives way.
        for gone in (b"WORK ORDER", b"DESCRIPTION", b"Page 1 of 1", b"King bed base", b"First release"):
            assert gone not in pdf, gone
    else:
        for text in (b"WORK ORDER", b"King bed base", b"First release", b"Pack per room set."):
            assert text in pdf, text

    # An English sheet is what it was: no Arabic face, unless Arabic was typed.
    plain = _issue(client, factory, [_line(bed, description_ar="قاعدة سرير")], title_ar="عنوان").json()
    assert b"IBMPlexSansArabic" not in _pdf_text(factory, plain["id"])
    typed = _issue(client, factory, [_line(bed, area="غرف النزلاء")]).json()
    assert b"IBMPlexSansArabic" in _pdf_text(factory, typed["id"])


# -- translation --------------------------------------------------------------


class _FakeClaude:
    """Stands in for the Anthropic client: answers each segment with a marked
    copy, and remembers what it was asked."""

    def __init__(self, answer=None, stop_reason="end_turn", raises=None):
        self.requests = []
        self._answer, self._stop_reason, self._raises = answer, stop_reason, raises
        self.beta = self
        self.messages = self

    def stream(self, **kwargs):
        import json
        from types import SimpleNamespace

        self.requests.append(kwargs)
        if self._raises is not None:
            raise self._raises
        segments = json.loads(kwargs["messages"][0]["content"])
        answer = self._answer or (lambda rows: [{"id": r["id"], "text": f"ع[{r['text']}]"} for r in rows])
        message = SimpleNamespace(
            stop_reason=self._stop_reason,
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text=json.dumps({"translations": answer(segments)})),
            ],
            _request_id="req_test",
        )

        class _Stream:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return message

        return _Stream()


def _with_claude(monkeypatch, fake):
    from app.services import translate

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(translate, "_client", lambda: fake)
    return fake


def _translate(client, texts):
    return client.post("/api/work-orders/translate", json={"texts": texts})


def test_translation_answers_in_order_and_sends_only_what_has_text(client, monkeypatch):
    fake = _with_claude(monkeypatch, _FakeClaude())
    answered = _translate(client, ["King bed base", "", "Oak veneer on MDF 18 mm", "   "])
    assert answered.status_code == 200, answered.text
    assert answered.json()["translations"] == ["ع[King bed base]", "", "ع[Oak veneer on MDF 18 mm]", ""]

    (request,) = fake.requests
    import json

    assert [row["text"] for row in json.loads(request["messages"][0]["content"])] == [
        "King bed base", "Oak veneer on MDF 18 mm",
    ]
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "Arabic" in request["system"]

    # Nothing but blanks: nobody is asked anything.
    assert _translate(client, ["", " "]).json()["translations"] == ["", ""]
    assert len(fake.requests) == 1


def test_a_long_work_order_is_translated_in_several_requests(client, monkeypatch):
    fake = _with_claude(monkeypatch, _FakeClaude())
    texts = [f"Item {n}: " + "oak veneer panel " * 20 for n in range(90)]
    answered = _translate(client, texts).json()["translations"]
    assert answered == [f"ع[{text}]" for text in texts]
    assert len(fake.requests) > 2            # split up, and put back together in order


def test_translation_says_why_when_it_cannot(client, monkeypatch):
    import anthropic
    import httpx2

    from app.services import translate

    # No key: the feature is off, and says how to turn it on.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    off = _translate(client, ["King bed base"])
    assert off.status_code == 503 and "ANTHROPIC_API_KEY" in off.json()["detail"]

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    def status_error(kind, code):
        return kind("nope", response=httpx2.Response(code, request=request), body=None)

    cases = [
        (anthropic.AuthenticationError, 401, 503, "API key"),
        (anthropic.RateLimitError, 429, 503, "busy"),
        (anthropic.InternalServerError, 500, 502, "trouble"),
        (anthropic.BadRequestError, 400, 502, "refused"),
    ]
    for kind, code, expected, word in cases:
        _with_claude(monkeypatch, _FakeClaude(raises=status_error(kind, code)))
        answer = _translate(client, ["King bed base"])
        assert (answer.status_code, word in answer.json()["detail"]) == (expected, True), answer.text

    _with_claude(monkeypatch, _FakeClaude(raises=anthropic.APIConnectionError(request=request)))
    assert "reach" in _translate(client, ["King bed base"]).json()["detail"]

    # An answer that is declined, cut short or missing a segment is an error,
    # never a half-filled work order.
    _with_claude(monkeypatch, _FakeClaude(stop_reason="refusal"))
    assert _translate(client, ["King bed base"]).status_code == 502
    _with_claude(monkeypatch, _FakeClaude(stop_reason="max_tokens"))
    assert "cut short" in _translate(client, ["King bed base"]).json()["detail"]
    _with_claude(monkeypatch, _FakeClaude(answer=lambda rows: rows[:-1]))
    assert "incomplete" in _translate(client, ["one", "two"]).json()["detail"]

    # Too much, or nothing at all, is refused before anyone is asked.
    assert _translate(client, []).status_code == 422
    assert _translate(client, ["x" * 4001]).status_code == 422
    assert translate.is_configured()


def test_translation_needs_a_session(client, monkeypatch):
    fake = _with_claude(monkeypatch, _FakeClaude())
    client.post("/api/auth/logout")
    assert _translate(client, ["King bed base"]).status_code == 401
    assert fake.requests == []
