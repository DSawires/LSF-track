from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


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
        secret_key=os.environ.get("LSF_SECRET_KEY", "dev-only-insecure-key"),
        session_max_age_days=_int("LSF_SESSION_MAX_AGE_DAYS", 30),
        secure_cookies=_bool("LSF_SECURE_COOKIES", True),
        max_device_ahead_seconds=_int("LSF_MAX_DEVICE_AHEAD_SECONDS", 3600),
        max_sync_lag_days=_int("LSF_MAX_SYNC_LAG_DAYS", 14),
    )
