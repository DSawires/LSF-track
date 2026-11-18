"""Work orders: numbered documents that release a project's items to production.

A work order is paper, not state. Saving one changes nothing about its items --
they were loggable before it and are loggable after -- and no report reads it.
What it records is what was *written*: every line keeps its own copy of the
text it prints, so correcting an item next week does not quietly rewrite a
document that is already pinned up on the floor.

Editing one re-issues it. The revision goes up, the issue date and issuer are
stamped again, and the lines are replaced with what was sent.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import Area, Item, ItemImage, Phase, Project, User, WorkOrder, WorkOrderLine


class WorkOrderError(Exception):
    """The work order cannot be saved as sent. `status` is the HTTP answer."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def label(work_order: WorkOrder) -> str:
    return f"WO-{work_order.number:04d}"


def _lines(db: Session, project: Project, payload, existing: WorkOrder | None) -> list[WorkOrderLine]:
    """The lines as they will be stored: what the office typed, plus a copy of
    everything else the document prints about each item."""
    old = {line.id: line for line in (existing.lines if existing else [])}
    item_ids = [line.item_id for line in payload.lines if line.item_id is not None]
    items = {item.id: item for item in db.scalars(sa.select(Item).where(Item.id.in_(item_ids)))}
    areas = {a.id: a.name for a in db.scalars(sa.select(Area).where(Area.project_id == project.id))}
    phases = {p.id: p.name for p in db.scalars(sa.select(Phase).where(Phase.project_id == project.id))}
    image_ids = [line.image_id for line in payload.lines if line.image_id is not None]
    images = {
        image.id: image
        for image in db.scalars(sa.select(ItemImage).where(ItemImage.id.in_(image_ids)))
    }

    out: list[WorkOrderLine] = []
    seen: set[uuid.UUID] = set()
    for position, line in enumerate(payload.lines, start=1):
        kept = old.get(line.id) if line.id is not None else None
        if line.item_id is None:
            # Only a line already on this work order may have lost its item (it
            # was deleted for good). It keeps what was written about it.
            if kept is None or kept.item_id is not None:
                raise WorkOrderError(f"line {position} names no item")
            code, area, phase, revision = kept.code, kept.area, kept.phase, kept.drawing_revision
        else:
            item = items.get(line.item_id)
            if item is None:
                raise WorkOrderError(f"line {position}: that item no longer exists")
            if item.project_id != project.id:
                raise WorkOrderError(
                    f"{item.code} belongs to another project; a work order releases one "
                    "project's items",
                    409,
                )
            if item.id in seen:
                raise WorkOrderError(f"{item.code} is on this work order twice", 409)
            seen.add(item.id)
            code, revision = item.code, item.drawing_revision
            area = areas.get(item.area_id, "") if item.area_id else ""
            phase = phases.get(item.phase_id, "") if item.phase_id else ""

        if line.image_id is not None:
            image = images.get(line.image_id)
            if image is None or image.item_id != line.item_id:
                raise WorkOrderError(f"line {position}: that picture is not one of {code}'s")

        out.append(
            WorkOrderLine(
                seq=position * 10,
                item_id=line.item_id,
                image_id=line.image_id,
                code=code,
                description=line.description.strip(),
                dimensions=line.dimensions.strip(),
                qty=line.qty,
                area=area,
                phase=phase,
                drawing_revision=revision,
            )
        )
    return out


def save(db: Session, user: User, payload, existing: WorkOrder | None = None) -> WorkOrder:
    """Issue a new work order, or re-issue an existing one as its next revision."""
    project = db.get(Project, payload.project_id)
    if project is None:
        raise WorkOrderError("unknown project")
    if existing is not None and existing.project_id != project.id:
        raise WorkOrderError("a work order stays with its project", 409)
    if existing is None and not project.is_active:
        raise WorkOrderError(f"project {project.code} is archived; reactivate it first", 409)

    lines = _lines(db, project, payload, existing)
    fields = {
        "title": payload.title.strip(),
        "notes": payload.notes.strip(),
        "show_picture": payload.show_picture,
        "show_description": payload.show_description,
        "show_dimensions": payload.show_dimensions,
        "show_location": payload.show_location,
        "issued_at": utcnow(),
        "issued_by_user_id": user.id,
    }

    if existing is not None:
        for name, value in fields.items():
            setattr(existing, name, value)
        existing.revision += 1
        existing.lines = lines
        db.flush()
        return existing

    # The next number, taken optimistically: two people issuing at the same
    # instant both read the same maximum, the unique index refuses the second,
    # and it simply tries again one higher. A SAVEPOINT, so the loser's failed
    # insert does not take the rest of the request down with it.
    for _attempt in range(8):
        number = (db.scalar(sa.select(sa.func.max(WorkOrder.number))) or 0) + 1
        work_order = WorkOrder(number=number, project_id=project.id, revision=1, **fields)
        work_order.lines = lines
        try:
            with db.begin_nested():
                db.add(work_order)
                db.flush()
            return work_order
        except sa.exc.IntegrityError:
            if work_order in db:
                db.expunge(work_order)
            lines = _lines(db, project, payload, None)
    raise WorkOrderError("could not take a work order number; try again", 503)


def payload_of(db: Session, work_order: WorkOrder, with_lines: bool = True) -> dict:
    issuer = db.get(User, work_order.issued_by_user_id)
    body = {
        "id": str(work_order.id),
        "number": work_order.number,
        "label": label(work_order),
        "project_id": str(work_order.project_id),
        "title": work_order.title,
        "notes": work_order.notes,
        "revision": work_order.revision,
        "show_picture": work_order.show_picture,
        "show_description": work_order.show_description,
        "show_dimensions": work_order.show_dimensions,
        "show_location": work_order.show_location,
        "issued_at": work_order.issued_at.isoformat(),
        "issued_by": issuer.display_name if issuer else None,
        "line_count": len(work_order.lines),
        "total_qty": sum(line.qty for line in work_order.lines),
        "pdf_url": f"/api/work-orders/{work_order.id}/pdf",
    }
    if with_lines:
        body["lines"] = [
            {
                "id": str(line.id),
                "item_id": str(line.item_id) if line.item_id else None,
                "image_id": str(line.image_id) if line.image_id else None,
                "image_url": f"/api/images/{line.image_id}" if line.image_id else None,
                "code": line.code,
                "description": line.description,
                "dimensions": line.dimensions,
                "qty": line.qty,
                "area": line.area,
                "phase": line.phase,
                "drawing_revision": line.drawing_revision,
            }
            for line in work_order.lines
        ]
    return body
