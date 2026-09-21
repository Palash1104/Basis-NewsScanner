"""Engine and session setup for the SQLite database."""

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

log = logging.getLogger(__name__)


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
        # The rate limiter writes from its own session while the pipeline holds one, and a
        # second process (the scheduler, a manual run) may be writing too. Without this,
        # SQLite fails such a write immediately with "database is locked".
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    return engine


def make_read_only_engine(db_path: Path | str) -> Engine:
    """An engine that cannot write, for the web UI (SPEC 11).

    The scheduled pipeline writes to this file every three hours, so the reader beside it:

    - sets `query_only`, so SQLite refuses every write and a bug in a page can never touch
      the data;
    - does **not** set `journal_mode`, unlike `make_engine`: that statement takes a write
      lock, which is the one thing a reader must never do here. The file is already WAL, and
      WAL readers never block the writer or each other;
    - keeps a short `busy_timeout`, since a page should fail fast rather than hang.
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"no database at {path}: run `newsdesk run` first")
    engine = create_engine(f"sqlite:///{path.as_posix()}")

    @event.listens_for(engine, "connect")
    def _read_only_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA query_only=ON")
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
        # Added 2026-09-21 with temperature 0 and a fixed seed: every stored LLM output
        # records the sampling settings it was produced with.
        "temperature": "FLOAT",
        "seed": "INTEGER",
    },
    "events": {
        # Added with event prompt v2, after the first extractions had been stored.
        "policy_actor": "VARCHAR(64)",
        "temperature": "FLOAT",
        "seed": "INTEGER",
    },
    "impacts": {
        # Layer B's provenance; null on origin=playbook.
        "model": "VARCHAR(64)",
        "prompt_version": "VARCHAR(32)",
        "temperature": "FLOAT",
        "seed": "INTEGER",
    },
    "rule_disagreements": {
        "temperature": "FLOAT",
        "seed": "INTEGER",
    },
    "runs": {
        # Added 2026-09-21 for `newsdesk health`.
        "llm_impact_calls": "INTEGER NOT NULL DEFAULT 0",
        "llm_impact_declines": "INTEGER NOT NULL DEFAULT 0",
    },
    "ticker_checks": {
        # Added in Phase 4: trading-day counting needs each exchange's time zone.
        "timezone": "VARCHAR(48)",
    },
}


# Full-text search over stories, for the web UI (SPEC 11). An external-content FTS5 table:
# it indexes `stories` without copying it, and the triggers keep the index in step with
# every write the pipeline makes. Created here, never by the web app, which cannot write.
SEARCH_TABLE = "stories_fts"
_SEARCH_SCHEMA = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {SEARCH_TABLE} USING fts5("
    "headline, summary, content='stories', content_rowid='id', tokenize='unicode61')",
    f"""CREATE TRIGGER IF NOT EXISTS {SEARCH_TABLE}_insert AFTER INSERT ON stories BEGIN
        INSERT INTO {SEARCH_TABLE}(rowid, headline, summary)
        VALUES (new.id, new.headline, new.summary);
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS {SEARCH_TABLE}_delete AFTER DELETE ON stories BEGIN
        INSERT INTO {SEARCH_TABLE}({SEARCH_TABLE}, rowid, headline, summary)
        VALUES ('delete', old.id, old.headline, old.summary);
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS {SEARCH_TABLE}_update AFTER UPDATE ON stories BEGIN
        INSERT INTO {SEARCH_TABLE}({SEARCH_TABLE}, rowid, headline, summary)
        VALUES ('delete', old.id, old.headline, old.summary);
        INSERT INTO {SEARCH_TABLE}(rowid, headline, summary)
        VALUES (new.id, new.headline, new.summary);
    END""",
)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for table, columns in ADDED_COLUMNS.items():
            existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

        _build_search_index(connection)


def _build_search_index(connection: Connection) -> None:
    """Create the search index if it isn't there, and fill it the first time.

    Search is a convenience for the web UI, so a database it can't index (an old one missing
    a column, say) logs a warning and carries on: a run must never fail over it.
    """
    try:
        fresh = not connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SEARCH_TABLE,)
        ).first()
        for statement in _SEARCH_SCHEMA:
            connection.exec_driver_sql(statement)
        if fresh:
            # Index the stories that existed before the table did. Only on creation: after
            # that the triggers keep it current, and a rebuild would be wasted work.
            connection.exec_driver_sql(
                f"INSERT INTO {SEARCH_TABLE}({SEARCH_TABLE}) VALUES ('rebuild')"
            )
    except OperationalError as exc:
        log.warning("search index unavailable (the web UI's search will be empty): %s", exc)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_read_only_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Sessions for the web UI: no autoflush, so reading never tries to write."""
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
