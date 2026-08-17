from __future__ import annotations

import hashlib
import logging
import logging.config
import os
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import auth, events, images, items, office, reference, reports, status, users
from app.config import get_settings, require_session_key
from app.db import dispose_engine, get_engine


def _configure_logging() -> None:
    """Give app loggers a real handler.

    Without this, everything logged under `app.*` falls through to Python's
    lastResort handler: WARNING-and-above only, no timestamps, and INFO --
    including the security-relevant records -- dropped silently.
    """
    level = os.environ.get("LSF_LOG_LEVEL", "INFO").upper()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                }
            },
            "handlers": {
                "stderr": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                }
            },
            "root": {"handlers": ["stderr"], "level": level},
            # uvicorn configures its own loggers; leave them be.
            "loggers": {
                "uvicorn": {"level": level, "propagate": True},
            },
        }
    )


_configure_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Fail at boot, not on the first request: a bad LSF_SECRET_KEY or an
    # inconsistent TLS/cookie combination must stop the deploy while someone
    # is still looking at it. This is the web app, so the key is required --
    # database-only commands validate nothing and need nothing.
    get_settings()
    require_session_key()
    yield
    dispose_engine()


app = FastAPI(
    title="Life Style Track",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=_lifespan,
)

app.include_router(auth.router)
app.include_router(reference.router)
app.include_router(items.router)
app.include_router(office.router)
app.include_router(images.router)
app.include_router(events.router)
app.include_router(reports.router)
app.include_router(users.router)
app.include_router(status.router)

_STATIC = Path(__file__).resolve().parent.parent / "static"

_CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    # A stack trace in the log, a usable JSON body for the client -- never a
    # bare 500 with an HTML error page the PWA cannot parse.
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "internal server error"}, status_code=500)


@app.middleware("http")
async def csrf_origin_check(request: Request, call_next):
    # SameSite=Lax on the session cookie is the primary CSRF defence; this is
    # the second lock on the same door. Browsers always send Origin on
    # cross-site state-changing requests, so a mismatched Origin is an attack
    # (or a proxy misconfiguration worth failing loudly on). Requests without
    # an Origin header -- curl, the test client, same-origin GETs -- pass.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin:
            expected = f"{request.url.scheme}://{request.url.netloc}"
            if urlsplit(origin).scheme != request.url.scheme or urlsplit(origin).netloc != request.url.netloc:
                return JSONResponse(
                    {"detail": f"cross-origin request blocked (origin {origin}, expected {expected})"},
                    status_code=403,
                )
    return await call_next(request)


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
def health() -> Response:
    """Deep enough to mean something: a pod with a dead database is not healthy."""
    try:
        with get_engine().connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception:
        return JSONResponse({"ok": False, "database": "unreachable"}, status_code=503)
    migration = None
    try:
        with get_engine().connect() as conn:
            migration = conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar()
    except Exception:
        # No alembic_version table (e.g. a test database built by create_all).
        pass
    return JSONResponse({"ok": True, "migration": migration})


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
