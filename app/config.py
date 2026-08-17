from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

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


_GENERATE_HINT = 'python -c "import secrets; print(secrets.token_urlsafe(48))"'
_MIN_KEY_LENGTH = 32


def require_session_key() -> str:
    """The session-signing key, validated where it is actually used.

    Only the web app signs sessions. `manage.py backup`, `restore`, `migrate`
    and friends talk to the database and nothing else, and the backup job runs
    in its own container that never serves a request -- failing the nightly
    dump over a key it will never touch loses real data protection to guard
    nothing. The web app still refuses to boot without a good key: app.main's
    lifespan calls this before the first request.
    """
    value = get_settings().secret_key
    if value.lower() in _KNOWN_WEAK_KEYS:
        raise RuntimeError(
            "LSF_SECRET_KEY is set to a known placeholder value. Generate a real "
            f"one: {_GENERATE_HINT}"
        )
    if not value:
        # Refuse to boot rather than mint an ephemeral key. An ephemeral key
        # looks fine until the first redeploy, which then logs every engineer
        # out mid-shift -- and a signed-out phone cannot drain its offline
        # queue. With more than one worker it is worse: each process would hold
        # a different key and sessions would fail at random. The Docker
        # entrypoint generates and persists a key on first boot, so this error
        # is only ever seen on a misconfigured bare-metal run.
        raise RuntimeError(
            "LSF_SECRET_KEY is not set. Sessions must survive restarts (an "
            "engineer signed out mid-shift cannot sync their offline queue), so "
            f"there is no ephemeral fallback. Generate one: {_GENERATE_HINT}"
        )
    if len(value) < _MIN_KEY_LENGTH:
        raise RuntimeError(
            f"LSF_SECRET_KEY is shorter than {_MIN_KEY_LENGTH} characters; a "
            f"guessable key lets anyone forge an admin session. Generate a real "
            f"one: {_GENERATE_HINT}"
        )
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
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from None


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: str
    session_max_age_days: int
    secure_cookies: bool
    domain: str
    max_device_ahead_seconds: int
    max_sync_lag_days: int
    upload_dir: str
    max_upload_bytes: int
    s3_bucket: str
    s3_prefix: str
    backup_keep: int

    @property
    def session_max_age_seconds(self) -> int:
        return self.session_max_age_days * 24 * 3600


@lru_cache
def get_settings() -> Settings:
    settings = Settings(
        database_url=os.environ.get(
            "LSF_DATABASE_URL", "postgresql+psycopg://lsf:lsf@localhost:5432/lsf_track"
        ),
        # Raw here, checked by require_session_key() at the point of use, so a
        # database-only command is not held hostage to a session concern.
        secret_key=os.environ.get("LSF_SECRET_KEY", "").strip(),
        session_max_age_days=_int("LSF_SESSION_MAX_AGE_DAYS", 30),
        secure_cookies=_bool("LSF_SECURE_COOKIES", True),
        domain=os.environ.get("LSF_DOMAIN", "").strip(),
        max_device_ahead_seconds=_int("LSF_MAX_DEVICE_AHEAD_SECONDS", 3600),
        max_sync_lag_days=_int("LSF_MAX_SYNC_LAG_DAYS", 14),
        upload_dir=os.environ.get("LSF_UPLOAD_DIR", "data/uploads"),
        max_upload_bytes=_int("LSF_MAX_UPLOAD_BYTES", 10 * 1024 * 1024),
        s3_bucket=os.environ.get("LSF_S3_BUCKET", "").strip(),
        s3_prefix=os.environ.get("LSF_S3_PREFIX", "").strip(),
        backup_keep=_int("LSF_BACKUP_KEEP", 30),
    )
    if settings.domain and not settings.secure_cookies:
        # A real domain means real TLS via the bundled proxy; shipping
        # non-Secure session cookies alongside it would be a silent downgrade
        # that nothing else ever surfaces.
        raise RuntimeError(
            "LSF_DOMAIN is set (TLS deployment) but LSF_SECURE_COOKIES is false. "
            "Set LSF_SECURE_COOKIES=true, or unset LSF_DOMAIN for plain-HTTP use."
        )
    return settings
