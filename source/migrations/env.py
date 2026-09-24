"""Alembic environment.

Two things here are not boilerplate:

* The URL comes from the application's own `Settings`, never from alembic.ini.
  Two sources of truth for "which database" is how you migrate the wrong one.
* `render_as_batch` is on for SQLite, which cannot ALTER most things and needs
  Alembic to rebuild the table instead. Without it, development migrations pass
  on Postgres and fail on a laptop.
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.models import Base  # noqa: E402

config = context.config

# Only configure logging for the CLI. When the application runs migrations at
# startup it sets `configure_logging` false, because applying alembic.ini here
# would reset the root logger to WARNING and silence the server afterwards.
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def database_url() -> str:
    return get_settings().database_url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER a column; batch mode rebuilds the table instead.
        render_as_batch=connection.dialect.name == "sqlite",
        # Catch a column whose type changed, not just added and dropped ones.
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting — for reviewing a change first."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": database_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
