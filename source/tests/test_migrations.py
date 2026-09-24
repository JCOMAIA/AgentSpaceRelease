"""Migrations must apply cleanly and must match the models.

The second half is the one that earns its keep. `create_all` adds tables but
never columns, which is how a model change once reached a running instance and
turned every request touching it into an opaque 500. A migration that exists but
does not describe the models reproduces exactly that failure, one deploy later.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.models import Base

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fresh_db(tmp_path):
    """A database built only by migrations, in its own process.

    Alembic drives its own event loop, so it is run as a subprocess rather than
    fought with from inside the test's loop.
    """
    db_path = tmp_path / "migrated.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            **_clean_env(),
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path.as_posix()}",
        },
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"
    return db_path


def _clean_env() -> dict[str, str]:
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith("DATABASE_URL")}
    env.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
    return env


def test_migrations_build_every_table(fresh_db):
    engine = create_engine(f"sqlite:///{fresh_db.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    expected = set(Base.metadata.tables)
    assert expected <= tables, f"missing after upgrade: {sorted(expected - tables)}"
    assert "alembic_version" in tables


def test_migrations_and_models_agree(fresh_db):
    """Fails the moment a model is edited without generating a migration."""
    engine = create_engine(f"sqlite:///{fresh_db.as_posix()}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "target_metadata": Base.metadata},
            )
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], (
        "The models and the migrations have drifted. Generate the missing "
        "migration with:\n"
        '    alembic revision --autogenerate -m "describe the change"\n'
        f"Differences: {diff}"
    )


def test_downgrade_to_base_is_reversible(tmp_path):
    """A migration you cannot undo is a migration you cannot deploy confidently."""
    db_path = tmp_path / "reversible.db"
    env = {**_clean_env(), "DATABASE_URL": f"sqlite+aiosqlite:///{db_path.as_posix()}"}

    for args in (["upgrade", "head"], ["downgrade", "base"], ["upgrade", "head"]):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr}"

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "users" in tables


async def test_an_existing_database_reports_its_revision(client):
    """The suite's own database is migrated, so it must be stamped at head."""
    from app.db import current_revision, head_revision

    current = await current_revision()
    assert current is not None, "the test database was never migrated"
    assert current == head_revision()
