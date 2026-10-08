"""Watchlist alerts (step 4): what is sent, once, and the digest's watchlist section."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, WatchlistFile, load_watchlist_file
from app.db import init_db, make_engine, make_session_factory
from app.models import (
    FeedCheck,
    WatchAlert,
    WatchArticle,
    WatchCall,
    WatchFiling,
    WatchMatch,
    WatchRun,
    WatchSighting,
    WatchStory,
)
from app.watch.alerts import (
    MESSAGE_SEPARATOR,
    Outgoing,
    away_summaries,
    deliver,
    digest_section,
    feed_warnings,
    followups,
    news_alerts,
    retry_pending,
)
from app.watch.moves import Snapshot, Typical, detect, typical_moves

# Thursday 2026-10-08, 10:30 IST.
NOW = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)
NAMES = {"PFOCUS.NS": "Prime Focus", "HAL.NS": "HAL", "BDL.NS": "Bharat Dynamics"}


@pytest.fixture
def db(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(tmp_path / "alerts.db")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def watchlist() -> WatchlistFile:
    return load_watchlist_file()


def story_with_call(
    session: Session,
    title: str,
    calls: dict[str, tuple[str, str, str]],  # symbol -> (relevance, sentiment, materiality)
    seen: datetime = NOW,
    published: datetime | None = None,
) -> WatchStory:
    story = WatchStory(first_seen_at=seen, headline=title)
    article = WatchArticle(
        url=f"https://news.example.com/{abs(hash(title))}",
        source_name="CNBC-TV18",
        title=title,
        snippet="",
        published_at=published or seen,
        first_seen_at=seen,
        story=story,
    )
    article.matches = [
        WatchMatch(symbol=s, verdict="keep", reason="named in the headline") for s in calls
    ]
    article.sightings = [
        WatchSighting(feed_name="CNBC-TV18", feed_url="https://x/rss", via="rss", seen_at=seen)
    ]
    session.add_all([story, article])
    session.flush()
    for symbol, (relevance, sentiment, materiality) in calls.items():
        session.add(
            WatchCall(
                story_id=story.id,
                symbol=symbol,
                relevance=relevance,
                sentiment=sentiment,
                materiality=materiality,
                event_type="regulatory",
                reason=f"{symbol} reason.",
                summary="Something happened. It matters.",
                article_count=1,
                filing_count=0,
                read_reply=False,
                trigger="new",
                model="m",
                prompt_version="watch-v1",
                created_at=seen,
            )
        )
    session.commit()
    return story


# ---------------------------------------------------------------- moves


def snap(symbol: str, move: float) -> Snapshot:
    return Snapshot(symbol, NOW, move)


TYPICAL = {
    "PFOCUS.NS": Typical(0.04, None),
    "HAL.NS": Typical(0.016, 0.01),
    "BDL.NS": Typical(0.024, 0.012),
    "ZENTEC.NS": Typical(0.027, 0.018),
    "NIFTY_IND_DEFENCE.NS": Typical(0.014, None),
}
GROUPS = {"defence": ("NIFTY_IND_DEFENCE.NS", ["HAL.NS", "BDL.NS", "ZENTEC.NS"])}
DAY = date(2026, 10, 8)


def test_a_lone_stock_alerts_at_twice_its_typical_day() -> None:
    events = detect({"PFOCUS.NS": snap("PFOCUS.NS", -0.085)}, TYPICAL, {}, ["PFOCUS.NS"], {}, DAY)
    assert [(e.kind, e.key) for e in events] == [("price", "price:PFOCUS.NS:2026-10-08")]
    assert events[0].multiple == pytest.approx(0.085 / 0.04)
    assert (
        detect({"PFOCUS.NS": snap("PFOCUS.NS", -0.07)}, TYPICAL, {}, ["PFOCUS.NS"], {}, DAY) == []
    )
    story = {"PFOCUS.NS": True}  # the story explains it: no "no story yet"
    assert (
        detect({"PFOCUS.NS": snap("PFOCUS.NS", -0.09)}, TYPICAL, {}, ["PFOCUS.NS"], story, DAY)
        == []
    )


def test_peers_moving_together_are_one_sector_alert() -> None:
    snaps = {
        "NIFTY_IND_DEFENCE.NS": snap("NIFTY_IND_DEFENCE.NS", -0.031),
        "HAL.NS": snap("HAL.NS", -0.033),
        "BDL.NS": snap("BDL.NS", -0.04),
        "ZENTEC.NS": snap("ZENTEC.NS", -0.035),
    }
    events = detect(snaps, TYPICAL, GROUPS, [], {}, DAY)
    assert [e.kind for e in events] == ["sector"]  # no stock moved beyond its peers
    assert events[0].key == "sector:defence:2026-10-08:down"
    assert dict(events[0].peers)["BDL.NS"] == -0.04


def test_a_stock_well_beyond_its_peers_alerts_on_its_own() -> None:
    snaps = {
        "NIFTY_IND_DEFENCE.NS": snap("NIFTY_IND_DEFENCE.NS", -0.005),
        "HAL.NS": snap("HAL.NS", -0.004),
        "BDL.NS": snap("BDL.NS", -0.045),  # 4% below the index: 3.3x its typical net move
        "ZENTEC.NS": snap("ZENTEC.NS", 0.002),
    }
    events = detect(snaps, TYPICAL, GROUPS, [], {}, DAY)
    assert [(e.kind, e.symbol) for e in events] == [("price", "BDL.NS")]
    assert events[0].excess == pytest.approx(-0.04)


def test_most_of_the_group_moving_is_a_sector_move_even_without_the_index() -> None:
    snaps = {
        "HAL.NS": snap("HAL.NS", 0.03),
        "BDL.NS": snap("BDL.NS", 0.04),
        "ZENTEC.NS": snap("ZENTEC.NS", 0.001),
    }
    events = detect(snaps, TYPICAL, GROUPS, [], {}, DAY)
    assert [e.kind for e in events] == ["sector"] and events[0].key.endswith(":up")


def test_typical_moves_are_the_median_excursion_of_earlier_sessions() -> None:
    def bars(day: int, high: float, low: float, close: float):
        start = datetime(2026, 8, 1, 4, 0, tzinfo=UTC) + timedelta(days=day)
        return (start, high, low, close)

    series = [bars(d, 101 + (d % 3), 99 - (d % 3), 100) for d in range(40)]
    found = typical_moves({"X.NS": series}, {"X.NS": None}, date(2026, 10, 1), lambda m: m.date())
    assert found["X.NS"].move == pytest.approx(0.02)  # excursions 1%, 2%, 3% -> median 2%


# ---------------------------------------------------------------- news and follow-ups


def test_one_alert_per_story_lists_every_stock_it_is_about(
    db: sessionmaker[Session], settings: Settings
) -> None:
    with db() as session:
        story = story_with_call(
            session,
            "Russia partners Adani Defence, HAL, BDL for missiles",
            {
                "HAL.NS": ("primary", "positive", "high"),
                "BDL.NS": ("secondary", "positive", "medium"),
            },
        )
        story_with_call(
            session, "HAL director appointed", {"HAL.NS": ("primary", "neutral", "low")}
        )
        story_with_call(session, "HAL options chart", {"HAL.NS": ("passing", "neutral", "low")})
        out = news_alerts(session, settings, NAMES, NOW)
    assert [o.key for o in out] == [f"news:{story.id}"]
    text = out[0].messages[0]
    assert "▲ HAL (high) · ▲ Bharat Dynamics (medium)" in text
    assert "media report, not yet filed" in text and "<blockquote expandable>" in text
    deliver(db, out, lambda messages: None, NOW)
    with db() as session:
        assert news_alerts(session, settings, NAMES, NOW) == []  # once per story


def test_old_news_read_in_a_catch_up_goes_to_the_summary_not_an_alert(
    db: sessionmaker[Session], settings: Settings
) -> None:
    with db() as session:
        session.add(
            WatchRun(
                job="catchup",
                started_at=NOW,
                finished_at=NOW,
                errors=[],
                details={
                    "gap": {
                        "start": (NOW - timedelta(hours=10)).isoformat(),
                        "end": NOW.isoformat(),
                    },
                    "sources": [],
                    "possible_gaps": [],
                    "summary_sent": False,
                },
            )
        )
        story_with_call(
            session,
            "Prime Focus shares tank after tax raids",
            {"PFOCUS.NS": ("primary", "negative", "high")},
            seen=NOW,
            published=NOW - timedelta(hours=3),
        )
        assert news_alerts(session, settings, NAMES, NOW) == []


def test_a_filing_after_the_alert_is_one_follow_up(
    db: sessionmaker[Session], settings: Settings
) -> None:
    with db() as session:
        story = story_with_call(
            session,
            "Prime Focus shares tank after tax raids",
            {"PFOCUS.NS": ("primary", "negative", "high")},
        )
    deliver(db, [Outgoing("news", f"news:{story.id}", ("x",), story.id)], lambda m: None, NOW)
    later = NOW + timedelta(minutes=40)
    with db() as session:
        session.add(
            WatchFiling(
                key="f1",
                exchange="NSE",
                symbol="PFOCUS.NS",
                company="Prime Focus Limited",
                subject="News Verification",
                description="The Exchange has sought clarification.",
                filed_at=later,
                first_seen_at=later,
                kind="clarification_sought",
                story_id=story.id,
            )
        )
        session.commit()
        assert followups(session, settings, NAMES, later) == []  # waiting for the re-call
        out = followups(session, settings, NAMES, later + timedelta(minutes=16))
    assert [o.key for o in out] == [f"followup:{story.id}"]
    assert "NSE asked the company to clarify" in out[0].messages[0]
    deliver(db, out, lambda m: None, later)
    with db() as session:
        assert followups(session, settings, NAMES, later + timedelta(hours=1)) == []


# ---------------------------------------------------------------- while you were away


def test_the_away_summary_is_newest_first_with_original_times_and_gaps(
    db: sessionmaker[Session], settings: Settings, watchlist: WatchlistFile
) -> None:
    gap_start = NOW - timedelta(hours=10)
    with db() as session:
        session.add(
            WatchRun(
                job="catchup",
                started_at=NOW,
                finished_at=NOW,
                errors=[],
                details={
                    "gap": {"start": gap_start.isoformat(), "end": NOW.isoformat()},
                    "sources": [],
                    "possible_gaps": [
                        {
                            "what": "news",
                            "from": gap_start.isoformat(),
                            "to": (NOW - timedelta(hours=7)).isoformat(),
                            "why": "5 searches failed",
                        }
                    ],
                    "summary_sent": False,
                },
            )
        )
        story_with_call(
            session,
            "Older raid story",
            {"PFOCUS.NS": ("primary", "negative", "high")},
            seen=NOW,
            published=NOW - timedelta(hours=6),
        )
        story_with_call(
            session,
            "Newer HAL order",
            {"HAL.NS": ("primary", "positive", "medium")},
            seen=NOW,
            published=NOW - timedelta(hours=2),
        )
        story_with_call(
            session,
            "Fresh BDL news",
            {"BDL.NS": ("primary", "neutral", "low")},
            seen=NOW,
            published=NOW - timedelta(minutes=20),
        )
        out, empty = away_summaries(session, watchlist, None, settings, NOW + timedelta(minutes=11))
    assert empty == [] and len(out) == 1
    text = "\n".join(out[0].messages)
    assert text.index("Newer HAL order") < text.index("Older raid story")  # newest first
    assert "Fresh BDL news" not in text  # under an hour old: alerted as usual instead
    assert "<b>08:30</b>" in text and "<b>04:30</b>" in text  # original times, IST
    assert "Possible gap: Wed 07 Oct 20:30 to Thu 08 Oct 23:30" not in text
    assert "Possible gap:" in text and "5 searches failed" in text


# ---------------------------------------------------------------- feeds and sending


def test_a_failing_feed_is_one_warning_per_episode(
    db: sessionmaker[Session], settings: Settings
) -> None:
    with db() as session:
        for minutes in (30, 20, 10, 0):
            session.add(
                FeedCheck(
                    kind="watch",
                    feed_name="CNBC-TV18",
                    feed_url="https://c/rss",
                    checked_at=NOW - timedelta(minutes=minutes),
                    status="ok" if minutes == 30 else "error",
                    error="HTTP 403",
                    newest_entry_at=NOW - timedelta(minutes=40),
                )
            )
        session.commit()
        out = feed_warnings(session, settings, NOW)
    assert (
        len(out) == 1 and "failing since" in out[0].messages[0] and "HTTP 403" in out[0].messages[0]
    )
    deliver(db, out, lambda m: None, NOW)
    with db() as session:
        assert feed_warnings(session, settings, NOW) == []


def test_a_feed_that_is_always_slow_is_not_stale(
    db: sessionmaker[Session], settings: Settings
) -> None:
    """ET's curated company feed is hours old all day: only a change from that is a fault."""
    with db() as session:
        for hour in range(48):
            checked = NOW - timedelta(hours=hour)
            session.add(
                FeedCheck(
                    kind="watch",
                    feed_name="The Economic Times",
                    feed_url="https://et/rss",
                    checked_at=checked,
                    status="ok",
                    newest_entry_at=checked - timedelta(hours=10),
                )
            )
        session.commit()
        assert feed_warnings(session, settings, NOW) == []


