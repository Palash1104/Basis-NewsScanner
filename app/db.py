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


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
