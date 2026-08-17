"""Seed and demo-data safety: idempotent, no-clobber, and demo data can never
mix into a production database."""

from __future__ import annotations

import sqlalchemy as sa

from app.models import Project, User
from seeds.demo import run as run_demo


def test_demo_refuses_a_database_with_real_projects(factory):
    """factory's fixture already created a real project (PRJ); loading demo
    data on top must be a refusal, not a merge."""
    run_demo(factory.db)
    factory.db.flush()

    assert (
        factory.db.scalars(sa.select(User).where(User.username == "demo")).first()
        is None
    )
    codes = {p.code for p in factory.db.scalars(sa.select(Project))}
    assert "HOTEL-A" not in codes


def test_demo_user_is_not_admin(seeded):
    run_demo(seeded)
    seeded.flush()
    demo_user = seeded.scalars(sa.select(User).where(User.username == "demo")).one()
    assert demo_user.is_admin is False
