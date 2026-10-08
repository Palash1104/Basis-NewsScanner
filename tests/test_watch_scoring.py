"""Scoring watchlist calls against the Nifty and the defence index, and who had it first."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, WatchlistFile, load_watchlist_file
from app.db import init_db, make_engine, make_session_factory
from app.models import (
    WatchArticle,
    WatchCall,
    WatchFiling,
    WatchMatch,
    WatchRun,
    WatchScore,
    WatchStory,
)
from app.pipeline.prices import Bar
from app.watch.scoring import (
    daily_sessions,
    lead_times,
    scorable_calls,
    score_watch_calls,
    watch_track_record,
)
from tests.fakes import FakePrices

# The story: first seen Wednesday 2026-10-07, 11:00 IST.
SEEN = datetime(2026, 10, 7, 5, 30, tzinfo=UTC)
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)  # Friday evening: one horizon due, not five


@pytest.fixture
def db(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(tmp_path / "score.db")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def watchlist() -> WatchlistFile:
    return load_watchlist_file()


def hourly(jump: float) -> list[Bar]:
    """Two hourly bars a session (10:00 and 15:00 IST) from late August, the close swinging
    1% either way, then `jump` at 15:00 on the story's day."""
    found: list[Bar] = []
    day = date(2026, 8, 20)
    price = 100.0
    swing = 1
    while day <= date(2026, 10, 9):
        if day.weekday() < 5:
            morning = datetime(day.year, day.month, day.day, 4, 30, tzinfo=UTC)
            evening = datetime(day.year, day.month, day.day, 9, 30, tzinfo=UTC)
            found.append(Bar(morning, price, price, price, price, 1000))
            price *= 1 + 0.01 * swing
            swing = -swing
            if day == date(2026, 10, 7):
                price *= 1 + jump
            found.append(Bar(evening, price, price, price, price, 1000))
        day += timedelta(days=1)
    return found


def add_call(
    session: Session,
    symbol: str,
    sentiment: str = "positive",
    relevance: str = "primary",
    created: datetime = SEEN,
    story: WatchStory | None = None,
) -> WatchCall:
    if story is None:
        story = WatchStory(first_seen_at=SEEN, headline=f"{symbol} news")
        session.add(story)
        session.flush()
    call = WatchCall(
        story_id=story.id,
        symbol=symbol,
        relevance=relevance,
        sentiment=sentiment,
        materiality="high",
        event_type="order_win",
        reason="It won an order.",
        summary="It won an order.",
        trigger="new",
        model="m",
        prompt_version="watch-v1",
        created_at=created,
    )
    session.add(call)
    session.commit()
    return call


def test_sessions_close_at_their_last_hourly_bar_in_exchange_time() -> None:
    bars = hourly(0.0)
    sessions = daily_sessions(bars)
    assert sessions[0].date == date(2026, 8, 20)
    assert sessions[0].close == bars[1].close  # the 15:00 bar, not the 10:00 one
    assert all(s.date.weekday() < 5 for s in sessions)  # no bars, no session


def test_only_the_first_directional_call_per_story_and_stock_is_scored(
    db: sessionmaker[Session],
) -> None:
    with db() as session:
        first = add_call(session, "HAL.NS")
        story = session.get(WatchStory, first.story_id)
        add_call(session, "HAL.NS", created=SEEN + timedelta(hours=2), story=story)  # a re-call
        add_call(session, "BDL.NS", sentiment="neutral")
        add_call(session, "ZENTEC.NS", relevance="passing")
        assert [c.id for c in scorable_calls(session)] == [first.id]


def test_a_sector_rally_is_a_hit_against_the_nifty_but_not_against_the_index(
    db: sessionmaker[Session], settings: Settings, watchlist: WatchlistFile
) -> None:
    """The point of the second benchmark (user, 2026-10-08)."""
    prices = FakePrices(
        {
            "HAL.NS": {"60m": hourly(0.05)},
            "^NSEI": {"60m": hourly(0.0)},
            "NIFTY_IND_DEFENCE.NS": {"60m": hourly(0.05)},
        }
    )
    with db() as session:
        call = add_call(session, "HAL.NS")
        report = score_watch_calls(session, prices, watchlist, settings, NOW)
        rows = {
            (s.benchmark_symbol, s.horizon_days): s
            for s in session.scalars(select(WatchScore).where(WatchScore.call_id == call.id))
        }
    assert rows[("^NSEI", 1)].outcome == "hit"
    assert rows[("NIFTY_IND_DEFENCE.NS", 1)].outcome == "no_move"
    assert rows[("^NSEI", 1)].excess_return == pytest.approx(0.05, abs=0.002)
    assert ("^NSEI", 5) not in rows and report.not_due == 2  # five sessions haven't passed


