from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.models import Article
from tests.conftest import NOW


def _article(url: str, published_at: datetime = NOW) -> Article:
    return Article(
        url=url,
        source_name="Outlet",
        source_region="GLOBAL",
        source_weight=2,
        title="Title",
        snippet="",
        published_at=published_at,
        fetched_at=NOW,
    )


def test_datetimes_round_trip_as_aware_utc(session: Session) -> None:
    article = _article("https://example.com/tz", datetime(2026, 9, 16, 15, 30, tzinfo=UTC))
    session.add(article)
    session.commit()
    session.expire_all()
    loaded = session.get(Article, article.id)
    assert loaded is not None
    assert loaded.published_at == datetime(2026, 9, 16, 15, 30, tzinfo=UTC)
    assert loaded.published_at.tzinfo is not None


def test_naive_datetimes_rejected(session: Session) -> None:
    session.add(_article("https://example.com/naive", datetime(2026, 9, 16, 15, 30)))
    with pytest.raises(StatementError, match="naive datetime"):
        session.commit()


def test_normalized_url_is_unique(session: Session) -> None:
    session.add_all([_article("https://example.com/a"), _article("https://example.com/a")])
    with pytest.raises(IntegrityError):
        session.commit()
