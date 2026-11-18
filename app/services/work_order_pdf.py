"""A work order on paper: one table, a row per item, the company's mark on top.

Pure rendering. It reads the saved work order and the pictures its lines point
at, and returns the bytes of an A4 PDF. The optional columns (picture,
description, dimensions, area/phase) are printed or not as the work order says,
and the description column takes up whatever width the others leave.

It prints in one of three ways, as the work order's `language` says:

- `en`   -- English, left to right.
- `ar`   -- Arabic: Arabic captions, the Arabic wording where there is some
            (the English where there is not), and the whole sheet mirrored so
            it reads right to left -- first column on the right.
- `both` -- the English sheet with the Arabic under it: captions in both, and
            the Arabic title, notes and descriptions beneath the English ones.

Arabic typed into any field prints as Arabic in every mode. How it gets onto
the page is app/services/arabic.py.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
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
from app.services import arabic
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


# Every caption the sheet prints, in English and in Arabic.
_CAPTIONS = {
    "work_order": ("WORK ORDER", "أمر تشغيل"),
    "release": ("Release to production", "إصدار للإنتاج"),
    "rev": ("Rev", "مراجعة"),
    "page": ("Page {page} of {total}", "صفحة {page} من {total}"),
    "project": ("Project", "المشروع"),
    "client": ("Client", "العميل"),
    "issued": ("Issued", "تاريخ الإصدار"),
    "issued_by": ("Issued by", "أصدره"),
    "number": ("#", "م"),
    "picture": ("Picture", "الصورة"),
    "code": ("Code", "الكود"),
    "description": ("Description", "الوصف"),
    "dimensions": ("Dimensions", "الأبعاد"),
    "location": ("Area / Phase", "المنطقة / المرحلة"),
    "qty": ("Qty", "الكمية"),
    "notes": ("Notes", "ملاحظات"),
    # Label first, number after: Arabic nouns change form with the number
    # counted, and this wording is right for every count.
    "totals": ("", "عدد البنود: {count}   ·   عدد القطع: {pieces}"),
    "sign_issued": ("Issued by (technical office)", "أصدره (المكتب الفني)"),
    "sign_received": ("Received by (production)", "استلمه (الإنتاج)"),
    "sign_date": ("Date received", "تاريخ الاستلام"),
}

_LANGUAGES = {"en", "ar", "both"}


def _caption(key: str, language: str, stacked: bool = False) -> str:
    english, in_arabic = _CAPTIONS[key]
    if language == "ar":
        return in_arabic
    if language == "both":
        return f"{english}\n{in_arabic}" if stacked else f"{english}  ·  {in_arabic}"
    return english


def _text(value: str) -> str:
    """Safe for a Paragraph, and for the built-in fonts.

    The standard PDF fonts cover Western European text only. Anything outside
    it is replaced with '?' rather than left to break the page or vanish
    silently -- a visibly wrong character gets fixed; a missing one does not.
    Arabic never comes through here: `_para` sends it to the Arabic font.
    """
    cleaned = (value or "").encode("cp1252", errors="replace").decode("cp1252")
    return escape(cleaned).replace("\n", "<br/>")


_MIRRORED = {TA_LEFT: TA_RIGHT, TA_RIGHT: TA_LEFT}
_styles: dict[tuple, ParagraphStyle] = {}


def _style(base: ParagraphStyle, font: str, alignment: int, leading: float) -> ParagraphStyle:
    key = (base.name, font, alignment, leading)
    if key not in _styles:
        _styles[key] = ParagraphStyle(
            f"{base.name}-{len(_styles)}", parent=base, fontName=font, alignment=alignment,
            leading=leading,
        )
    return _styles[key]


def _para(value: str, style: ParagraphStyle, width: float | None = None, rtl: bool = False) -> Paragraph:
    """One piece of text as a paragraph, in whichever font can draw it.

    `rtl` is the sheet's direction: on an Arabic sheet everything is mirrored,
    Latin text included. `width` is the room it has, needed only for Arabic --
    which is wrapped here, before shaping, because a shaped line cannot be
    broken afterwards (see app/services/arabic.py).
    """
    value = value or ""
    alignment = style.alignment
    if alignment != TA_CENTER and rtl:
        alignment = _MIRRORED.get(alignment, alignment)
    if not arabic.has_arabic(value):
        return Paragraph(_text(value), _style(style, style.fontName, alignment, style.leading))
    if alignment != TA_CENTER and not rtl and arabic.is_rtl(value):
        alignment = TA_RIGHT
    font = arabic.font_for(style.fontName)
    lines = arabic.wrap(
        value, font, style.fontSize, None if width is None else max(width - 1, 10),
        rtl=True if rtl else None,
    )
    leading = max(style.leading, round(style.fontSize * 1.5, 1))
    return Paragraph("<br/>".join(escape(line) for line in lines), _style(style, font, alignment, leading))


def _draw(page: canvas.Canvas, x: float, y: float, value: str, font: str, size: float,
          anchor: str = "left", rtl: bool = False) -> float:
    """Text straight onto the canvas. Returns its width."""
    if arabic.has_arabic(value):
        font = arabic.font_for(font)
        value = arabic.visual(value, True if rtl else None)
    else:
        value = _text_plain(value)
    page.setFont(font, size)
    if anchor == "right":
        page.drawRightString(x, y, value)
    else:
        page.drawString(x, y, value)
    return page.stringWidth(value, font, size)


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
    language = work_order.language if work_order.language in _LANGUAGES else "en"
    rtl = language == "ar"
    rev = f"{_CAPTIONS['rev'][1 if rtl else 0]} {work_order.revision}"

    def ordered(cells: list) -> list:
        """Left to right on an English sheet; the other way on an Arabic one."""
        return cells[::-1] if rtl else cells

    def chrome(page: canvas.Canvas, page_no: int, total: int) -> None:
        top = height - _MARGIN
        if _LOGO.is_file():
            mark_h = 24 * mm
            reader = ImageReader(str(_LOGO))
            mark_w = mark_h * reader.getSize()[0] / reader.getSize()[1]
            page.drawImage(reader, _MARGIN, top - mark_h, width=mark_w, height=mark_h, mask="auto")
        right = width - _MARGIN
        page.setFillColor(_INK)
        if language == "both":
            # The English name, with the Arabic one beside it.
            taken = _draw(page, right, top - 9 * mm, _CAPTIONS["work_order"][0], "Helvetica-Bold", 20, "right")
            page.setFillColor(_MUTED)
            _draw(page, right - taken - 4 * mm, top - 9 * mm, _CAPTIONS["work_order"][1], "Helvetica-Bold", 15, "right")
        else:
            _draw(page, right, top - 9 * mm, _caption("work_order", language), "Helvetica-Bold", 20, "right", rtl)
        page.setFillColor(_GOLD)
        _draw(page, right, top - 16 * mm, f"{number}   {rev}", "Helvetica-Bold", 12, "right", rtl)
        page.setFillColor(_MUTED)
        _draw(page, right, top - 21 * mm, _caption("release", language), "Helvetica", 8, "right", rtl)
        page.setStrokeColor(_GOLD)
        page.setLineWidth(1.2)
        page.line(_MARGIN, top - 26 * mm, width - _MARGIN, top - 26 * mm)

        page.setStrokeColor(_RULE)
        page.setLineWidth(0.5)
        page.line(_MARGIN, _MARGIN + 5 * mm, width - _MARGIN, _MARGIN + 5 * mm)
        page.setFillColor(_MUTED)
        _draw(
            page, _MARGIN, _MARGIN + 1.5 * mm,
            f"{number} {rev}  ·  {project.code if project else ''}", "Helvetica", 7.5, "left", rtl,
        )
        _draw(
            page, width - _MARGIN, _MARGIN + 1.5 * mm,
            _CAPTIONS["page"][1 if rtl else 0].format(page=page_no, total=total),
            "Helvetica", 7.5, "right", rtl,
        )

    story = []

    # -- who, what, when -------------------------------------------------------
    def wording(english: str, in_arabic: str) -> list[str]:
        """What to print of a text that exists in two languages."""
        if language == "ar":
            return [in_arabic or english]
        if language == "both":
            return [text for text in (english, in_arabic) if text]
        return [english]

    def fact(key: str, value: str, room: float):
        return [
            _para(_caption(key, language).upper(), _LABEL, room, rtl),
            _para(value, _BASE, room, rtl) if value else Paragraph("&nbsp;", _BASE),
        ]

    shares = [0.34, 0.30, 0.14, 0.22] if language == "en" else [0.32, 0.26, 0.20, 0.22]
    fact_widths = [usable * share for share in shares]
    issued = work_order.issued_at.strftime("%d/%m/%Y" if rtl else "%d %b %Y")
    facts = Table(
        [ordered([
            fact("project", f"{project.code} — {project.name}" if project else "", fact_widths[0] - 6),
            fact("client", (project.client or "") if project else "", fact_widths[1] - 6),
            fact("issued", issued, fact_widths[2] - 6),
            fact("issued_by", issuer.display_name if issuer else "", fact_widths[3] - 6),
        ])],
        colWidths=ordered(fact_widths),
    )
    facts.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6 if rtl else 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0 if rtl else 6),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(facts)
    titles = [text for text in wording(work_order.title, work_order.title_ar) if text]
    if titles:
        story.append(Spacer(1, 3 * mm))
        heading = ParagraphStyle(
            "title", parent=_BASE, fontName="Helvetica-Bold", fontSize=11.5, leading=14)
        story.extend(_para(text, heading, usable, rtl) for text in titles)
    story.append(Spacer(1, 4 * mm))

    # -- the items -------------------------------------------------------------
    pic_w, pic_h = 23 * mm, 18 * mm
    # Captions in Arabic need a little more room than "Rev" does.
    columns = [("number", 7 * mm)]
    if work_order.show_picture:
        columns.append(("picture", pic_w + 3 * mm))
    columns.append(("code", 23 * mm))
    if work_order.show_description:
        columns.append(("description", None))
    if work_order.show_dimensions:
        columns.append(("dimensions", None if not work_order.show_description else 33 * mm))
    if work_order.show_location:
        columns.append(("location", 23 * mm if language == "en" else 26 * mm))
    columns += [("rev", 9 * mm if language == "en" else 13 * mm), ("qty", 13 * mm)]
    fixed = sum(w for _name, w in columns if w is not None)
    flexible = [name for name, w in columns if w is None]
    if not flexible:
        # Nothing to stretch: let the code column take the slack.
        columns = [(name, (w + usable - fixed) if name == "code" else w) for name, w in columns]
    else:
        share = (usable - fixed) / len(flexible)
        columns = [(name, share if w is None else w) for name, w in columns]
    columns = ordered(columns)
    names = [name for name, _w in columns]
    room = {name: w - 8 for name, w in columns}   # the cell, less its padding

    def cell(name: str, value: str, style: ParagraphStyle) -> Paragraph:
        return _para(value, style, room[name], rtl)

    rows = [[cell(name, _caption(name, language, stacked=True).upper(), _HEAD) for name in names]]
    for index, line in enumerate(work_order.lines, start=1):
        described = [
            cell("description", text, _BASE)
            for text in wording(line.description, line.description_ar) if text
        ] if "description" in room else []
        cells = {
            "number": lambda: Paragraph(str(index), _SMALL),
            "picture": lambda: _picture(line.image_id, pic_w, pic_h) if line.image_id else "",
            "code": lambda: cell("code", line.code, _CODE),
            "description": lambda: described[0] if len(described) == 1 else described or "",
            "dimensions": lambda: cell("dimensions", line.dimensions, _BASE),
            "location": lambda: cell(
                "location", "\n".join(part for part in (line.area, line.phase) if part), _SMALL
            ),
            "rev": lambda: cell("rev", line.drawing_revision, _CENTER),
            "qty": lambda: _para(str(line.qty), _NUM, None, rtl),
        }
        rows.append([cells[name]() for name in names])

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
    if "picture" in names:
        style.append(("ALIGN", (names.index("picture"), 1), (names.index("picture"), -1), "CENTER"))
    table.setStyle(TableStyle(style))
    story.append(table)

    # -- totals, notes, signatures: kept together, never split from each other --
    pieces = sum(line.qty for line in work_order.lines)
    count = len(work_order.lines)
    total_style = ParagraphStyle("total", parent=_BASE, alignment=TA_RIGHT, fontSize=9.5)
    closing = [Spacer(1, 3 * mm)]
    if language != "ar":
        closing.append(Paragraph(
            f"<b>{count}</b> item{'' if count == 1 else 's'}  ·  <b>{pieces}</b> piece{'' if pieces == 1 else 's'}",
            total_style,
        ))
    if language != "en":
        closing.append(_para(
            _CAPTIONS["totals"][1].format(count=count, pieces=pieces), total_style, usable, rtl
        ))
    notes = [text for text in wording(work_order.notes, work_order.notes_ar) if text]
    if notes:
        closing += [
            Spacer(1, 4 * mm),
            _para(_caption("notes", language).upper(), _LABEL, usable, rtl),
            Spacer(1, 1 * mm),
        ]
        for position, text in enumerate(notes):
            if position:
                closing.append(Spacer(1, 1.5 * mm))
            closing.append(_para(text, _BASE, usable, rtl))
    # Three lines to sign on, with air between them.
    gap = 8 * mm
    sign_w = (usable - 2 * gap) / 3
    signed = [
        _para(_caption(key, language, stacked=True), _SMALL, sign_w, rtl)
        for key in ("sign_issued", "sign_received", "sign_date")
    ]
    if rtl:
        signed.reverse()
    signatures = Table(
        [[signed[0], "", signed[1], "", signed[2]]],
        colWidths=[sign_w, gap, sign_w, gap, sign_w],
        rowHeights=[6 * mm] if language == "en" else None,
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
