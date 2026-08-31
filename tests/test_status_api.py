"""The admin status page: account activity, and the banner it broadcasts.

Two properties matter beyond the happy path. The activity figures are counted
from the log rather than stored, so they must stay correct when a stage is
added at runtime -- the aggregation is per-account and must not acquire a
per-stage assumption. And the banner is append-only: setting it twice leaves
two rows and shows the newer one.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.main import app
from app.models import StatusBanner

from tests.conftest import hours_ago


def _floor_user(client, factory, username="floor", password="a-long-password"):
    """A non-admin account, created through the API the way an admin would."""
    created = client.post("/api/users", json={
        "username": username,
        "display_name": username.title(),
        "password": password,
    })
    assert created.status_code == 201
    factory.db.commit()
    return created.json()


def test_last_login_is_stamped_on_sign_in(factory, client):
    """The one figure on the page the event log cannot answer."""
    rows = client.get("/api/status/users").json()["users"]
    me = next(r for r in rows if r["username"] == "test")
    # The `client` fixture signed in, so the admin has a login time.
    assert me["last_login_at"] is not None

    created = _floor_user(client, factory)
    fresh = next(
        r for r in client.get("/api/status/users").json()["users"]
        if r["id"] == created["id"]
    )
    assert fresh["last_login_at"] is None  # created, never signed in

    with TestClient(app) as floor:
        floor.post("/api/auth/login", json={"username": "floor", "password": "a-long-password"})

    after = next(
        r for r in client.get("/api/status/users").json()["users"]
        if r["id"] == created["id"]
    )
    assert after["last_login_at"] is not None


def test_activity_counts_events_credited_to_each_user(factory, client, world):
    _, _, item = world
    other = _floor_user(client, factory, "floor-a")

    # Two entries credited to the floor user, one to the admin.
    factory.log(item, 10, "queued", 5, at=hours_ago(3), user_id=other["id"])
    factory.log(item, 10, "in_progress", 5, at=hours_ago(1), user_id=other["id"])
    factory.log(item, 10, "completed", 5, at=hours_ago(0.5))
    factory.db.commit()

    rows = {r["username"]: r for r in client.get("/api/status/users").json()["users"]}
    assert rows["floor-a"]["actions"] == 2
    assert rows["floor-a"]["last_event_at"] is not None
    # Admin: the one entry above. Creating an item is not an entry -- it
    # writes no event -- so it does not pad anyone's count.
    assert rows["test"]["actions"] == 1
    # An account with no entries reads as zero, not as missing.
    idle = _floor_user(client, factory, "floor-idle")
    idle_row = next(
        r for r in client.get("/api/status/users").json()["users"] if r["id"] == idle["id"]
    )
    assert idle_row["actions"] == 0
    assert idle_row["last_event_at"] is None


def test_activity_survives_a_stage_added_at_runtime(factory, client, world):
    """Adding a stage is a data change, and must not touch this aggregation."""
    _, _, item = world
    factory.log(item, 10, "queued", 5, at=hours_ago(2))
    factory.db.commit()
    before = client.get("/api/status/users").json()["users"]

    factory.add_stage("glass", sort=45)
    factory.db.commit()

    assert client.get("/api/status/users").json()["users"] == before


def test_status_page_is_admin_only(factory, client):
    _floor_user(client, factory)

    with TestClient(app) as floor:
        floor.post("/api/auth/login", json={"username": "floor", "password": "a-long-password"})
        assert floor.get("/api/status/users").status_code == 403
        assert floor.post("/api/status/banner", json={"color": "red"}).status_code == 403
        # Reading the banner is not admin-only: the floor is who it is for.
        assert floor.get("/api/status/banner").status_code == 200


def test_banner_defaults_to_neutral_and_silent(factory, client):
    banner = client.get("/api/status/banner").json()
    assert banner == {"color": "neutral", "message": "", "set_at": None, "set_by": None}
    # And it rides along in the offline cache payload.
    assert client.get("/api/reference").json()["banner"] == banner


def test_setting_the_banner_appends_and_the_newest_wins(factory, client):
    first = client.post(
        "/api/status/banner", json={"color": "red", "message": "Paint booth 2 down"}
    )
    assert first.status_code == 200
    assert first.json()["color"] == "red"
    assert first.json()["set_by"] == "Test Engineer"

    client.post("/api/status/banner", json={"color": "green", "message": ""})

    live = client.get("/api/status/banner").json()
    assert live["color"] == "green"
    assert live["message"] == ""
    assert client.get("/api/reference").json()["banner"]["color"] == "green"

    # Nothing was overwritten: both statements are still in the record.
    rows = factory.db.scalars(sa.select(StatusBanner).order_by(StatusBanner.set_at)).all()
    assert [r.color for r in rows] == ["red", "green"]


def test_banner_rejects_a_colour_the_ui_cannot_paint(factory, client):
    assert client.post("/api/status/banner", json={"color": "purple"}).status_code == 422
    assert client.post(
        "/api/status/banner", json={"color": "red", "message": "x" * 201}
    ).status_code == 422
