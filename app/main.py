from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
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

    @lru_cache
    def _static_hash() -> str:
        digest = hashlib.sha256()
        for path in sorted(_STATIC.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(_STATIC)).encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()[:12]

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> Response:
        # Served from the root so its scope covers the whole app. The shell
        # cache name carries a hash of static/, so any shipped frontend change
        # busts every phone's cache without a hand-bumped version constant.
        source = (_STATIC / "sw.js").read_text(encoding="utf-8")
        return Response(
            source.replace("__STATIC_HASH__", _static_hash()),
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(
            _STATIC / "manifest.webmanifest", media_type="application/manifest+json"
        )