def test_a_failed_send_is_recorded_and_retried(db: sessionmaker[Session]) -> None:
    item = Outgoing("news", "news:1", ("first", "second"))

    def broken(messages: list[str]) -> None:
        raise RuntimeError("telegram down")

    result = deliver(db, [item], broken, NOW)
    assert result.failed == [("news:1", "RuntimeError: telegram down")]
    with db() as session:
        pending = retry_pending(session, NOW + timedelta(minutes=5))
        assert [p.messages for p in pending] == [("first", "second")]
        row = session.scalars(select(WatchAlert)).one()
        assert row.text == f"first{MESSAGE_SEPARATOR}second" and row.attempts == 1
    sent: list[list[str]] = []
    deliver(db, pending, sent.append, NOW + timedelta(minutes=5))
    assert sent == [["first", "second"]]
    with db() as session:
        assert retry_pending(session, NOW + timedelta(minutes=6)) == []


# ---------------------------------------------------------------- the digest


def test_the_digest_section_lists_todays_stories_most_material_first(
    db: sessionmaker[Session], settings: Settings, watchlist: WatchlistFile
) -> None:
    with db() as session:
        story_with_call(
            session, "HAL director appointed", {"HAL.NS": ("primary", "neutral", "low")}
        )
        story_with_call(
            session,
            "Prime Focus raid",
            {"PFOCUS.NS": ("primary", "negative", "high")},
            seen=NOW - timedelta(hours=2),
        )
        story_with_call(session, "Options chart", {"HAL.NS": ("passing", "neutral", "low")})
        story_with_call(
            session,
            "Yesterday's BDL story",
            {"BDL.NS": ("primary", "positive", "high")},
            seen=NOW - timedelta(hours=14),
        )  # before 22:00 last night
        text = digest_section(session, watchlist, settings, NOW)
    assert text is not None and text.startswith("<b>WATCHLIST</b> · 2 stories today")
    assert text.index("Prime Focus raid") < text.index("HAL director appointed")
    assert "Options chart" not in text and "Yesterday's BDL story" not in text


def test_a_usually_fresh_feed_going_quiet_is_stale(
    db: sessionmaker[Session], settings: Settings
) -> None:
    afternoon = datetime(2026, 10, 8, 11, 0, tzinfo=UTC)  # 16:30 IST, inside the window
    with db() as session:
        for minutes in range(0, 24 * 60, 60):  # a day of checks, newest entry 30 min old
            checked = afternoon - timedelta(hours=8, minutes=minutes)
            session.add(
                FeedCheck(
                    kind="watch",
                    feed_name="Livemint",
                    feed_url="https://lm/rss",
                    checked_at=checked,
                    status="ok",
                    newest_entry_at=checked - timedelta(minutes=30),
                )
            )
        for hours in range(8, -1, -1):  # then eight hours with nothing new
            session.add(
                FeedCheck(
                    kind="watch",
                    feed_name="Livemint",
                    feed_url="https://lm/rss",
                    checked_at=afternoon - timedelta(hours=hours),
                    status="not_modified",
                )
            )
        session.commit()
        out = feed_warnings(session, settings, afternoon)
    assert len(out) == 1 and "Livemint: nothing new for 8.5 h" in out[0].messages[0]
