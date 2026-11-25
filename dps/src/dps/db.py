"""SQLAlchemy engine and session handling."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

SQLITE_BUSY_SECONDS = 30


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    kwargs: dict = {"pool_pre_ping": True}
    backend = make_url(url).get_backend_name()
    if backend == "sqlite":
        # Several workers and the API share one SQLite file on a laptop. Without a busy wait the second writer
        # failed at once with "database is locked" and its worker died (live run of 25 Nov 2026).
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": SQLITE_BUSY_SECONDS}
    if backend == "mysql":
        kwargs["isolation_level"] = "READ COMMITTED"
        kwargs["connect_args"] = {"charset": "utf8mb4"}
    engine = create_engine(url, **kwargs)
    if backend == "sqlite" and make_url(url).database not in (None, "", ":memory:"):
        @event.listens_for(engine, "connect")
        def sqlite_concurrency(connection, _):
            # WAL: readers never wait for a writer, and one writer waits (busy_timeout) instead of failing.
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            # A fixed literal (milliseconds = SQLITE_BUSY_SECONDS * 1000): PRAGMA takes no bound parameters, and a
            # formatted SQL string is flagged by the code scan. test_speed_workers keeps the two in step.
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()
    if backend == "mysql":
        @event.listens_for(engine, "connect")
        def set_utc(connection, _):
            with connection.cursor() as cursor:
                cursor.execute("SET time_zone = '+00:00'")
    return engine


def require_snapshot_isolation(engine: Engine) -> None:
    """On SQL Server, refuses to start unless READ_COMMITTED_SNAPSHOT is on (as Core does, review GAP-11).

    Without it, readers wait on writers and concurrent uploads time out or deadlock. Other databases
    (SQLite in tests and the local stack) are not checked.
    """
    if engine.dialect.name != "mssql":
        return
    with engine.connect() as conn:
        on = conn.execute(text(
            "SELECT is_read_committed_snapshot_on FROM sys.databases WHERE name = DB_NAME()")).scalar()
        name = conn.execute(text("SELECT DB_NAME()")).scalar()
    if not on:
        raise RuntimeError(
            f"READ_COMMITTED_SNAPSHOT is off for database {name}. Ask a DBA to run: "
            f"ALTER DATABASE [{name}] SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE; then start DPS again.")


class Database:
    """Holds the engine and session factory for one app instance."""

    def __init__(self, url: str) -> None:
        self.engine = make_engine(url)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def session(self) -> Iterator[Session]:
        with self.session_factory() as s:
            yield s
