"""An acknowledgement means the entry is in the database.

The phone clears a queued entry the moment the server says "stored". So the
server may only say it once the transaction has committed: an answer sent
first, followed by a commit that fails (the database restarting, a dropped
connection, a full disk), would leave the phone holding a receipt for a row
that does not exist -- and nothing left in its queue to send again.

FastAPI runs the code after a dependency's `yield` -- here, the commit in
`app.db.get_db` -- *after* the response has gone out, unless the dependency is
declared with `scope="function"`. Every endpoint declares it that way; the
second test below is what keeps a new endpoint from quietly going back.
"""

from __future__ import annotations

import importlib
import pkgutil
import uuid

import sqlalchemy as sa
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import api as api_routers
from app.db import get_db
from app.main import app
from tests.conftest import hours_ago


def _move(factory, item, qty):
    return {
        "id": str(uuid.uuid4()),
        "item_id": str(item.id),
        "event_type_id": str(factory.event_type("move").id),
        "stage_id": str(factory.stage("carpentry").id),
        "station_id": str(factory.station("carpentry_1").id),
        "from_unstarted": True,
        "qty": qty,
        "occurred_at": hours_ago(1).isoformat(),
    }


def test_a_commit_that_fails_is_never_acknowledged(factory, monkeypatch):
    item = factory.item("ACK-1", 10)
    factory.db.commit()

    # Server errors come back as responses here, as they do to a phone.
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(
            "/api/auth/login", json={"username": "test", "password": "pw"}
        ).status_code == 200

        def commit_fails(self):
            self.rollback()
            raise sa.exc.OperationalError("COMMIT", {}, Exception("server closed the connection"))

        with monkeypatch.context() as patched:
            patched.setattr(Session, "commit", commit_fails)
            single = client.post("/api/events", json=_move(factory, item, 5))
            batch = client.post("/api/events/batch", json={"events": [_move(factory, item, 3)]})

    # Neither answer may claim the entry was stored. A 5xx is what the phone
    # treats as "keep it queued and try again".
    assert single.status_code == 500, single.text
    assert batch.status_code == 500, batch.text
    assert "results" not in batch.json()


def _session_dependencies(dependant):
    """Every place `get_db` is asked for beneath one route, however deep."""
    for dependency in dependant.dependencies:
        if dependency.call is get_db:
            yield dependency
        yield from _session_dependencies(dependency)


def _all_routes():
    """Every endpoint the app serves: each router module under app/api (found
    by looking, so a new one is covered the day it is added), and the few
    routes declared on the app itself."""
    for module_info in pkgutil.iter_modules(api_routers.__path__):
        module = importlib.import_module(f"app.api.{module_info.name}")
        router = getattr(module, "router", None)
        if router is not None:
            yield from router.routes
    yield from app.routes


def test_every_endpoint_commits_before_it_answers():
    late = []
    seen = 0
    for route in _all_routes():
        if not isinstance(route, APIRoute):
            continue
        for dependency in _session_dependencies(route.dependant):
            seen += 1
            if dependency.scope != "function":
                late.append(f"{sorted(route.methods)} {route.path}")
    # The walk itself must be finding the session, or it proves nothing.
    assert seen > 40
    assert not late, (
        "these endpoints would answer before their transaction commits; declare the "
        'session as Depends(get_db, scope="function"): ' + ", ".join(sorted(set(late)))
    )
