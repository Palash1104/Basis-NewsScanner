"""Watchlist alerts (step 4): what is sent, once, and the digest's watchlist section."""

from datetime import UTC, date, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, WatchlistFile, load_watchlist_file
from app.db import init_db, make_engine, make_session_factory
from app.models import (
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
    assert text.startswith("<b>BASIS · watchlist alert · HIGH</b>\n")
    # The item reads as it does everywhere (app/watch/render.py): every company, the linked
    # headline, then when it came out and how far it has got.
    assert "▲ <b>HAL</b> · ▲ <b>Bharat Dynamics</b>: <a href=" in text
    assert ">Russia partners Adani Defence, HAL, BDL for missiles</a>" in text
    assert "<i>10:30 · media only, not filed</i>" in text
    # Each company's own call, and the sources, folded underneath.
    assert "<blockquote expandable>" in text and "▲ Bharat Dynamics · medium · regulatory" in text
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
    text = out[0].messages[0]
    assert text.startswith("<b>BASIS · watchlist update</b>")
    assert "first media report 10:30 · NSE asked for clarification 11:10" in text
    assert "11:10 · NSE · News Verification" in text
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
    assert text.startswith(
        "<b>BASIS · while you were away</b>\n<i>Thu 08 Oct, 00:30 – 10:30 · 2 updates</i>"
    )
    # Most material first (user, 2026-10-16), each with its original time, IST.
    assert text.index("<b>HIGH</b>") < text.index("Older raid story") < text.index("<b>MEDIUM</b>")
    assert text.index("<b>MEDIUM</b>") < text.index("Newer HAL order")
    assert "Older raid story</a>\n<i>04:30 · media only, not filed</i>" in text
    assert "Newer HAL order</a>\n<i>08:30 · media only, not filed</i>" in text
    assert "Fresh BDL news" not in text  # under an hour old: alerted as usual instead
    assert "Possible gap: Wed 07 Oct 20:30 to Thu 08 Oct 23:30" not in text
    assert "Possible gap:" in text and "5 searches failed" in text


# ---------------------------------------------------------------- feeds and sending


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


# ---------------------------------------------------------------- news in a story BASIS had
# A high call that lands in a story begun days ago (user, 2026-10-19): Zen's Cogknit
# acquisition inside a 13 Oct share-price note, Astra Microwave's Q1 results inside a 9 Oct
# multibagger list.

ZEN = {"ZENTEC.NS": "Zen Technologies"}
URLS = count(1)


def add_article(
    session: Session,
    story: WatchStory,
    title: str,
    published: datetime,
    seen: datetime,
    verdict: str = "keep",
) -> WatchArticle:
    article = WatchArticle(
        url=f"https://news.example.com/zen/{next(URLS)}",
        source_name="Business Standard",
        title=title,
        snippet="",
        published_at=published,
        first_seen_at=seen,
        story=story,
    )
    article.matches = [WatchMatch(symbol="ZENTEC.NS", verdict=verdict, reason="named")]
    session.add(article)
    session.flush()
    return article


def add_zen_call(
    session: Session, story: WatchStory, at: datetime, materiality: str, trigger: str
) -> None:
    session.add(
        WatchCall(
            story_id=story.id,
            symbol="ZENTEC.NS",
            relevance="primary",
            sentiment="positive" if materiality != "low" else "neutral",
            materiality=materiality,
            event_type="acquisition" if materiality != "low" else "other",
            reason="It is buying a majority stake in Cogknit Semantics.",
            summary="Zen Technologies is buying Cogknit.",
            article_count=len(story.articles),
            filing_count=len(story.filings),
            read_reply=False,
            trigger=trigger,
            model="m",
            prompt_version="watch-v1",
            created_at=at,
        )
    )
    session.commit()


def share_price_note(session: Session, seen: datetime) -> WatchStory:
    """#101 as it began: a share-price note, called low and neutral."""
    story = WatchStory(first_seen_at=seen, headline="Zen Technologies Share Price Rises 5.39%")
    session.add(story)
    add_article(session, story, story.headline, seen, seen)
    add_zen_call(session, story, seen, "low", "new")
    return story


def cogknit(session: Session, story: WatchStory, published: datetime, seen: datetime) -> None:
    """The acquisition joining it: NSE's filing, then a report."""
    session.add(
        WatchFiling(
            key=f"cogknit-{story.id}",
            exchange="NSE",
            symbol="ZENTEC.NS",
            company="Zen Technologies Limited",
            subject="Acquisition",
            description="Zen Technologies Limited has informed the Exchange about Acquisition",
            link="https://nsearchives.example/cogknit.pdf",
            filed_at=published - timedelta(minutes=5),
            first_seen_at=seen - timedelta(minutes=5),
            kind="filing",
            story=story,
        )
    )
    add_article(session, story, "Zen Technologies to acquire 52% stake in Cogknit", published, seen)


def test_news_that_raises_an_old_story_to_high_alerts_led_by_that_news(
    db: sessionmaker[Session], settings: Settings
) -> None:
    with db() as session:
        story = share_price_note(session, NOW - timedelta(days=4))
        cogknit(session, story, NOW - timedelta(minutes=20), NOW - timedelta(minutes=10))
        add_zen_call(session, story, NOW - timedelta(minutes=5), "high", "filing")
        out = news_alerts(session, settings, ZEN, NOW)
    assert [o.key for o in out] == [f"news:{story.id}"]
    text = out[0].messages[0]
    # The acquisition's own headline and times, not the share-price note's.
    assert ">To acquire 52% stake in Cogknit</a>" in text
    assert "Share Price Rises" not in text
    assert "<i>NSE filing 10:05 · first media report 10:10</i>" in text
    deliver(db, out, lambda messages: None, NOW)
    with db() as session:
        assert news_alerts(session, settings, ZEN, NOW + timedelta(minutes=1)) == []


def test_no_alert_when_the_news_behind_the_rise_is_old_or_the_stock_was_high_already(
    db: sessionmaker[Session], settings: Settings
) -> None:
    with db() as session:
        stale = share_price_note(session, NOW - timedelta(days=4))
        # Came out eight hours ago; BASIS only now saw it (not in a catch-up).
        add_article(
            session,
            stale,
            "Zen Technologies to acquire Cogknit",
            NOW - timedelta(hours=8),
            NOW - timedelta(minutes=10),
        )
        add_zen_call(session, stale, NOW - timedelta(minutes=5), "high", "articles")
        high_already = share_price_note(session, NOW - timedelta(days=3))
        add_zen_call(session, high_already, NOW - timedelta(days=2), "high", "articles")
        cogknit(session, high_already, NOW - timedelta(minutes=20), NOW - timedelta(minutes=10))
        add_zen_call(session, high_already, NOW - timedelta(minutes=5), "high", "filing")
        assert news_alerts(session, settings, ZEN, NOW) == []


def test_a_first_call_on_a_story_begun_by_a_mention_is_judged_by_its_news(
    db: sessionmaker[Session], settings: Settings
) -> None:
    """#59: a multibagger list naming the company only in passing began the story the evening
    before; the Q1 results joined it three hours after they came out, and the story's first
    call rated them high. Judged by when the story began, no alert was sent."""
    with db() as session:
        began = NOW - timedelta(hours=18)
        story = WatchStory(first_seen_at=began, headline="6 Defence stocks turn multibaggers")
        session.add(story)
        add_article(session, story, story.headline, began, began, "mention")
        add_article(
            session,
            story,
            "Zen Technologies Q1 Results: Net Profit Declines; MD Steps Down",
            NOW - timedelta(hours=3),
            NOW - timedelta(minutes=10),
        )
        add_zen_call(session, story, NOW - timedelta(minutes=5), "high", "new")
        out = news_alerts(session, settings, ZEN, NOW)
    assert [o.key for o in out] == [f"news:{story.id}"]
    assert ">Q1 Results: Net Profit Declines; MD Steps Down</a>" in out[0].messages[0]
    assert "multibaggers" not in out[0].messages[0]


def test_the_digest_lists_news_that_raised_an_older_story_today(
    db: sessionmaker[Session], settings: Settings, watchlist: WatchlistFile
) -> None:
    with db() as session:
        raised = share_price_note(session, NOW - timedelta(days=4))
        cogknit(session, raised, NOW - timedelta(hours=1), NOW - timedelta(minutes=50))
        add_zen_call(session, raised, NOW - timedelta(minutes=45), "medium", "filing")
        same = share_price_note(session, NOW - timedelta(days=3))
        for title in ("Zen Technologies among 3 stocks crossing VWAP", "Zen shares end flat"):
            add_article(session, same, title, NOW - timedelta(hours=1), NOW - timedelta(minutes=50))
        add_zen_call(session, same, NOW - timedelta(minutes=45), "low", "articles")
        text = digest_section(session, watchlist, settings, NOW)
    assert text is not None and text.startswith("<b>WATCHLIST</b> · 1 story today")
    assert "<b>MEDIUM</b>" in text and ">To acquire 52% stake in Cogknit</a>" in text
    assert "VWAP" not in text and "Share Price Rises" not in text


def test_the_away_summary_lists_news_a_catch_up_added_to_an_older_story(
    db: sessionmaker[Session], settings: Settings, watchlist: WatchlistFile
) -> None:
    """Read in a catch-up three hours after it came out: in the summary under its own
    headline and time, and no alert of its own."""
    gap = {"start": (NOW - timedelta(hours=10)).isoformat(), "end": NOW.isoformat()}
    with db() as session:
        session.add(
            WatchRun(
                job="catchup",
                started_at=NOW,
                finished_at=NOW,
                errors=[],
                details={"gap": gap, "sources": [], "possible_gaps": [], "summary_sent": False},
            )
        )
        story = share_price_note(session, NOW - timedelta(days=2))
        title = "Zen Technologies to acquire 52% stake in Cogknit"
        add_article(session, story, title, NOW - timedelta(hours=3), NOW)
        add_zen_call(session, story, NOW + timedelta(minutes=1), "high", "articles")
        assert news_alerts(session, settings, ZEN, NOW + timedelta(minutes=2)) == []
        out, empty = away_summaries(session, watchlist, None, settings, NOW + timedelta(minutes=2))
    assert empty == [] and len(out) == 1
    text = "\n".join(out[0].messages)
    assert "<b>HIGH</b>" in text and ">To acquire 52% stake in Cogknit</a>" in text
    assert "<i>07:30 · media only, not filed</i>" in text
    assert "Share Price Rises" not in text
