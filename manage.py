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
    sub.add_parser("demo").set_defaults(func=cmd_demo)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
