"""Admin user management over HTTP: CRUD, the self-lockout guard, and
password resets revoking sessions."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _create(client, username, password="a-long-password", **extra):
    return client.post("/api/users", json={
        "username": username,
        "display_name": username.title(),
        "password": password,
        **extra,
    })


def test_admin_creates_lists_and_updates_users(factory, client):
    created = _create(client, "floor1")
    assert created.status_code == 201
    assert created.json()["is_admin"] is False

    listed = client.get("/api/users").json()["users"]
    assert "floor1" in [u["username"] for u in listed]

    user_id = created.json()["id"]
    renamed = client.patch(f"/api/users/{user_id}", json={"display_name": "First Shift"})
    assert renamed.json()["display_name"] == "First Shift"

    assert _create(client, "floor1").status_code == 409  # duplicate
    assert _create(client, "weakpw", password="short").status_code == 422


def test_user_management_is_admin_only(factory, client):
    assert _create(client, "floor2").status_code == 201
    factory.db.commit()

    with TestClient(app) as floor:
        assert floor.post(
            "/api/auth/login", json={"username": "floor2", "password": "a-long-password"}
        ).status_code == 200
        assert floor.get("/api/users").status_code == 403
        assert _create(floor, "sneaky").status_code == 403


def test_admin_cannot_demote_or_deactivate_self(factory, client):
    me = client.get("/api/auth/me").json()["user"]
    for body in ({"is_admin": False}, {"is_active": False}):
        response = client.patch(f"/api/users/{me['id']}", json=body)
        assert response.status_code == 409


def test_password_reset_via_api_revokes_the_users_sessions(factory, client):
    created = _create(client, "floor3").json()
    factory.db.commit()

    with TestClient(app) as floor:
        floor.post("/api/auth/login", json={"username": "floor3", "password": "a-long-password"})
        assert floor.get("/api/auth/me").status_code == 200

        reset = client.patch(f"/api/users/{created['id']}", json={"password": "rotated-elsewhere"})
        assert reset.status_code == 200
        factory.db.commit()

        assert floor.get("/api/auth/me").status_code == 401  # old session dead
        assert floor.post(
            "/api/auth/login", json={"username": "floor3", "password": "rotated-elsewhere"}
        ).status_code == 200


def test_deactivated_user_is_rejected_on_next_request(factory, client):
    created = _create(client, "floor4").json()
    factory.db.commit()

    with TestClient(app) as floor:
        floor.post("/api/auth/login", json={"username": "floor4", "password": "a-long-password"})
        assert floor.get("/api/auth/me").status_code == 200
        client.patch(f"/api/users/{created['id']}", json={"is_active": False})
        factory.db.commit()
        assert floor.get("/api/auth/me").status_code == 401
