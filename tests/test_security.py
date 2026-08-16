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


def test_placeholder_secret_key_is_refused():
    from app.config import get_settings

    original = os.environ.get("LSF_SECRET_KEY")
    try:
        os.environ["LSF_SECRET_KEY"] = "change-me"
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="placeholder"):
            get_settings()
    finally:
        os.environ["LSF_SECRET_KEY"] = original or ""
        get_settings.cache_clear()


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
