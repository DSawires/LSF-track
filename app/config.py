from __future__ import annotations

import logging
import os
import secrets as _secrets
from dataclasses import dataclass
from functools import lru_cache

log = logging.getLogger(__name__)

# Placeholder values that have appeared in this repo's examples or that people
# reach for reflexively. Booting with one of these as the session-signing key
# would let anyone who has read the repo forge an admin session, so it is a hard
# error, not a warning.
_KNOWN_WEAK_KEYS = {
    "change-me",
    "changeme",
    "secret",
    "dev-only-insecure-key",
    "dev-only-change-me",
}


def _secret_key() -> str:
    value = os.environ.get("LSF_SECRET_KEY", "").strip()
    if value.lower() in _KNOWN_WEAK_KEYS:
        raise RuntimeError(
            "LSF_SECRET_KEY is set to a known placeholder value. Generate a real "
            "one: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    if not value:
        log.warning(
            "LSF_SECRET_KEY is not set; using a random ephemeral key. Sessions "
            "will not survive a restart. Set a real key for production."
        )
        return _secrets.token_urlsafe(48)
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: str
    session_max_age_days: int
    secure_cookies: bool
    max_device_ahead_seconds: int
    max_sync_lag_days: int

    @property
    def session_max_age_seconds(self) -> int:
        return self.session_max_age_days * 24 * 3600


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "LSF_DATABASE_URL", "postgresql+psycopg://lsf:lsf@localhost:5432/lsf_track"
        ),
        secret_key=_secret_key(),
        session_max_age_days=_int("LSF_SESSION_MAX_AGE_DAYS", 30),
        secure_cookies=_bool("LSF_SECURE_COOKIES", True),
        max_device_ahead_seconds=_int("LSF_MAX_DEVICE_AHEAD_SECONDS", 3600),
        max_sync_lag_days=_int("LSF_MAX_SYNC_LAG_DAYS", 14),
    )
