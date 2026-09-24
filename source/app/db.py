"""Async engine + session plumbing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings
from .models import Base

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        if url.startswith("sqlite"):
            # Make sure the parent directory exists before SQLite tries to open it.
            db_path = url.split(":///", 1)[-1]
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(url, echo=False, pool_pre_ping=True)
        if url.startswith("sqlite"):
            _apply_sqlite_pragmas(_engine)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _apply_sqlite_pragmas(engine) -> None:
    """Make SQLite behave like a server database.

    Default SQLite serialises readers against writers, so a background reaper
    pass or a second worker turns into "database is locked" under any real
    concurrency. WAL lets readers proceed during a write; busy_timeout makes the
    remaining writer-writer contention wait instead of failing immediately.

    Postgres needs none of this — see DATABASE_URL in .env.example.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # pragma: no cover - driver callback
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def _alembic_config():
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    # env.py honours this. Letting Alembic apply alembic.ini's logging config
    # here would reset the root logger to WARNING and silence the application
    # for the rest of its life, because migrations run during startup.
    config.attributes["configure_logging"] = False
    return config


def run_migrations() -> None:
    """Bring the database up to head. Synchronous — Alembic drives its own loop."""
    import logging

    # Must precede the import: Alembic logs seven "setup plugin ..." lines at
    # INFO while its registry loads, so silencing afterwards is too late.
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)

    from alembic import command

    command.upgrade(_alembic_config(), "head")


async def current_revision() -> str | None:
    """The revision this database is stamped at, or None if unmigrated.

    Goes through `run_sync` rather than touching `engine.sync_engine` directly:
    the DBAPI underneath is async, so a plain sync connection has no greenlet to
    suspend into and raises instead of connecting.
    """
    from alembic.migration import MigrationContext

    def _read(sync_connection) -> str | None:
        return MigrationContext.configure(sync_connection).get_current_revision()

    async with get_engine().connect() as connection:
        return await connection.run_sync(_read)


def head_revision() -> str | None:
    """The newest revision on disk. No database access — just reads the scripts."""
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


async def init_db() -> None:
    """Migrate to head.

    Migrations run in development and in the test suite too, not only in
    production. A migration path that is only exercised at deploy time is a
    migration path that is discovered to be broken at deploy time.

    Run in a worker thread because Alembic's async env calls `asyncio.run`,
    which cannot nest inside the loop already running here.
    """
    get_engine()
    await anyio.to_thread.run_sync(run_migrations)


async def schema_status() -> tuple[str | None, str | None]:
    """`(current, head)` revisions, for the startup check."""
    return await current_revision(), head_revision()


async def schema_drift() -> list[str]:
    """Columns the models expect that the database does not have.

    `create_all` adds missing *tables* but never missing *columns*, so adding a
    field to an existing model leaves a database that looks fine at startup and
    then throws "no such column" on the first query that touches it — an opaque
    500 far from its cause. Reporting the drift at boot turns that into one log
    line naming the fix.

    This is a diagnostic, not a migration tool. Applying the change is Alembic's
    job once there is data worth preserving.
    """
    engine = get_engine()

    def _compare(sync_conn) -> list[str]:
        inspector = inspect(sync_conn)
        existing = set(inspector.get_table_names())
        problems: list[str] = []
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                continue  # create_all handles whole tables
            actual = {column["name"] for column in inspector.get_columns(table.name)}
            missing = [c.name for c in table.columns if c.name not in actual]
            if missing:
                problems.append(f"{table.name}: {', '.join(missing)}")
        return problems

    async with engine.connect() as conn:
        return await conn.run_sync(_compare)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_scope() as session:
        yield session


async def reset_engine() -> None:
    """Test helper: drop cached engine so a new DATABASE_URL takes effect."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