def test_a_stock_outside_any_group_has_only_the_nifty(
    db: sessionmaker[Session], settings: Settings, watchlist: WatchlistFile
) -> None:
    prices = FakePrices({"PFOCUS.NS": {"60m": hourly(-0.09)}, "^NSEI": {"60m": hourly(0.0)}})
    with db() as session:
        add_call(session, "PFOCUS.NS", sentiment="negative")
        score_watch_calls(session, prices, watchlist, settings, NOW)
        scores = session.scalars(select(WatchScore)).all()
    assert [(s.benchmark_symbol, s.horizon_days, s.outcome) for s in scores] == [
        ("^NSEI", 1, "hit")
    ]


def test_scoring_is_written_once(
    db: sessionmaker[Session], settings: Settings, watchlist: WatchlistFile
) -> None:
    prices = FakePrices(
        {
            "HAL.NS": {"60m": hourly(0.05)},
            "^NSEI": {"60m": hourly(0.0)},
            "NIFTY_IND_DEFENCE.NS": {"60m": hourly(0.0)},
        }
    )
    with db() as session:
        add_call(session, "HAL.NS")
        score_watch_calls(session, prices, watchlist, settings, NOW)
        again = score_watch_calls(session, prices, watchlist, settings, NOW)
    assert again.total == 0


def test_the_track_record_has_a_row_per_benchmark(
    db: sessionmaker[Session], settings: Settings, watchlist: WatchlistFile
) -> None:
    prices = FakePrices(
        {
            "HAL.NS": {"60m": hourly(0.05)},
            "^NSEI": {"60m": hourly(0.0)},
            "NIFTY_IND_DEFENCE.NS": {"60m": hourly(0.05)},
        }
    )
    with db() as session:
        add_call(session, "HAL.NS")
        score_watch_calls(session, prices, watchlist, settings, NOW)
        rows = watch_track_record(session, "materiality")
    assert [(r.key, r.benchmark, r.hits, r.no_move) for r in rows] == [
        ("high", "^NSEI", 1, 0),
        ("high", "NIFTY_IND_DEFENCE.NS", 0, 1),
    ]


def test_who_had_it_first_and_by_how_much(db: sessionmaker[Session], settings: Settings) -> None:
    """CNBC-TV18 first, Business Today 30 min later, NSE's filing 42 min after CNBC; a story
    read in a catch-up says nothing about who was first and is left out."""
    with db() as session:
        session.add(
            WatchRun(job="feeds", started_at=SEEN - timedelta(hours=2), entries=900, errors=[])
        )
        session.add(
            WatchRun(job="feeds", started_at=SEEN - timedelta(minutes=10), entries=900, errors=[])
        )
        session.add(WatchRun(job="catchup", started_at=SEEN + timedelta(days=1), errors=[]))
        story = WatchStory(first_seen_at=SEEN, headline="Prime Focus shares tank")
        session.add(story)
        session.flush()
        for minutes, outlet in ((0, "CNBC-TV18"), (30, "Business Today")):
            article = WatchArticle(
                url=f"https://x/{outlet}",
                source_name=outlet,
                title="Prime Focus shares tank",
                snippet="",
                published_at=SEEN,
                first_seen_at=SEEN + timedelta(minutes=minutes),
                story=story,
            )
            article.matches = [WatchMatch(symbol="PFOCUS.NS", verdict="keep", reason="x")]
            session.add(article)
        session.add(
            WatchFiling(
                key="f",
                exchange="NSE",
                symbol="PFOCUS.NS",
                company="Prime Focus Limited",
                subject="News Verification",
                description="x",
                filed_at=SEEN + timedelta(minutes=42),
                first_seen_at=SEEN + timedelta(minutes=45),
                kind="clarification_sought",
                story_id=story.id,
            )
        )
        late = WatchStory(first_seen_at=SEEN + timedelta(days=1), headline="Read in a catch-up")
        session.add(late)
        session.flush()
        old = WatchArticle(
            url="https://x/old",
            source_name="Livemint",
            title="old",
            snippet="",
            published_at=SEEN,
            first_seen_at=SEEN + timedelta(days=1),
            story=late,
        )
        old.matches = [WatchMatch(symbol="HAL.NS", verdict="keep", reason="x")]
        session.add(old)
        session.commit()
        rows, counted, skipped = lead_times(session, settings, SEEN + timedelta(days=2), days=30)
    by_source = {row.source: row for row in rows}
    assert (counted, skipped) == (1, 1)
    assert by_source["CNBC-TV18"].first == 1
    assert by_source["CNBC-TV18"].median_lead == 42 * 60
    assert by_source["Business Today"].median_lag == 30 * 60
    assert by_source["NSE filing"].median_lag == 42 * 60
