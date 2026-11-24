"""Work orders: issue, reopen, re-issue, and download as a PDF.

Open to every engineer, like the import: releasing work to the floor is the
job. Deleting one is an admin's call. Online-only, like the rest of the office
screens -- the offline guarantee protects the logging path, and a work order is
written at a desk.
"""

from __future__ import annotations

import logging
import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.deps import current_user, require_admin
from app.models import User, WorkOrder
from app.schemas import TranslateRequest, WorkOrderSave
from app.services import translate, work_orders
from app.services.work_order_pdf import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/work-orders", tags=["work orders"])


def _get(db: Session, work_order_id: uuid.UUID) -> WorkOrder:
    work_order = db.get(WorkOrder, work_order_id, options=[selectinload(WorkOrder.lines)])
    if work_order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown work order")
    return work_order


def _save(db: Session, user: User, payload: WorkOrderSave, existing: WorkOrder | None) -> dict:
    try:
        saved = work_orders.save(db, user, payload, existing)
    except work_orders.WorkOrderError as exc:
        raise HTTPException(exc.status, str(exc))
    db.refresh(saved)
    return work_orders.payload_of(db, saved)


@router.get("")
def list_work_orders(
    project_id: uuid.UUID | None = None,
    db: Session = Depends(get_db, scope="function"),
    user: User = Depends(current_user),
) -> dict:
    """Newest first. Without their lines: this is the list, not the documents."""
    query = (
        sa.select(WorkOrder).options(selectinload(WorkOrder.lines)).order_by(WorkOrder.number.desc())
    )
    if project_id is not None:
        query = query.where(WorkOrder.project_id == project_id)
    return {
        "work_orders": [
            work_orders.payload_of(db, row, with_lines=False) for row in db.scalars(query)
        ]
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def issue(
    payload: WorkOrderSave, db: Session = Depends(get_db, scope="function"), user: User = Depends(current_user)
) -> dict:
    return _save(db, user, payload, None)


@router.post("/translate")
def translate_texts(payload: TranslateRequest, user: User = Depends(current_user)) -> dict:
    """A first draft of the Arabic for a work order's texts, in the order sent.

    Stores nothing: the answer goes into editable fields on the screen and is
    saved, corrected or not, when the work order is issued. The texts go to
    Anthropic's API -- the only place in the app that sends anything out.
    """
    try:
        return {"translations": translate.to_arabic(payload.texts)}
    except translate.TranslationError as exc:
        raise HTTPException(exc.status, str(exc))


@router.get("/{work_order_id}")
def read(
    work_order_id: uuid.UUID, db: Session = Depends(get_db, scope="function"), user: User = Depends(current_user)
) -> dict:
    return work_orders.payload_of(db, _get(db, work_order_id))


@router.put("/{work_order_id}")
def reissue(
    work_order_id: uuid.UUID,
    payload: WorkOrderSave,
    db: Session = Depends(get_db, scope="function"),
    user: User = Depends(current_user),
) -> dict:
    """Save changes as the next revision, stamped with who and when."""
    return _save(db, user, payload, _get(db, work_order_id))


@router.delete("/{work_order_id}")
def delete(
    work_order_id: uuid.UUID, db: Session = Depends(get_db, scope="function"), admin: User = Depends(require_admin)
) -> dict:
    """Remove a work order that should not have been issued. Its number is not
    reused while a later one exists. Nothing about its items changes."""
    work_order = _get(db, work_order_id)
    log.warning(
        "%s rev %d deleted by %s", work_orders.label(work_order), work_order.revision, admin.username
    )
    db.delete(work_order)
    db.flush()
    return {"deleted": True}


@router.get("/{work_order_id}/pdf")
def pdf(
    work_order_id: uuid.UUID, db: Session = Depends(get_db, scope="function"), user: User = Depends(current_user)
) -> Response:
    work_order = _get(db, work_order_id)
    name = f"{work_orders.label(work_order)}-rev{work_order.revision}.pdf"
    return Response(
        content=render(db, work_order),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            # Re-issued under the same URL as the next revision: never serve an old one.
            "Cache-Control": "no-store",
        },
    )
