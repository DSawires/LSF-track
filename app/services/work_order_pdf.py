"""A work order on paper: one table, a row per item, the company's mark on top.

Pure rendering. It reads the saved work order and the pictures its lines point
at, and returns the bytes of an A4 PDF. The optional columns (picture,
description, dimensions, area/phase) are printed or not as the work order says,
and the description column takes up whatever width the others leave.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy.orm import Session

from app.models import Project, User, WorkOrder
from app.services.images import image_key
from app.services.work_orders import label
from app.storage import get_storage

log = logging.getLogger(__name__)

_LOGO = Path(__file__).resolve().parent.parent.parent / "static" / "brand" / "logo-print.png"

_GOLD = colors.HexColor("#A88846")   # the mark's own colour
_INK = colors.HexColor("#1B1F24")
_MUTED = colors.HexColor("#5B6470")
_RULE = colors.HexColor("#C9CED4")
_BAND = colors.HexColor("#F3F1EC")

_MARGIN = 14 * mm
_HEADER = 30 * mm   # room kept at the top of every page for the mark and title
_FOOTER = 8 * mm

_BASE = ParagraphStyle("base", fontName="Helvetica", fontSize=8.5, leading=11, textColor=_INK)
_SMALL = ParagraphStyle("small", parent=_BASE, fontSize=7.5, leading=9.5, textColor=_MUTED)
_HEAD = ParagraphStyle("head", parent=_BASE, fontName="Helvetica-Bold", fontSize=7.5, textColor=colors.white)
_CODE = ParagraphStyle("code", parent=_BASE, fontName="Helvetica-Bold")
_NUM = ParagraphStyle("num", parent=_BASE, alignment=TA_RIGHT, fontName="Helvetica-Bold", fontSize=9.5)
_CENTER = ParagraphStyle("center", parent=_BASE, alignment=TA_CENTER)
_LABEL = ParagraphStyle("label", parent=_SMALL, fontName="Helvetica-Bold", fontSize=7, leading=9)


def _text(value: str) -> str:
    """Safe for a Paragraph, and for the built-in fonts.

    The standard PDF fonts cover Western European text only. Anything outside
    it is replaced with '?' rather than left to break the page or vanish
    silently -- a visibly wrong character gets fixed; a missing one does not.
    """
    cleaned = (value or "").encode("cp1252", errors="replace").decode("cp1252")
    return escape(cleaned).replace("\n", "<br/>")


def _picture(image_id, box_w: float, box_h: float):
    """The line's picture, scaled into its cell -- or nothing, if it cannot be
    read. A missing or unreadable picture must never stop a work order."""
    try:
        from PIL import Image as PILImage

        data = get_storage().read(image_key(image_id))
        if not data:
            return ""
        with PILImage.open(io.BytesIO(data)) as source:
            source.load()
            picture = source.convert("RGBA")
        flat = PILImage.new("RGB", picture.size, "white")
        flat.paste(picture, mask=picture.getchannel("A"))
        flat.thumbnail((480, 480))       # plenty for a 26 mm cell; keeps the PDF small
        packed = io.BytesIO()
        flat.save(packed, "JPEG", quality=85)
        packed.seek(0)
        scale = min(box_w / flat.width, box_h / flat.height)
        return Image(packed, width=flat.width * scale, height=flat.height * scale)
    except Exception:  # an unreadable format (HEIC), a truncated file, storage down
        log.warning("work order picture %s could not be drawn", image_id, exc_info=True)
        return ""


class _Numbered(canvas.Canvas):
    """Draws the header and footer on every page once the page count is known,
    so the footer can say "Page 2 of 3"."""

    def __init__(self, *args, chrome=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._chrome = chrome
        self._pages: list[dict] = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._chrome(self, self._pageNumber, total)
            super().showPage()
        super().save()


def render(db: Session, work_order: WorkOrder, compress: bool = True) -> bytes:
    project = db.get(Project, work_order.project_id)
    issuer = db.get(User, work_order.issued_by_user_id)
    number = label(work_order)
    width, height = A4
    usable = width - 2 * _MARGIN

    def chrome(page: canvas.Canvas, page_no: int, total: int) -> None:
        top = height - _MARGIN
        if _LOGO.is_file():
            mark_h = 24 * mm
            reader = ImageReader(str(_LOGO))
            mark_w = mark_h * reader.getSize()[0] / reader.getSize()[1]
            page.drawImage(reader, _MARGIN, top - mark_h, width=mark_w, height=mark_h, mask="auto")
        page.setFillColor(_INK)
        page.setFont("Helvetica-Bold", 20)
        page.drawRightString(width - _MARGIN, top - 9 * mm, "WORK ORDER")
        page.setFont("Helvetica-Bold", 12)
        page.setFillColor(_GOLD)
        page.drawRightString(width - _MARGIN, top - 16 * mm, f"{number}   Rev {work_order.revision}")
        page.setFont("Helvetica", 8)
        page.setFillColor(_MUTED)
        page.drawRightString(width - _MARGIN, top - 21 * mm, "Release to production")
        page.setStrokeColor(_GOLD)
        page.setLineWidth(1.2)
        page.line(_MARGIN, top - 26 * mm, width - _MARGIN, top - 26 * mm)

        page.setStrokeColor(_RULE)
        page.setLineWidth(0.5)
        page.line(_MARGIN, _MARGIN + 5 * mm, width - _MARGIN, _MARGIN + 5 * mm)
        page.setFont("Helvetica", 7.5)
        page.setFillColor(_MUTED)
        page.drawString(
            _MARGIN, _MARGIN + 1.5 * mm,
            _text_plain(f"{number} Rev {work_order.revision}  ·  {project.code if project else ''}"),
        )
        page.drawRightString(width - _MARGIN, _MARGIN + 1.5 * mm, f"Page {page_no} of {total}")

    story = []

    # -- who, what, when -------------------------------------------------------
    def fact(caption: str, value: str):
        return [Paragraph(caption.upper(), _LABEL), Paragraph(_text(value) or "&nbsp;", _BASE)]

    facts = Table(
        [[
            fact("Project", f"{project.code} — {project.name}" if project else ""),
            fact("Client", (project.client or "") if project else ""),
            fact("Issued", work_order.issued_at.strftime("%d %b %Y")),
            fact("Issued by", issuer.display_name if issuer else ""),
        ]],
        colWidths=[usable * 0.34, usable * 0.30, usable * 0.14, usable * 0.22],
    )
    facts.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(facts)
    if work_order.title:
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(_text(work_order.title), ParagraphStyle(
            "title", parent=_BASE, fontName="Helvetica-Bold", fontSize=11.5, leading=14)))
    story.append(Spacer(1, 4 * mm))

    # -- the items -------------------------------------------------------------
    pic_w, pic_h = 23 * mm, 18 * mm
    columns = [("#", 7 * mm)]
    if work_order.show_picture:
        columns.append(("Picture", pic_w + 3 * mm))
    columns.append(("Code", 23 * mm))
    if work_order.show_description:
        columns.append(("Description", None))
    if work_order.show_dimensions:
        columns.append(("Dimensions", None if not work_order.show_description else 33 * mm))
    if work_order.show_location:
        columns.append(("Area / Phase", 23 * mm))
    columns += [("Rev", 9 * mm), ("Qty", 13 * mm)]
    fixed = sum(w for _name, w in columns if w is not None)
    flexible = [name for name, w in columns if w is None]
    if not flexible:
        # Nothing to stretch: let the code column take the slack.
        columns = [(name, (w + usable - fixed) if name == "Code" else w) for name, w in columns]
    else:
        share = (usable - fixed) / len(flexible)
        columns = [(name, share if w is None else w) for name, w in columns]
    names = [name for name, _w in columns]

    rows = [[Paragraph(name.upper(), _HEAD) for name in names]]
    for index, line in enumerate(work_order.lines, start=1):
        cells = {
            "#": Paragraph(str(index), _SMALL),
            "Picture": _picture(line.image_id, pic_w, pic_h) if line.image_id else "",
            "Code": Paragraph(_text(line.code), _CODE),
            "Description": Paragraph(_text(line.description), _BASE),
            "Dimensions": Paragraph(_text(line.dimensions), _BASE),
            "Area / Phase": Paragraph(
                "<br/>".join(_text(part) for part in (line.area, line.phase) if part), _SMALL
            ),
            "Rev": Paragraph(_text(line.drawing_revision), _CENTER),
            "Qty": Paragraph(str(line.qty), _NUM),
        }
        rows.append([cells[name] for name in names])

    table = Table(rows, colWidths=[w for _name, w in columns], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, _RULE),
        ("BOX", (0, 0), (-1, -1), 0.5, _RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _BAND]),
    ]
    if "Picture" in names:
        style.append(("ALIGN", (names.index("Picture"), 1), (names.index("Picture"), -1), "CENTER"))
    table.setStyle(TableStyle(style))
    story.append(table)

    # -- totals, notes, signatures: kept together, never split from each other --
    pieces = sum(line.qty for line in work_order.lines)
    count = len(work_order.lines)
    closing = [
        Spacer(1, 3 * mm),
        Paragraph(
            f"<b>{count}</b> item{'' if count == 1 else 's'}  ·  <b>{pieces}</b> piece{'' if pieces == 1 else 's'}",
            ParagraphStyle("total", parent=_BASE, alignment=TA_RIGHT, fontSize=9.5),
        ),
    ]
    if work_order.notes:
        closing += [
            Spacer(1, 4 * mm),
            Paragraph("NOTES", _LABEL),
            Spacer(1, 1 * mm),
            Paragraph(_text(work_order.notes), _BASE),
        ]
    # Three lines to sign on, with air between them.
    gap = 8 * mm
    sign_w = (usable - 2 * gap) / 3
    captions = ("Issued by (technical office)", "Received by (production)", "Date received")
    signatures = Table(
        [[Paragraph(captions[0], _SMALL), "", Paragraph(captions[1], _SMALL), "", Paragraph(captions[2], _SMALL)]],
        colWidths=[sign_w, gap, sign_w, gap, sign_w],
        rowHeights=[6 * mm],
    )
    signatures.setStyle(TableStyle(
        [("LINEABOVE", (column, 0), (column, 0), 0.6, _INK) for column in (0, 2, 4)]
        + [
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
        ]
    ))
    closing += [Spacer(1, 16 * mm), signatures]
    story.append(KeepTogether(closing))

    out = io.BytesIO()
    document = SimpleDocTemplate(
        out,
        pagesize=A4,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN + _HEADER,
        bottomMargin=_MARGIN + _FOOTER,
        title=f"{number} Rev {work_order.revision}",
        author="Life Style Track",
        subject=f"Work order for {project.code}" if project else "Work order",
        pageCompression=1 if compress else 0,
    )
    document.build(
        story,
        canvasmaker=lambda *args, **kwargs: _Numbered(*args, chrome=chrome, **kwargs),
    )
    return out.getvalue()


def _text_plain(value: str) -> str:
    """For text drawn straight onto the canvas (no markup to escape)."""
    return (value or "").encode("cp1252", errors="replace").decode("cp1252")
