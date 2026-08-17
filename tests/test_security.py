from __future__ import annotations

import os

import pytest

from app.throttle import LoginThrottle


def test_login_lockout_after_repeated_failures(client, factory):
    factory.db.commit()
    for _ in range(5):
        response = client.post(
            "/api/auth/login", json={"username": "test", "password": "wrong"}
        )
        assert response.status_code == 401

    locked = client.post("/api/auth/login", json={"username": "test", "password": "wrong"})
    assert locked.status_code == 429
    assert "Retry-After" in locked.headers

    # The right password does not bypass the lockout window.
    still = client.post("/api/auth/login", json={"username": "test", "password": "pw"})
    assert still.status_code == 429


def test_success_clears_only_that_username():
    throttle = LoginThrottle(max_per_username=2, max_per_ip=25)
    throttle.record_failure("alice", "10.0.0.1")
    throttle.record_failure("alice", "10.0.0.1")
    throttle.record_failure("bob", "10.0.0.1")
    throttle.record_failure("bob", "10.0.0.1")
    assert throttle.retry_after("alice", "10.0.0.9") > 0

    throttle.record_success("alice")
    assert throttle.retry_after("alice", "10.0.0.9") == 0
    assert throttle.retry_after("bob", "10.0.0.9") > 0  # untouched


def test_ip_budget_is_shared_across_usernames():
    throttle = LoginThrottle(max_per_username=100, max_per_ip=3)
    for i in range(3):
        throttle.record_failure(f"user{i}", "203.0.113.7")
    assert throttle.retry_after("someone-new", "203.0.113.7") > 0
    assert throttle.retry_after("someone-new", "203.0.113.8") == 0


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("change-me", "placeholder"),
        ("", "not set"),
        ("shortkey", "shorter"),
    ],
)
def test_bad_secret_keys_are_refused(value, match):
    from app.config import get_settings, require_session_key

    original = os.environ.get("LSF_SECRET_KEY")
    try:
        os.environ["LSF_SECRET_KEY"] = value
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match=match):
            require_session_key()
    finally:
        os.environ["LSF_SECRET_KEY"] = original or ""
        get_settings.cache_clear()


def test_database_only_commands_do_not_need_a_session_key():
    """The nightly backup runs in its own container, never serves a request,
    and used to die on this check -- losing real data protection to guard a
    key it does not touch. Settings must load; the web app's own refusal to
    boot without a key lives in app.main's lifespan and is unaffected."""
    from app.config import get_settings

    original = os.environ.get("LSF_SECRET_KEY")
    try:
        os.environ["LSF_SECRET_KEY"] = ""
        get_settings.cache_clear()
        assert get_settings().database_url  # no RuntimeError
    finally:
        os.environ["LSF_SECRET_KEY"] = original or ""
        get_settings.cache_clear()


def test_web_app_still_refuses_to_boot_without_a_key():
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    original = os.environ.get("LSF_SECRET_KEY")
    try:
        os.environ["LSF_SECRET_KEY"] = ""
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="not set"):
            with TestClient(app):
                pass
    finally:
        os.environ["LSF_SECRET_KEY"] = original or ""
        get_settings.cache_clear()


def test_tls_domain_with_insecure_cookies_is_refused():
    from app.config import get_settings

    original = os.environ.get("LSF_DOMAIN")
    try:
        os.environ["LSF_DOMAIN"] = "track.example.com"
        # conftest sets LSF_SECURE_COOKIES=false
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="LSF_SECURE_COOKIES"):
            get_settings()
    finally:
        if original is None:
            os.environ.pop("LSF_DOMAIN", None)
        else:
            os.environ["LSF_DOMAIN"] = original
        get_settings.cache_clear()


def test_password_change_revokes_existing_sessions(client, factory):
    from app.security import hash_password

    assert client.get("/api/auth/me").status_code == 200

    factory.user.password_hash = hash_password("rotated")
    factory.db.commit()

    # The old cookie dies on its next request; signing in with the new
    # password works.
    assert client.get("/api/auth/me").status_code == 401
    response = client.post(
        "/api/auth/login", json={"username": "test", "password": "rotated"}
    )
    assert response.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_session_cookie_attributes(factory):
    from fastapi.testclient import TestClient

    from app.main import app

    factory.db.commit()
    with TestClient(app) as anon:
        response = anon.post(
            "/api/auth/login", json={"username": "test", "password": "pw"}
        )
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "Max-Age" in cookie


def test_cross_origin_writes_are_blocked(world, client):
    factory, _route, item = world
    response = client.post(
        "/api/projects",
        json={"code": "EVIL", "name": "csrf"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    # Same-origin (and origin-less curl/test traffic) passes.
    ok = client.post(
        "/api/projects",
        json={"code": "FINE", "name": "legit"},
        headers={"Origin": str(client.base_url)},
    )
    assert ok.status_code == 201


def test_events_record_who_submitted_alongside_who_is_credited(factory):
    """user_id is the engineer who saw the work (client-claimed, for shared
    phones); submitted_by_user_id is always the authenticated session."""
    from app.models import User
    from app.security import hash_password

    other = User(
        username="floor2",
        display_name="Second Engineer",
        password_hash=hash_password("pw2"),
    )
    factory.db.add(other)
    factory.db.flush()

    route = factory.route("r", ["carpentry"])
    item = factory.item("SEC-1", 5, route)
    event = factory.log(item, 10, "queued", 5, user_id=other.id)

    assert event.user_id == other.id  # credited to who logged it on the floor
    assert event.submitted_by_user_id == factory.user.id  # posted by this session


def test_security_headers_present(client, factory):
    factory.db.commit()
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_batch_size_is_capped(world, client):
    factory, _route, item = world
    import uuid as _uuid
    from datetime import datetime, timezone

    event = {
        "id": str(_uuid.uuid4()),
        "item_id": str(item.id),
        "event_type_id": str(_uuid.uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    response = client.post("/api/events/batch", json={"events": [event] * 501})
    assert response.status_code == 422
