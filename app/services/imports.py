"""Projects arrive as a file: one spreadsheet, one row per item, all at once.

The format is deliberately what an engineer already has open -- a sheet with a
header row -- saved as .xlsx or .csv:

    project | project name | client | item | description | qty | sample qty | revision | area | phase
    HOTEL-B | Grand Hotel  | Acme   | BST-1| Bedside     | 120 | 1          | A        | Guest rooms | Phase 1

- `project`, `item`, `qty` are required; the rest are optional.
- `area` and `phase` say which part of the job, and which phase of it, the
  batch is for. Each project has its own lists; a name is matched against the
  row's project, and one that project does not have yet is added to it when
  the file is committed -- the preview says so first. The same item code may appear on several rows as
  long as each has a different area or phase: those are separate batches.
- `project name` and `client` are only read when the project is new. An
  existing project is added to, never renamed by a file.
- `qty` is the production batch. `sample qty` (default 1, 0 for none) is built
  first; the batch is released when it is logged Done.

There is no `stages` column. An item is not given a route: it can be logged at
any stage in the factory, in any order. Files written for the older format
still carry one; it is read past, and the preview says so.

Pictures come along too. A picture placed on an item's row of an .xlsx -- over
the cells, in any column -- becomes that item's icon when the file is
committed, exactly as if it had been uploaded in the item editor. A picture is
never a reason to refuse a file: one that cannot be used (wrong format, too
big, placed *inside* a cell) is left out and the preview says which.

Headers are matched loosely (case, spaces, underscores) because the file is
typed by people, not generated. Validation is all-or-nothing: every row is
checked and every problem reported with its spreadsheet row number, and nothing
is written unless the whole file is clean. A half-imported project is worse
than none, because the missing items are exactly the ones nobody notices.
"""

from __future__ import annotations

import csv
import io
import logging
import posixpath
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import Area, Item, ItemImage, Phase, Project, User
from app.services import images as pictures_store
from app.services.images import Prepared, Unreadable, prepare, sniff
from app.services.items import ItemSetupError, identity_taken, lock_project
from app.storage import get_storage

log = logging.getLogger(__name__)

MAX_ROWS = 2000
# Room for a picture on every row: a couple of hundred thumbnails is a few MB.
MAX_BYTES = 40 * 1024 * 1024
# One picture. These become item icons, shown at thumbnail size on a phone.
MAX_PICTURE_BYTES = 5 * 1024 * 1024
# How far down a sheet is read looking for items. Blank rows are free, but a
# sheet formatted to the bottom of Excel is a million of them.
MAX_SCANNED_ROWS = 50_000

# Column widths from app/models.py; an over-length value is a row error here
# rather than a DataError at commit.
_PROJECT_CODE_MAX = 32
_ITEM_CODE_MAX = 64
_LABEL_MAX = 120

COLUMNS: dict[str, set[str]] = {
    "project": {"project", "project code", "job", "job code"},
    "project_name": {"project name", "job name"},
    "client": {"client", "customer"},
    "item": {"item", "item code", "code", "reference", "ref"},
    "description": {"description", "desc"},
    "qty": {"qty", "quantity", "total qty", "batch qty", "production qty"},
    "sample_qty": {"sample", "sample qty", "samples", "sample quantity"},
    "revision": {"revision", "rev", "drawing revision", "drawing rev"},
    "dimensions": {"dimensions", "dimension", "dims", "size"},
    "area": {"area", "zone", "location"},
    "phase": {"phase", "project phase", "delivery phase"},
}
REQUIRED = ("project", "item", "qty")

# Headers of the column older files used for an item's stage sequence. Not
# read any more; recognised only so the preview can say it was skipped.
_RETIRED_STAGE_HEADERS = {"stages", "sequence", "route"}


class ImportFileError(Exception):
    """The file itself cannot be read -- as opposed to a row that is wrong."""


@dataclass
class Picture:
    """A picture lifted out of the file, already decoded: what will be stored
    of it, never the bytes the spreadsheet carried."""

    prepared: Prepared


@dataclass
class PlannedItem:
    row: int
    project_code: str
    code: str
    description: str
    qty: int
    sample_qty: int
    revision: str
    dimensions: str = ""
    # As they will be stored: the name on the list (existing spelling wins over
    # the file's), or the file's own spelling for one that is new. "" = none.
    area: str = ""
    phase: str = ""
    # The picture on this item's row, if the file carried one.
    picture: Picture | None = None


