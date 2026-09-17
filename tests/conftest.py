from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.config import DedupeSettings, FeedConfig, Settings, load_settings
from app.db import init_db, make_engine, make_session_factory

FIXTURES = Path(__file__).parent / "fixtures"

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return load_settings()


@pytest.fixture
def dedupe_settings() -> DedupeSettings:
    return DedupeSettings(
        same_source_title_similarity=90,
        same_source_window_hours=24,
        syndication_title_similarity=90,
    )


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine(":memory:")
    init_db(engine)
    with make_session_factory(engine)() as db_session:
        yield db_session
    engine.dispose()


def make_feed(**overrides: object) -> FeedConfig:
    values: dict[str, object] = {
        "name": "Example Outlet",
        "url": "https://www.example.com/rss.xml",
        "region": "GLOBAL",
        "weight": 2,
    }
    values.update(overrides)
    return FeedConfig.model_validate(values)


def read_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()
