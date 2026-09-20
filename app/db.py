"""Engine and session setup for the SQLite database."""

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base


def make_engine(db_path: Path | str) -> Engine:
    """Create an engine for a file path, or ":memory:" for tests."""
    if str(db_path) == ":memory:":
        url = "sqlite:///:memory:"
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


# Columns added after their table was first created. create_all() only creates missing tables,
# so these are added with ALTER TABLE when an existing database lacks them.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "articles": {
        "non_news": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "stories": {
        "model": "VARCHAR(64)",
        "summary_pending": "BOOLEAN NOT NULL DEFAULT 0",
        "event_pending": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "events": {
        # Added with event prompt v2, after the first extractions had been stored.
        "policy_actor": "VARCHAR(64)",
    },
    "ticker_checks": {
        # Added in Phase 4: trading-day counting needs each exchange's time zone.
        "timezone": "VARCHAR(48)",
    },
}


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for table, columns in ADDED_COLUMNS.items():
            existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