@dataclass
class PlannedProject:
    code: str
    name: str
    client: str | None
    exists: bool
    item_count: int = 0


@dataclass
class ImportPlan:
    projects: dict[str, PlannedProject] = field(default_factory=dict)
    items: list[PlannedItem] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    # Things worth knowing that do not stop the import.
    notes: list[str] = field(default_factory=list)
    # Names the file uses that their project does not have yet, in file order:
    # (project code, name).
    new_areas: list[tuple[str, str]] = field(default_factory=list)
    new_phases: list[tuple[str, str]] = field(default_factory=list)

    def error(self, row: int | None, message: str) -> None:
        self.errors.append({"row": row, "message": message})

    def as_dict(self) -> dict:
        return {
            "projects": [
                {
                    "code": p.code,
                    "name": p.name,
                    "client": p.client,
                    "is_new": not p.exists,
                    "item_count": p.item_count,
                }
                for p in self.projects.values()
            ],
            "items": [
                {
                    "row": i.row,
                    "project": i.project_code,
                    "code": i.code,
                    "description": i.description,
                    "qty": i.qty,
                    "sample_qty": i.sample_qty,
                    "revision": i.revision,
                    "dimensions": i.dimensions,
                    "area": i.area or None,
                    "phase": i.phase or None,
                    "has_picture": i.picture is not None,
                }
                for i in self.items
            ],
            "errors": self.errors,
            "notes": self.notes,
            "new_areas": [{"project": p, "name": n} for p, n in self.new_areas],
            "new_phases": [{"project": p, "name": n} for p, n in self.new_phases],
        }


# -- reading the file ---------------------------------------------------------


def read_rows(filename: str, content: bytes) -> list[tuple[int, list]]:
    """(spreadsheet row number, cell values) for every row of the first sheet."""
    if len(content) > MAX_BYTES:
        raise ImportFileError(
            f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB; "
            "split it by project, or shrink its pictures"
        )
    if content[:4] == b"PK\x03\x04":
        return _read_xlsx(content)
    if filename.lower().endswith((".xlsx", ".xlsm")):
        raise ImportFileError("that does not look like a real .xlsx file")
    if filename.lower().endswith(".xls"):
        raise ImportFileError("old-style .xls is not supported; save it as .xlsx or .csv")
    return _read_csv(content)


def _read_xlsx(content: bytes) -> list[tuple[int, list]]:
    import openpyxl

    try:
        book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises a zoo of types for bad files
        raise ImportFileError(f"could not open the spreadsheet ({exc.__class__.__name__})")
    try:
        sheet = book.worksheets[0]
        rows = []
        for number, values in enumerate(sheet.iter_rows(values_only=True), start=1):
            # Blank rows are skipped here, not counted: a sheet with gaps
            # between its groups must not push real items past a row cap and
            # have them dropped without a word. Stopping early is only ever
            # done loudly -- by plan_import when there are too many items, or
            # below when the sheet itself is implausibly long.
            if number > MAX_SCANNED_ROWS:
                raise ImportFileError(
                    f"the sheet runs past row {MAX_SCANNED_ROWS}; delete the empty "
                    "rows below the data (or split the file) and try again"
                )
            if not any(_cell(value) for value in values):
                continue
            rows.append((number, list(values)))
            if len(rows) > MAX_ROWS + 2:
                break  # header + more items than one file may hold: an error
        return rows
    finally:
        book.close()


def _read_csv(content: bytes) -> list[tuple[int, list]]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ImportFileError("could not read the file's text encoding; save it as UTF-8 CSV")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [
        (number, row)
        for number, row in enumerate(csv.reader(io.StringIO(text), dialect), start=1)
    ]


# -- pictures ------------------------------------------------------------------

_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}
_R_ID = f"{{{_NS['r']}}}id"
_R_EMBED = f"{{{_NS['r']}}}embed"

_IN_CELL_NOTE = (
    "This file has pictures placed inside cells (Excel's \"Place in Cell\"). Those "
    "cannot be read: use Insert > Pictures > Place over Cells, on the item's row."
)


