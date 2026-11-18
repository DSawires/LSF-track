"""Project import: a spreadsheet in, projects and items out.

Two calls, one file. `commit=false` (the default) reads and checks the file
and says what it would create, so the office sees the plan -- and every error,
by row -- before anything touches the floor. `commit=true` does it, and only if
the whole file is clean. See app/services/imports.py for the format.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user
from app.models import User
from app.services.items import ItemSetupError
from app.services.imports import (
    MAX_BYTES,
    ImportFileError,
    commit_import,
    plan_import,
    read_pictures,
    read_rows,
    template_csv,
)

router = APIRouter(prefix="/api/import", tags=["import"])


@router.get("/template")
def template(user: User = Depends(current_user)) -> Response:
    return Response(
        content=template_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="lsf-import-template.csv"'},
    )


@router.post("")
def import_file(
    file: UploadFile = File(...),
    commit: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    content = file.file.read(MAX_BYTES + 1)
    try:
        rows = read_rows(file.filename or "", content)
    except ImportFileError as exc:
        raise HTTPException(422, {"reason": str(exc), "errors": [{"row": None, "message": str(exc)}]})

    pictures, picture_notes = read_pictures(content)
    plan = plan_import(db, rows, pictures, picture_notes)
    body = plan.as_dict()
    if not commit:
        return {**body, "committed": False}
    if plan.errors:
        raise HTTPException(
            422, {"reason": f"{len(plan.errors)} problem(s) in the file; nothing was imported", **body}
        )
    try:
        commit_import(db, plan, user)
    except ItemSetupError as exc:
        # Something changed between the check and the write. Nothing was
        # imported; the office previews again and sees what.
        raise HTTPException(409, {"reason": str(exc), **body})
    return {**body, "committed": True}
