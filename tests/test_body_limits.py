"""The app decides how much of a request it will read before it reads it.

FastAPI parses a body before the endpoint's session check runs, so the limit
has to sit in front of both -- see app/limits.py.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient

from app import limits
from app.main import app
from tests.conftest import hours_ago


def _entry(factory, item):
    return {
        "id": str(uuid.uuid4()),
        "item_id": str(item.id),
        "event_type_id": str(factory.event_type("move").id),
        "stage_id": str(factory.stage("carpentry").id),
        "station_id": str(factory.station("carpentry_1").id),
        "from_unstarted": True,
        "qty": 1,
        "occurred_at": hours_ago(1).isoformat(),
        "note": "n" * 400,
    }


def test_a_signed_out_client_cannot_make_the_server_read_a_large_body(factory):
    factory.db.commit()
    with TestClient(app) as anonymous:
        huge = b"[" + b" " * (3 * 1024 * 1024) + b"]"
        for path, kwargs in (
            ("/api/events/batch", {"content": huge, "headers": {"content-type": "application/json"}}),
            ("/api/import", {"files": {"file": ("a.xlsx", huge)}}),
            ("/api/auth/login", {"content": huge, "headers": {"content-type": "application/json"}}),
        ):
            refused = anonymous.post(path, **kwargs)
            assert refused.status_code == 413, (path, refused.status_code)
            assert "too large" in refused.json()["detail"]


def test_a_phone_with_an_expired_session_still_gets_its_401(world):
    """A full queue from a signed-out phone is told to sign in -- a 413 here
    would read as "this batch is the problem"."""
    factory, _stages, item = world
    with TestClient(app) as anonymous:
        body = {"events": [_entry(factory, item) for _ in range(500)]}
        answer = anonymous.post("/api/events/batch", json=body)
        assert answer.status_code == 401, answer.text
        # A cookie nobody here signed is no session either.
        anonymous.cookies.set("lsf_session", "forged.value.here")
        assert anonymous.post("/api/events/batch", json=body).status_code == 401
        huge = b"[" + b" " * (3 * 1024 * 1024) + b"]"
        assert anonymous.post(
            "/api/events/batch", content=huge, headers={"content-type": "application/json"}
        ).status_code == 413


def test_a_signed_in_client_has_room_for_everything_the_app_sends(client, world, monkeypatch):
    factory, _stages, item = world
    # Far more than the signed-out allowance, and nothing like the limit.
    padded = {"events": [], "padding": "x" * (3 * 1024 * 1024)}
    assert client.post("/api/events/batch", json=padded).status_code == 200

    # The limit itself, brought down to where a test can reach it.
    monkeypatch.setattr(limits, "JSON_BYTES", 2 * 1024 * 1024)
    assert client.post("/api/events/batch", json=padded).status_code == 413
    assert client.post("/api/events/batch", json={"events": []}).status_code == 200

    # A file: the endpoint's own limit and its own words still answer first.
    too_big_photo = b"\x89PNG\r\n\x1a\n" + b"0" * (11 * 1024 * 1024)
    photo = client.post(
        f"/api/items/{item.id}/images", files={"file": ("p.png", too_big_photo, "image/png")}
    )
    assert photo.status_code in (413, 422), photo.status_code
    assert "too large; the limit here" not in photo.text


def test_a_body_sent_without_saying_its_length_is_cut_off_where_the_limit_is(factory):
    """No Content-Length (a chunked upload): the bytes are counted as they
    arrive, and the server stops asking for more."""
    factory.db.commit()
    megabyte = b" " * (1024 * 1024)
    asked = 0
    answer = {}

    async def receive():
        nonlocal asked
        asked += 1
        return {"type": "http.request", "body": megabyte, "more_body": asked < 100}

    async def send(message):
        if message["type"] == "http.response.start":
            answer["status"] = message["status"]

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "path": "/api/events/batch", "raw_path": b"/api/events/batch", "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"host", b"testserver")],
        "client": ("203.0.113.9", 1), "server": ("testserver", 80), "scheme": "http",
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    assert answer["status"] == 413
    assert asked <= 3   # of the hundred megabytes on offer