def _relationships(archive: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    """{relationship id: (type, target part)} for one part of the package."""
    rels_path = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if rels_path not in archive.namelist():
        return {}
    out = {}
    for rel in ElementTree.fromstring(archive.read(rels_path)).findall("rel:Relationship", _NS):
        target = rel.get("Target", "")
        if rel.get("TargetMode") == "External":
            continue
        resolved = (
            target.lstrip("/")
            if target.startswith("/")
            else posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
        )
        out[rel.get("Id", "")] = (rel.get("Type", ""), resolved)
    return out


def read_pictures(content: bytes) -> tuple[dict[int, Picture], list[str]]:
    """Pictures on the first sheet of an .xlsx, keyed by the spreadsheet row
    their top-left corner sits on, plus anything worth telling the office.

    A picture belongs to the row it is anchored to, whatever column it is in;
    if a row has several, the left-most wins. Read straight out of the package
    rather than through openpyxl, which needs an imaging library to hand
    pictures back and would make that a dependency of the server for the sake
    of looking at the first sixteen bytes.

    Nothing here raises. Pictures are a convenience riding on the import: a
    file whose pictures cannot be read still imports its items.
    """
    if content[:4] != b"PK\x03\x04":
        return {}, []
    pictures: dict[int, tuple[int, Picture]] = {}
    notes: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            if any(name.startswith("xl/richData/") for name in names):
                notes.append(_IN_CELL_NOTE)

            workbook_rels = _relationships(archive, "xl/workbook.xml")
            sheets = ElementTree.fromstring(archive.read("xl/workbook.xml")).findall(
                "main:sheets/main:sheet", _NS
            )
            sheet_part = next(
                (
                    workbook_rels[sheet.get(_R_ID)][1]
                    for sheet in sheets
                    if sheet.get(_R_ID) in workbook_rels
                    and workbook_rels[sheet.get(_R_ID)][0].endswith("/worksheet")
                ),
                None,
            )
            if sheet_part is None or sheet_part not in names:
                return {}, notes

            skipped_format: list[int] = []
            skipped_size: list[int] = []
            skipped_unreadable: list[int] = []
            for kind, drawing_part in _relationships(archive, sheet_part).values():
                if not kind.endswith("/drawing") or drawing_part not in names:
                    continue
                media = _relationships(archive, drawing_part)
                root = ElementTree.fromstring(archive.read(drawing_part))
                for anchor in list(root.findall("xdr:twoCellAnchor", _NS)) + list(
                    root.findall("xdr:oneCellAnchor", _NS)
                ):
                    row_text = anchor.findtext("xdr:from/xdr:row", namespaces=_NS)
                    col_text = anchor.findtext("xdr:from/xdr:col", namespaces=_NS)
                    blip = anchor.find(".//xdr:pic/xdr:blipFill/a:blip", _NS)
                    if row_text is None or blip is None:
                        continue
                    row, column = int(row_text) + 1, int(col_text or 0)
                    target = media.get(blip.get(_R_EMBED, ""), ("", ""))[1]
                    if target not in names:
                        continue
                    if row in pictures and pictures[row][0] <= column:
                        continue
                    # The declared size, checked before a byte is inflated.
                    if archive.getinfo(target).file_size > MAX_PICTURE_BYTES:
                        skipped_size.append(row)
                        continue
                    data = archive.read(target)
                    if sniff(data[:16]) is None:
                        skipped_format.append(row)
                        continue
                    # Decoded here, at preview, so a picture that cannot be
                    # used is a note now and never a failed commit later.
                    try:
                        prepared = prepare(data)
                    except Unreadable:
                        skipped_unreadable.append(row)
                        continue
                    pictures[row] = (column, Picture(prepared=prepared))

            def rows(numbers: list[int]) -> str:
                listed = ", ".join(str(n) for n in sorted(set(numbers))[:12])
                return listed + (" …" if len(set(numbers)) > 12 else "")

            if skipped_format:
                notes.append(
                    f"Pictures on row(s) {rows(skipped_format)} are not JPEG, PNG, WebP, GIF or "
                    "HEIC and were left out."
                )
            if skipped_unreadable:
                notes.append(
                    f"Pictures on row(s) {rows(skipped_unreadable)} could not be read as images "
                    "and were left out."
                )
            if skipped_size:
                notes.append(
                    f"Pictures on row(s) {rows(skipped_size)} are larger than "
                    f"{MAX_PICTURE_BYTES // (1024 * 1024)} MB and were left out."
                )
    except (zipfile.BadZipFile, KeyError, ValueError, ElementTree.ParseError):
        log.exception("could not read the pictures out of an import file")
        return {}, ["The pictures in this file could not be read; the items are unaffected."]
    return {row: picture for row, (_column, picture) in pictures.items()}, notes


# -- validating ---------------------------------------------------------------


def _norm_header(value) -> str:
    return re.sub(r"[\s_\-]+", " ", str(value or "")).strip().lower()


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _whole_number(value) -> int | None:
    text = _cell(value)
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if not number.is_integer():
        return None
    return int(number)


def plan_import(
    db: Session,
    rows: list[tuple[int, list]],
    pictures: dict[int, Picture] | None = None,
    picture_notes: list[str] | None = None,
) -> ImportPlan:
    plan = ImportPlan()
    pictures = pictures or {}
    plan.notes.extend(picture_notes or [])

    header_at = next((i for i, (_, values) in enumerate(rows) if any(_cell(v) for v in values)), None)
    if header_at is None:
        plan.error(None, "the file is empty")
        return plan
    header_row, header_values = rows[header_at]
    columns: dict[str, int] = {}
    for index, value in enumerate(header_values):
        name = _norm_header(value)
        for key, aliases in COLUMNS.items():
            if name in aliases and key not in columns:
                columns[key] = index
    missing = [key for key in REQUIRED if key not in columns]
    if missing:
        plan.error(
            header_row,
            "missing column(s): " + ", ".join(missing)
            + " -- the first row must be the headers (download the template to see them)",
        )
        return plan

    if any(_norm_header(value) in _RETIRED_STAGE_HEADERS for value in header_values):
        plan.notes.append(
            "The stages column is not used any more and was skipped: "
            "an item can be logged at any stage, in any order."
        )

    data = [(n, v) for n, v in rows[header_at + 1:] if any(_cell(x) for x in v)]
    if not data:
        plan.error(None, "the file has headers but no items")
        return plan
    if len(data) > MAX_ROWS:
        plan.error(None, f"more than {MAX_ROWS} items in one file; split it by project")
        return plan

    existing_projects = {p.code: p for p in db.scalars(sa.select(Project))}
    # Each project has its own areas and phases, so a name only means something
    # next to a project code.
    code_of = {p.id: p.code for p in existing_projects.values()}
    areas = {(code_of[a.project_id], a.name.casefold()): a for a in db.scalars(sa.select(Area))}
    phases = {(code_of[ph.project_id], ph.name.casefold()): ph for ph in db.scalars(sa.select(Phase))}
    new_areas: dict[tuple[str, str], str] = {}   # (project, casefolded) -> first spelling in the file
    new_phases: dict[tuple[str, str], str] = {}

    def label(
        project_code: str, text: str, existing: dict, fresh: dict, noun: str, problems: list[str]
    ) -> str:
        """The name this row's area or phase will be stored under."""
        if not text:
            return ""
        if len(text) > _LABEL_MAX:
            problems.append(f"{noun} longer than {_LABEL_MAX} characters")
            return ""
        key = (project_code, text.casefold())
        row = existing.get(key)
        if row is not None:
            if not row.is_active:
                problems.append(f"{noun} '{row.name}' is retired; reactivate it first")
            return row.name
        return fresh.setdefault(key, text)

    def get(values: list, key: str) -> str:
        index = columns.get(key)
        return _cell(values[index]) if index is not None and index < len(values) else ""

    # An item is project + code + area + phase, so that is what may not repeat.
    seen_items: dict[tuple[str, str, str, str], int] = {}
    for row, values in data:
        project_code = get(values, "project").upper()
        code = get(values, "item")
        problems: list[str] = []
        area = label(project_code, get(values, "area"), areas, new_areas, "area", problems)
        phase = label(project_code, get(values, "phase"), phases, new_phases, "phase", problems)
        identity = (project_code, code, area.casefold(), phase.casefold())

        if not project_code:
            problems.append("no project code")
        elif len(project_code) > _PROJECT_CODE_MAX:
            problems.append(f"project code longer than {_PROJECT_CODE_MAX} characters")
        if not code:
            problems.append("no item code")
        elif len(code) > _ITEM_CODE_MAX:
            problems.append(f"item code longer than {_ITEM_CODE_MAX} characters")
        elif identity in seen_items:
            where = ", ".join(part for part in (area, phase) if part)
            problems.append(
                f"item {code}{f' ({where})' if where else ''} is also on row {seen_items[identity]}"
                + ("" if where else "; give the rows different areas or phases if they are separate batches")
            )
        else:
            seen_items[identity] = row

        qty_cell = values[columns["qty"]] if columns["qty"] < len(values) else None
        qty = _whole_number(qty_cell)
        if qty is None or qty <= 0 or qty > 1_000_000:
            problems.append(f"qty must be a whole number above 0 (got '{_cell(qty_cell)}')")

        sample_text = get(values, "sample_qty")
        sample_qty = 1 if not sample_text else _whole_number(sample_text)
        if sample_qty is None or sample_qty < 0 or sample_qty > 1_000:
            problems.append(f"sample qty must be a whole number, 0 or more (got '{sample_text}')")

        revision = get(values, "revision") or "A"
        if len(revision) > 32:
            problems.append("revision longer than 32 characters")
        description = get(values, "description")
        if len(description) > 255:
            problems.append("description longer than 255 characters")
        dimensions = get(values, "dimensions")
        if len(dimensions) > 255:
            problems.append("dimensions longer than 255 characters")

        project = existing_projects.get(project_code) if project_code else None
        if project is not None and not project.is_active:
            problems.append(f"project {project_code} is archived; reactivate it first")

        if problems:
            for problem in problems:
                plan.error(row, problem)
            continue

        planned_project = plan.projects.get(project_code)
        if planned_project is None:
            planned_project = PlannedProject(
                code=project_code,
                name=project.name if project else "",
                client=project.client if project else None,
                exists=project is not None,
            )
            plan.projects[project_code] = planned_project
        if not planned_project.exists:
            planned_project.name = planned_project.name or get(values, "project_name")
            planned_project.client = planned_project.client or get(values, "client") or None
        planned_project.item_count += 1

        plan.items.append(
            PlannedItem(
                row=row,
                project_code=project_code,
                code=code,
                description=description,
                qty=qty,
                sample_qty=sample_qty,
                revision=revision,
                dimensions=dimensions,
                area=area,
                phase=phase,
                picture=pictures.get(row),
            )
        )

    # Against what is already there. Only an existing project can hold a clash,
    # and only on an area and phase that both exist already.
    for planned in plan.items:
        project = existing_projects.get(planned.project_code)
        if project is None:
            continue
        area_row = areas.get((planned.project_code, planned.area.casefold())) if planned.area else None
        phase_row = (
            phases.get((planned.project_code, planned.phase.casefold())) if planned.phase else None
        )
        if (planned.area and area_row is None) or (planned.phase and phase_row is None):
            continue
        if identity_taken(
            db,
            project.id,
            planned.code,
            area_row.id if area_row else None,
            phase_row.id if phase_row else None,
        ):
            where = ", ".join(part for part in (planned.area, planned.phase) if part)
            plan.error(
                planned.row,
                f"item {planned.code}{f' ({where})' if where else ''} already exists in "
                f"{planned.project_code}",
            )

    # Only names used by rows that are actually going in.
    used_areas = {(i.project_code, i.area.casefold()) for i in plan.items}
    used_phases = {(i.project_code, i.phase.casefold()) for i in plan.items}
    plan.new_areas = [(key[0], name) for key, name in new_areas.items() if key in used_areas]
    plan.new_phases = [(key[0], name) for key, name in new_phases.items() if key in used_phases]
    for noun, fresh in (("area", plan.new_areas), ("phase", plan.new_phases)):
        by_project: dict[str, list[str]] = {}
        for project_code, name in fresh:
            by_project.setdefault(project_code, []).append(name)
        for project_code, names in by_project.items():
            plan.notes.append(
                f"New {noun}{'' if len(names) == 1 else 's'} for {project_code}, added on import: "
                + ", ".join(names)
                + "."
            )

    plan.errors.sort(key=lambda e: (e["row"] is not None, e["row"] or 0))
    return plan


def commit_import(db: Session, plan: ImportPlan, user: User) -> None:
    """Write a clean plan. Every item gets one creation moment: the file is
    one handoff to the floor. A picture on an item's row is stored as its
    icon, under the name of whoever imported the file."""
    if plan.errors:
        raise ItemSetupError("the file has errors; nothing was imported")
    moment = utcnow()
    project_ids = {}
    for planned in plan.projects.values():
        project = db.scalars(sa.select(Project).where(Project.code == planned.code)).first()
        if project is None:
            project = Project(
                code=planned.code,
                name=planned.name or planned.code,
                client=planned.client,
                created_at=moment,
            )
            db.add(project)
            db.flush()
        project_ids[planned.code] = project.id

    # One writer at a time per project, then look again: the plan was checked
    # a moment ago, and another import may have landed in that moment.
    for project_id in sorted(project_ids.values(), key=str):
        lock_project(db, project_id)

    def labels(model, wanted: list[tuple[str, str]]) -> dict[tuple[str, str], uuid.UUID]:
        """{(project code, casefolded name): id} for the projects in this file,
        creating the rows the file introduces."""
        code_of = {project_id: code for code, project_id in project_ids.items()}
        rows = {
            (code_of[row.project_id], row.name.casefold()): row
            for row in db.scalars(sa.select(model).where(model.project_id.in_(project_ids.values())))
        }
        last: dict[str, int] = {}
        for (project_code, _name), row in rows.items():
            last[project_code] = max(last.get(project_code, 0), row.sort_order)
        for project_code, name in wanted:
            key = (project_code, name.casefold())
            if key not in rows:
                last[project_code] = last.get(project_code, 0) + 10
                row = model(
                    project_id=project_ids[project_code], name=name, sort_order=last[project_code]
                )
                db.add(row)
                rows[key] = row
        db.flush()
        return {key: row.id for key, row in rows.items()}

    area_ids = labels(Area, plan.new_areas)
    phase_ids = labels(Phase, plan.new_phases)

    storage = get_storage()
    stored: list[str] = []
    try:
        for planned in plan.items:
            area_id = (
                area_ids.get((planned.project_code, planned.area.casefold())) if planned.area else None
            )
            phase_id = (
                phase_ids.get((planned.project_code, planned.phase.casefold())) if planned.phase else None
            )
            if identity_taken(db, project_ids[planned.project_code], planned.code, area_id, phase_id):
                raise ItemSetupError(
                    f"item {planned.code} on row {planned.row} was created by someone else "
                    "while this file was being imported; nothing was imported"
                )
            item = Item(
                id=uuid.uuid4(),
                code=planned.code,
                project_id=project_ids[planned.project_code],
                area_id=area_id,
                phase_id=phase_id,
                description=planned.description,
                dimensions=planned.dimensions or None,
                total_qty=planned.qty,
                sample_qty=planned.sample_qty,
                drawing_revision=planned.revision,
                created_at=moment,
            )
            db.add(item)
            if planned.picture is not None:
                image_id = uuid.uuid4()
                stored.append(image_id)
                size = pictures_store.store(storage, image_id, planned.picture.prepared)
                db.add(
                    ItemImage(
                        id=image_id,
                        item_id=item.id,
                        kind="icon",
                        filename=f"{planned.code} (import)"[:255],
                        content_type=pictures_store.STORED_TYPE,
                        size_bytes=size,
                        uploaded_by_user_id=user.id,
                        uploaded_at=moment,
                    )
                )
        db.flush()
    except Exception:
        # All-or-nothing reaches the pictures too: the items are about to be
        # rolled back, so files already written must not be left behind with
        # no row that will ever name them.
        for image_id in stored:
            try:
                pictures_store.remove(storage, image_id)
            except Exception:  # best effort; the original failure is what matters
                log.exception("could not remove picture %s after a failed import", image_id)
        raise


def template_csv() -> str:
    """A header row and one example."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["project", "project name", "client", "item", "description", "dimensions", "qty",
         "sample qty", "revision", "area", "phase", "picture"]
    )
    # The last column is only a place to put a picture once the file is saved
    # as .xlsx; a CSV cannot carry one, and whatever is typed there is ignored.
    writer.writerow(
        ["PRJ-01", "Project name", "Client name", "ITEM-001", "Description", "W 600 x D 450 x H 720 mm",
         100, 1, "A", "", "", ""]
    )
    return out.getvalue()
