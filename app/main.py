from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, events, images, items, office, reference, reports
from app.config import get_settings

app = FastAPI(title="LSF Track", docs_url="/api/docs", openapi_url="/api/openapi.json")

app.include_router(auth.router)
app.include_router(reference.router)
app.include_router(items.router)
app.include_router(office.router)
app.include_router(images.router)
app.include_router(events.router)
app.include_router(reports.router)

_STATIC = Path(__file__).resolve().parent.parent / "static"

_CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    if get_settings().secure_cookies:
        # Only meaningful once TLS is actually in front; harmless before then.
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


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
