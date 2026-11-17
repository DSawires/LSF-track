"""Projects arrive as a file: one spreadsheet, one row per item, all at once.

The format is deliberately what an engineer already has open -- a sheet with a
header row -- saved as .xlsx or .csv:

    project | project name | client | item | description | qty | sample qty | revision | stages
    HOTEL-B | Grand Hotel  | Acme   | BST-1| Bedside     | 120 | 1          | A        | Carpentry > Veneer > Paint > QC > Packing

- `project`, `item`, `qty`, `stages` are required; the rest are optional.
- `project name` and `client` are only read when the project is new. An
  existing project is added to, never renamed by a file.
- `qty` is the production batch. `sample qty` (default 1, 0 for none) walks the
  stages first; the batch is released when it is done.
- `stages` is the item's own sequence, in order, by stage name or code,
  separated by `>` (or `,` `;` `|` `→`). Stages are matched against whatever
  is in the `stages` table right now -- a stage added this morning is usable in
  this afternoon's file.

Headers are matched loosely (case, spaces, underscores) because the file is
typed by people, not generated. Validation is all-or-nothing: every row is
checked and every problem reported with its spreadsheet row number, and nothing
is written unless the whole file is clean. A half-imported project is worse
than none, because the missing items are exactly the ones nobody notices.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import Item, Project, Stage
from app.services.items import ItemSetupError, assign_steps

MAX_ROWS = 2000
MAX_BYTES = 5 * 1024 * 1024
# How far down a sheet is read looking for items. Blank rows are free, but a
# sheet formatted to the bottom of Excel is a million of them.
MAX_SCANNED_ROWS = 50_000

# Column widths from app/models.py; an over-length value is a row error here
# rather than a DataError at commit.
_PROJECT_CODE_MAX = 32
_ITEM_CODE_MAX = 64

COLUMNS: dict[str, set[str]] = {
    "project": {"project", "project code", "job", "job code"},
    "project_name": {"project name", "job name"},
    "client": {"client", "customer"},
    "item": {"item", "item code", "code", "reference", "ref"},
    "description": {"description", "desc"},
    "qty": {"qty", "quantity", "total qty", "batch qty", "production qty"},
    "sample_qty": {"sample", "sample qty", "samples", "sample quantity"},
    "revision": {"revision", "rev", "drawing revision", "drawing rev"},
    "stages": {"stages", "sequence", "route"},
}
REQUIRED = ("project", "item", "qty", "stages")

_STAGE_SPLIT = re.compile(r"\s*(?:>|→|,|;|\|)\s*")


class ImportFileError(Exception):
    """The file itself cannot be read -- as opposed to a row that is wrong."""


@dataclass
class PlannedItem:
    row: int
    project_code: str
    code: str
    description: str
    qty: int
    sample_qty: int
    revision: str
    stage_ids: list = field(default_factory=list)
    stage_names: list[str] = field(default_factory=list)


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
                    "stages": i.stage_names,
                }
                for i in self.items
            ],
            "errors": self.errors,
        }


# -- reading the file ---------------------------------------------------------


def read_rows(filename: str, content: bytes) -> list[tuple[int, list]]:
    """(spreadsheet row number, cell values) for every row of the first sheet."""
    if len(content) > MAX_BYTES:
        raise ImportFileError("the file is larger than 5 MB; split it by project")
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


def plan_import(db: Session, rows: list[tuple[int, list]]) -> ImportPlan:
    plan = ImportPlan()

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

    data = [(n, v) for n, v in rows[header_at + 1:] if any(_cell(x) for x in v)]
    if not data:
        plan.error(None, "the file has headers but no items")
        return plan
    if len(data) > MAX_ROWS:
        plan.error(None, f"more than {MAX_ROWS} items in one file; split it by project")
        return plan

    stages = list(db.scalars(sa.select(Stage)))
    by_code = {s.code.lower(): s for s in stages}
    by_name = {s.name.strip().casefold(): s for s in stages}
    existing_projects = {p.code: p for p in db.scalars(sa.select(Project))}

    def get(values: list, key: str) -> str:
        index = columns.get(key)
        return _cell(values[index]) if index is not None and index < len(values) else ""

    seen_items: dict[str, int] = {}
    for row, values in data:
        project_code = get(values, "project").upper()
        code = get(values, "item")
        problems: list[str] = []

        if not project_code:
            problems.append("no project code")
        elif len(project_code) > _PROJECT_CODE_MAX:
            problems.append(f"project code longer than {_PROJECT_CODE_MAX} characters")
        if not code:
            problems.append("no item code")
        elif len(code) > _ITEM_CODE_MAX:
            problems.append(f"item code longer than {_ITEM_CODE_MAX} characters")
        elif code in seen_items:
            problems.append(f"item {code} is also on row {seen_items[code]}")
        else:
            seen_items[code] = row

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

        stage_text = get(values, "stages")
        picked: list[Stage] = []
        if not stage_text:
            problems.append("no stages")
        for token in filter(None, _STAGE_SPLIT.split(stage_text)):
            stage = by_code.get(token.lower()) or by_name.get(token.casefold())
            if stage is None:
                problems.append(f"unknown stage '{token}'")
            elif not stage.is_active:
                problems.append(f"stage '{stage.name}' is retired")
            else:
                picked.append(stage)
        # Same rule assign_steps enforces, checked here so every row's problem
        # is reported at once instead of the first one aborting the commit.
        for stage in picked[:-1]:
            if stage.is_terminal:
                problems.append(f"'{stage.name}' finishes the sequence and must be the last stage")

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
                stage_ids=[s.id for s in picked],
                stage_names=[s.name for s in picked],
            )
        )

    if seen_items:
        clashes = db.scalars(sa.select(Item.code).where(Item.code.in_(list(seen_items))))
        for code in clashes:
            plan.error(seen_items[code], f"item {code} already exists")

    plan.errors.sort(key=lambda e: (e["row"] is not None, e["row"] or 0))
    return plan


def commit_import(db: Session, plan: ImportPlan) -> None:
    """Write a clean plan. Every item gets one creation moment: the file is
    one handoff to the floor."""
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

    for planned in plan.items:
        item = Item(
            code=planned.code,
            project_id=project_ids[planned.project_code],
            description=planned.description,
            total_qty=planned.qty,
            sample_qty=planned.sample_qty,
            drawing_revision=planned.revision,
            created_at=moment,
        )
        db.add(item)
        db.flush()
        assign_steps(db, item, planned.stage_ids, created_at=moment)
    db.flush()


def template_csv(db: Session) -> str:
    """A header row and one example, built from the stages that exist now."""
    stages = list(
        db.scalars(
            sa.select(Stage).where(Stage.is_active.is_(True)).order_by(Stage.sort_order, Stage.code)
        )
    )
    example = " > ".join(s.name for s in stages if not s.is_terminal)
    terminal = next((s for s in stages if s.is_terminal), None)
    if terminal is not None:
        example = f"{example} > {terminal.name}" if example else terminal.name
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["project", "project name", "client", "item", "description", "qty", "sample qty", "revision", "stages"]
    )
    writer.writerow(["PRJ-01", "Project name", "Client name", "ITEM-001", "Description", 100, 1, "A", example])
    return out.getvalue()
