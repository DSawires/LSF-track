from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, events, items, office, reference, reports

app = FastAPI(title="LSF Track", docs_url="/api/docs", openapi_url="/api/openapi.json")

app.include_router(auth.router)
app.include_router(reference.router)
app.include_router(items.router)
app.include_router(office.router)
app.include_router(events.router)
app.include_router(reports.router)

_STATIC = Path(__file__).resolve().parent.parent / "static"


@app.get("/health")
def health() -> dict:
    return {"ok": True}


if _STATIC.is_dir():  # pragma: no branch - static ships with the repo
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        # Served from the root so its scope covers the whole app.
        return FileResponse(_STATIC / "sw.js", media_type="application/javascript")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(
            _STATIC / "manifest.webmanifest", media_type="application/manifest+json"
        )
