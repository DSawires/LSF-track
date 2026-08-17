#!/usr/bin/env python3
"""Operational commands.

    python manage.py migrate                 # alembic upgrade head
    python manage.py seed                    # idempotent vocabularies
    python manage.py create-user NAME [--admin]
    python manage.py demo                    # a small demo factory with history

Adding a stage needs no command here at all -- it is an INSERT into `stages` (plus
stations and a new route template version), exactly as CLAUDE.md describes. The
release screen and reports pick it up on the next reference sync.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from app.db import get_sessionmaker


def cmd_migrate(_args) -> None:
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    print("migrated to head")


def cmd_seed(_args) -> None:
    from seeds.seed import run

    with get_sessionmaker()() as db:
        run(db)
        db.commit()
    print("seeded")


def cmd_create_user(args) -> None:
    import sqlalchemy as sa

    from app.models import User
    from app.security import hash_password

    username = args.username.strip().lower()
    password = args.password or getpass.getpass(f"password for {username}: ")
    with get_sessionmaker()() as db:
        existing = db.scalars(sa.select(User).where(User.username == username)).first()
        if existing:
            print(f"{username} already exists", file=sys.stderr)
            sys.exit(1)
        db.add(
            User(
                username=username,
                display_name=args.display_name or username,
                password_hash=hash_password(password),
                is_admin=args.admin,
            )
        )
        db.commit()
    print(f"created {username}{' (admin)' if args.admin else ''}")


def cmd_set_password(args) -> None:
    import sqlalchemy as sa

    from app.models import User
    from app.security import hash_password

    username = args.username.strip().lower()
    password = args.password or getpass.getpass(f"new password for {username}: ")
    with get_sessionmaker()() as db:
        user = db.scalars(sa.select(User).where(User.username == username)).first()
        if user is None:
            print(f"{username} does not exist", file=sys.stderr)
            sys.exit(1)
        user.password_hash = hash_password(password)
        db.commit()
    print(f"password updated for {username}")


def cmd_backup(_args) -> None:
    """pg_dump the database into blob storage (S3 when configured, else disk).

    Dumps land under `backups/` next to the item photos — one bucket for
    everything. Pruning keeps the newest LSF_BACKUP_KEEP dumps; the timestamped
    names sort chronologically, so "oldest" is just the front of the list.
    """
    import gzip
    import os
    import subprocess
    import tempfile
    from datetime import datetime, timezone
    from urllib.parse import unquote, urlparse

    from app.config import get_settings
    from app.storage import get_storage

    settings = get_settings()
    url = urlparse(settings.database_url)
    if not url.scheme.startswith("postgresql"):
        print(f"backup: {url.scheme} is not backed up by pg_dump; skipping")
        return

    command = [
        "pg_dump",
        "--no-owner",
        "-h", url.hostname or "localhost",
        "-p", str(url.port or 5432),
        "-U", unquote(url.username or "postgres"),
        "-d", url.path.lstrip("/"),
    ]
    env = {**os.environ, "PGPASSWORD": unquote(url.password or "")}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    key = f"backups/lsf-{stamp}.sql.gz"

    with tempfile.TemporaryFile() as spool:
        with subprocess.Popen(command, stdout=subprocess.PIPE, env=env) as proc:
            with gzip.GzipFile(fileobj=spool, mode="wb") as gz:
                while chunk := proc.stdout.read(256 * 1024):
                    gz.write(chunk)
        # An empty or failed dump must never overwrite the retention window
        # with garbage: bail before upload, loudly.
        if proc.returncode != 0:
            print(f"backup: pg_dump exited {proc.returncode}; nothing uploaded", file=sys.stderr)
            sys.exit(1)
        size = spool.tell()
        if size < 512:
            print(f"backup: dump implausibly small ({size} bytes); nothing uploaded", file=sys.stderr)
            sys.exit(1)
        spool.seek(0)
        storage = get_storage()
        storage.put(key, spool, "application/gzip")

    existing = storage.list("backups/")
    for old in existing[: -settings.backup_keep] if settings.backup_keep > 0 else []:
        storage.delete(old)
    destination = f"s3://{settings.s3_bucket}/{settings.s3_prefix}" if settings.s3_bucket else settings.upload_dir
    print(f"backup: wrote {key} ({size // 1024}KB) to {destination}; {min(len(existing), settings.backup_keep)} kept")


def cmd_restore(args) -> None:
    """Restore a pg_dump made by `manage.py backup`.

        python manage.py restore backups/lsf-20260816-020000Z.sql.gz --reset --yes
        python manage.py restore /path/to/dump.sql.gz --reset --yes

    The source is a storage key (S3 or the uploads volume; see `--list`) or a
    local file path. `--reset` drops and recreates the public schema first,
    which is what a dump made without --clean needs to land on a non-empty
    database. Stop the app service before restoring; events written during a
    restore are lost with the schema.
    """
    import gzip
    import os
    import subprocess
    import tempfile
    from urllib.parse import unquote, urlparse

    from app.config import get_settings
    from app.storage import get_storage

    settings = get_settings()
    url = urlparse(settings.database_url)
    if not url.scheme.startswith("postgresql"):
        print(f"restore: {url.scheme} is not restored by psql; aborting", file=sys.stderr)
        sys.exit(1)

    storage = get_storage()
    if args.list:
        for key in storage.list("backups/"):
            print(key)
        return
    if not args.source:
        print("restore: give a backup key or file path (see --list)", file=sys.stderr)
        sys.exit(1)

    if not args.yes:
        print(
            "restore: this OVERWRITES the current database. Re-run with --yes "
            "(and --reset unless the schema is already empty).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Materialise the dump locally: from disk, or spooled out of storage.
    if os.path.exists(args.source):
        dump_path = args.source
        cleanup = None
    else:
        response = storage.response(args.source, "application/gzip")
        if response is None:
            print(f"restore: no such backup {args.source!r}", file=sys.stderr)
            sys.exit(1)
        spool = tempfile.NamedTemporaryFile(suffix=".sql.gz", delete=False)
        if hasattr(response, "path"):  # FileResponse from local storage
            with open(response.path, "rb") as src:
                spool.write(src.read())
        else:  # StreamingResponse from S3
            import asyncio

            async def _drain() -> None:
                async for chunk in response.body_iterator:
                    spool.write(chunk)

            asyncio.run(_drain())
        spool.close()
        dump_path = spool.name
        cleanup = spool.name

    psql_base = [
        "psql",
        "--no-psqlrc",
        "-v", "ON_ERROR_STOP=1",
        "-h", url.hostname or "localhost",
        "-p", str(url.port or 5432),
        "-U", unquote(url.username or "postgres"),
        "-d", url.path.lstrip("/"),
    ]
    env = {**os.environ, "PGPASSWORD": unquote(url.password or "")}

    try:
        if args.reset:
            subprocess.run(
                psql_base + ["-c", "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"],
                env=env,
                check=True,
            )
        with subprocess.Popen(psql_base, stdin=subprocess.PIPE, env=env) as proc:
            with gzip.open(dump_path, "rb") as gz:
                while chunk := gz.read(256 * 1024):
                    proc.stdin.write(chunk)
            proc.stdin.close()
            proc.wait()
        if proc.returncode != 0:
            print(f"restore: psql exited {proc.returncode}", file=sys.stderr)
            sys.exit(1)
    finally:
        if cleanup:
            os.unlink(cleanup)
    print(f"restore: loaded {args.source}. Restart the app service.")


def cmd_bootstrap(_args) -> None:
    """Create the admin named by LSF_ADMIN_USERNAME / LSF_ADMIN_PASSWORD.

    Idempotent and safe to run on every container start: does nothing if the
    variables are unset or the user already exists. Never updates a password --
    rotating credentials is a deliberate act, not a side effect of a restart.
    """
    import os

    import sqlalchemy as sa

    from app.models import User
    from app.security import hash_password

    import secrets

    username = os.environ.get("LSF_ADMIN_USERNAME", "admin").strip().lower()
    password = os.environ.get("LSF_ADMIN_PASSWORD", "")
    with get_sessionmaker()() as db:
        if db.scalars(sa.select(User).where(User.username == username)).first():
            print(f"bootstrap: {username} already exists")
            return
        generated = not password
        if generated:
            # Never default to a guessable password: mint one and say it once.
            password = secrets.token_urlsafe(12)
        db.add(
            User(
                username=username,
                display_name=username,
                password_hash=hash_password(password),
                is_admin=True,
            )
        )
        db.commit()
    if generated:
        print("=" * 62)
        print(f"bootstrap: created admin '{username}' with a GENERATED password:")
        print(f"bootstrap:     {password}")
        print("bootstrap: shown only this once. Sign in and note it down, or")
        print("bootstrap: set LSF_ADMIN_PASSWORD before first boot next time.")
        print("=" * 62)
    else:
        print(f"bootstrap: created admin {username}")


def cmd_demo(_args) -> None:
    from seeds.demo import run

    with get_sessionmaker()() as db:
        run(db)
        db.commit()
    print("demo data loaded (user: demo / demo)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate").set_defaults(func=cmd_migrate)
    sub.add_parser("seed").set_defaults(func=cmd_seed)

    p_user = sub.add_parser("create-user")
    p_user.add_argument("username")
    p_user.add_argument("--display-name")
    p_user.add_argument("--password", help="omit to be prompted")
    p_user.add_argument("--admin", action="store_true")
    p_user.set_defaults(func=cmd_create_user)

    p_pass = sub.add_parser("set-password")
    p_pass.add_argument("username")
    p_pass.add_argument("--password", help="omit to be prompted")
    p_pass.set_defaults(func=cmd_set_password)

    sub.add_parser("bootstrap").set_defaults(func=cmd_bootstrap)
    sub.add_parser("backup").set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore")
    p_restore.add_argument("source", nargs="?", help="backup key or local file path")
    p_restore.add_argument("--list", action="store_true", help="list available backups")
    p_restore.add_argument("--reset", action="store_true",
                           help="drop and recreate the public schema first")
    p_restore.add_argument("--yes", action="store_true",
                           help="confirm overwriting the current database")
    p_restore.set_defaults(func=cmd_restore)

    sub.add_parser("demo").set_defaults(func=cmd_demo)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
