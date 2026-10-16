"""Feed health (app/feed_health.py): on /runs, never in Telegram, and never counting time the
laptop was off against a feed (user, 2026-10-16)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import init_db, make_engine, make_session_factory
from app.feed_health import configured_labels, feed_health
from app.models import FeedCheck, WatchAlert
from app.watch.alerts import retry_pending

# Thursday 2026-10-15, 19:09 IST: when 22 false "feed warning" notifications went out.
WOKE = datetime(2026, 10, 15, 13, 39, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(tmp_path / "feeds.db")
    init_db(engine)
    return make_session_factory(engine)


def check(
    url: str,
    at: datetime,
    newest: datetime | None,
    status: str = "ok",
    error: str | None = None,
    name: str = "Example",
) -> FeedCheck:
    return FeedCheck(
        kind="pipeline",
        feed_name=name,
        feed_url=url,
        checked_at=at,
        status=status,
        newest_entry_at=newest,
        error=error,
    )


def fresh_history(url: str, until: datetime, days: int = 3) -> list[FeedCheck]:
    """Checks every three hours up to `until`, each finding an entry half an hour old."""
    rows = []
    at = until - timedelta(days=days)
    while at <= until:
        rows.append(check(url, at, at - timedelta(minutes=30)))
        at += timedelta(hours=3)
    return rows


def state_of(db: sessionmaker[Session], settings: Settings, now: datetime, url: str) -> str:
    with db() as session:
        found = {s.url: s for s in feed_health(session, settings, now, labels={})}
    return found[url].state


def test_a_night_with_the_laptop_off_is_not_a_stale_feed(
    db: sessionmaker[Session], settings: Settings
) -> None:
    """The regression: last fetch Wed 21:50 IST, the laptop off until Thu 19:07. The feed
    did nothing wrong, and before BASIS fetches it again there is nothing to judge."""
    url = "https://www.thehindu.com/news/national/feeder/default.rss"
    last = datetime(2026, 10, 14, 16, 20, tzinfo=UTC)  # 21:50 IST
    with db() as session:
        session.add_all(fresh_history(url, last))
        session.commit()
    assert state_of(db, settings, WOKE, url) == "ok"


def test_after_the_laptop_is_back_a_feed_with_nothing_new_is_stale(
    db: sessionmaker[Session], settings: Settings
) -> None:
    """The same night, but the first fetch after waking still finds Wednesday's entries:
    that is the feed, not the laptop."""
    url = "https://lm/rss"
    last = datetime(2026, 10, 14, 16, 20, tzinfo=UTC)
    with db() as session:
        session.add_all(fresh_history(url, last))
        session.add(check(url, WOKE, last - timedelta(minutes=30)))
        session.commit()
    with db() as session:
        status = feed_health(session, settings, WOKE, labels={})[0]
    assert status.state == "stale" and "nothing new for 21.8 h at its last fetch" in status.detail


def test_a_feed_that_is_always_slow_is_not_stale(
    db: sessionmaker[Session], settings: Settings
) -> None:
    """ET's curated company feed is hours old all day: only a change from that is a fault."""
    url = "https://et/rss"
    with db() as session:
        for hour in range(48):
            at = WOKE - timedelta(hours=hour)
            session.add(check(url, at, at - timedelta(hours=10)))
        session.commit()
    assert state_of(db, settings, WOKE, url) == "ok"


def test_a_failing_feed_says_since_when_and_why(
    db: sessionmaker[Session], settings: Settings
) -> None:
    url = "https://c/rss"
    with db() as session:
        session.add_all(fresh_history(url, WOKE - timedelta(hours=1)))
        for minutes in (30, 20, 10):
            session.add(check(url, WOKE - timedelta(minutes=minutes), None, "error", "HTTP 403"))
        session.commit()
    with db() as session:
        status = feed_health(session, settings, WOKE, labels={})[0]
    assert status.state == "failing" and "HTTP 403" in status.detail and status.problem


def test_a_new_feed_is_not_judged(db: sessionmaker[Session], settings: Settings) -> None:
    url = "https://new/rss"
    with db() as session:
        session.add(check(url, WOKE, WOKE - timedelta(hours=30)))
        session.commit()
    assert state_of(db, settings, WOKE, url) == "new"


def test_google_news_is_never_stale(db: sessionmaker[Session], settings: Settings) -> None:
    url = "https://news.google.com/rss/search?q=x"
    last = WOKE - timedelta(hours=2)
    with db() as session:
        session.add_all(fresh_history(url, last))
        session.add(check(url, WOKE, last - timedelta(hours=20)))
        session.commit()
    assert state_of(db, settings, WOKE, url) == "ok"


def test_feeds_sharing_an_outlet_name_have_their_own_labels() -> None:
    labels = configured_labels()
    top = "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"
    economy = "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"
    assert labels[top] == "CNBC · top news" and labels[economy] == "CNBC · economy"
    assert labels["https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml"] == (
        "CNBC-TV18 · market"
    )
    # Every feed a person can read about on /runs is distinguishable by its label.
    by_label: dict[str, set[str]] = {}
    for url, label in labels.items():
        by_label.setdefault(label, set()).add(url)
    assert [label for label, urls in by_label.items() if len(urls) > 1] == []


def test_an_unsent_feed_warning_from_before_is_never_retried(
    db: sessionmaker[Session],
) -> None:
    with db() as session:
        session.add(
            WatchAlert(
                key="feed:https://x:stale:2026-10-15",
                kind="feed",
                symbols=[],
                text="<b>BASIS · feed warning</b>",
                created_at=WOKE,
                attempts=1,
            )
        )
        session.commit()
        assert retry_pending(session, WOKE + timedelta(minutes=5)) == []
