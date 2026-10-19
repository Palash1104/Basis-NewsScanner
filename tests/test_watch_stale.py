"""Old news stays old (user, 2026-10-19).

A Google News search returns old pages whatever window it asks for, and the scanner stored them
with first_seen_at = the day they turned up: Apollo Micro Systems' card and Latest tab showed a
5 August order win as "new", and a 2020 price page dated "16 Dec". When a story happened is
when it was first reported, never when BASIS first saw it.

The headlines are real ones from the live database of 2026-10-19 (stories 131, 194, 101).
"""

import re
import shutil
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, WatchlistFile, load_watchlist_file
from app.db import (
    init_db,
    make_engine,
    make_read_only_engine,
    make_read_only_session_factory,
    make_session_factory,
)
from app.models import (
    WatchArticle,
    WatchCall,
    WatchFiling,
    WatchMatch,
    WatchPrice,
    WatchRun,
    WatchScan,
    WatchStory,
    utcnow,
)
from app.watch import ondemand
from app.watch.alerts import moves_in_period, stories_since
from app.watch.analyse import first_reported, stale_story, stories_to_call
from app.watch.moves import Typical
from app.watch.render import status_line
from app.watch.scoring import scorable_calls
from app.web.main import _day, _in_zone, create_app
from tests.conftest import FIXTURES
from tests.fakes import FakeProvider
from tests.test_watch_analyse import answer, client
from tests.test_watch_catchup import CatchUpWeb
from tests.test_watch_scan import FEED_URL, T0, Clock, FakeSnapshots, make_watcher

WEEK = timedelta(days=7)

# Story 131's old page: published 15 Dec 2020, turned up by an Apollo search on 19 Oct 2026.
APOLLO_2020 = (
    "Apollo Micro Systems Stock Price Forecast. Should You Buy APOLLO.NS?",
    "MarketBeat",
    "https://news.google.com/rss/articles/apollo-2020",
    datetime(2020, 12, 15, 21, 17, tzinfo=UTC),
)
# Story 194: a 5 August order win, first seen on 19 Oct.
APOLLO_AUGUST = (
    "Apollo Micro Systems bags Rs 213.39 crore defence orders from DRDO, Defence PSUs and "
    "private firms",
    "Business Standard",
    "https://news.google.com/rss/articles/apollo-drdo",
    datetime(2026, 8, 5, 14, 10, tzinfo=UTC),
)


def google_news_dated(*items: tuple[str, str, str, datetime]) -> str:
    """Google News items as it sends them, each with its own pubDate."""
    body = "".join(
        f"<item><title>{title} - {outlet}</title><link>{link}</link>"
        f'<source url="https://{outlet.lower().replace(" ", "")}.com">{outlet}</source>'
        f"<pubDate>{format_datetime(published)}</pubDate></item>"
        for title, outlet, link, published in items
    )
    return f'<rss version="2.0"><channel><title>Google News</title>{body}</channel></rss>'


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "newsdesk.db"
    engine = make_engine(path)
    init_db(engine)
    engine.dispose()
    return path


@pytest.fixture
def db(database: Path) -> sessionmaker[Session]:
    return make_session_factory(make_engine(database))


@pytest.fixture
def watchlist() -> WatchlistFile:
    return load_watchlist_file()  # tests/fixtures/watchlist.yaml (conftest)


# ---------------------------------------------------------------- intake


def test_a_2020_google_news_result_is_counted_not_stored_in_a_regular_pass(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    fresh = (
        "Apollo Micro Systems wins Rs 120 crore order from BEL",
        "Mint",
        "https://news.google.com/rss/articles/apollo-bel",
        T0 - timedelta(hours=2),
    )
    web = CatchUpWeb()
    web.google = google_news_dated(APOLLO_2020, fresh)
    watcher = make_watcher(settings, db, watchlist, web, Clock(T0))
    result = watcher.run_job("google_news", T0)

    assert result.new_articles == 1 and result.stale == {"Google News": 1}
    assert "1 older than a week skipped (1 from Google News)" in result.line()
    with db() as session:
        titles = session.scalars(select(WatchArticle.title)).all()
        run = session.scalars(select(WatchRun).where(WatchRun.job == "google_news")).one()
    assert titles == ["Apollo Micro Systems wins Rs 120 crore order from BEL"]
    assert run.details == {"stale_skipped": {"Google News": 1}}


def test_an_old_item_in_an_outlet_s_own_feed_is_counted_by_its_name(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    """The ET company feed carries two weeks of curated items: the older ones aren't news."""
    old = format_datetime(T0 - timedelta(days=12))
    web = CatchUpWeb()
    web.feed = (
        '<rss version="2.0"><channel><title>Markets</title>'
        "<item><title>HAL bags Rs 2,000 crore order for Dhruv helicopters</title>"
        f"<link>https://markets.example.com/hal-old</link><pubDate>{old}</pubDate></item>"
        "</channel></rss>"
    )
    watcher = make_watcher(settings, db, watchlist, web, Clock(T0))
    watcher.find_gap = lambda now: None  # type: ignore[method-assign]
    result = watcher.run_job("feeds", T0)
    assert result.new_articles == 0 and result.stale == {"Markets Daily": 1}
    assert any(str(r.url) == FEED_URL for r in web.requests)  # the outlet's own feed


def test_an_on_demand_scan_skips_and_says_so(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, tmp_path: Path
) -> None:
    five_days = (
        "Apollo Micro Systems board to consider preferential allotment",
        "Business Standard",
        "https://news.google.com/rss/articles/apollo-board",
        T0 - timedelta(days=5),
    )
    web = CatchUpWeb()
    web.google = google_news_dated(APOLLO_2020, APOLLO_AUGUST, five_days)
    watcher = make_watcher(settings, db, watchlist, web, Clock(T0), FakeSnapshots())
    inbox = tmp_path / "inbox"
    watcher.scan_inbox = inbox
    watcher.llm = client(settings, FakeProvider(responder=answer))
    ondemand.write_request(inbox, "APOLLO.NS", T0)
    assert watcher.serve_scan_requests() == 1
    with db() as session:
        scan = session.scalars(select(WatchScan)).one()
        stored = session.scalars(select(WatchArticle.title)).all()
        run = session.scalars(select(WatchRun).where(WatchRun.job == "on_demand")).one()
    assert stored == ["Apollo Micro Systems board to consider preferential allotment"]
    assert scan.skipped_stale == 2 and scan.new_articles == 1
    google = next(step for step in scan.steps if step["key"] == "google_news")
    assert "2 older than a week skipped" in google["detail"]
    assert ondemand.footer_line(scan).endswith("· 2 results older than a week skipped")
    assert run.details is not None and run.details["stale_skipped"] == {"Google News": 2}


def test_filings_are_not_affected(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    """An NSE filing found days late keeps its late-arrival handling: the catch-up's NSE API
    rows of 7 Oct are stored on 8 Oct like any other."""
    web = CatchUpWeb()
    watcher = make_watcher(settings, db, watchlist, web, Clock(T0))
    with db() as session:
        session.add(
            WatchRun(job="feeds", started_at=T0 - timedelta(hours=10), entries=9, errors=[])
        )
        session.commit()
    watcher.run_job("feeds", T0)
    with db() as session:
        assert session.scalars(select(WatchFiling)).all()


# ---------------------------------------------------------------- what a story's date is


def add_story(
    session: Session,
    symbol: str,
    pieces: list[tuple[str, datetime, datetime]],  # (title, published, first seen)
    call: tuple[str, str, str] | None = ("primary", "positive", "high"),
    called_at: datetime | None = None,
    verdict: str = "keep",
) -> WatchStory:
    story = WatchStory(first_seen_at=min(seen for _, _, seen in pieces), headline=pieces[0][0])
    session.add(story)
    for n, (title, published, seen) in enumerate(pieces):
        article = WatchArticle(
            url=f"https://news.example/{abs(hash(title))}-{n}",
            source_name="Mint",
            title=title,
            snippet="",
            published_at=published,
            first_seen_at=seen,
            story=story,
        )
        article.matches = [WatchMatch(symbol=symbol, verdict=verdict, reason="named")]
        session.add(article)
    session.flush()
    if call is not None:
        relevance, sentiment, materiality = call
        session.add(
            WatchCall(
                story_id=story.id,
                symbol=symbol,
                relevance=relevance,
                sentiment=sentiment,
                materiality=materiality,
                event_type="order_win",
                reason=f"Reason for {pieces[0][0][:30]}.",
                summary="A summary.",
                article_count=len(pieces),
                filing_count=0,
                read_reply=False,
                trigger="new",
                model="m",
                prompt_version="watch-v1",
                created_at=called_at or story.first_seen_at,
            )
        )
    session.commit()
    return story


def loaded(session: Session, story_id: int) -> WatchStory:
    from sqlalchemy.orm import selectinload

    return session.scalars(
        select(WatchStory)
        .where(WatchStory.id == story_id)
        .options(
            selectinload(WatchStory.articles).selectinload(WatchArticle.matches),
            selectinload(WatchStory.filings),
        )
    ).one()


def test_an_old_page_grouped_into_a_current_story_doesn_t_date_it(
    db: sessionmaker[Session], settings: Settings
) -> None:
    """Story 101's shape: Zen's Cogknit acquisition (15 Oct on), and a 2024 price page a later
    search grouped into it. The story happened on 15 Oct; the page is ignored."""
    now = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
    with db() as session:
        story = add_story(
            session,
            "ZENTEC.NS",
            [
                (
                    "Zen Technologies to acquire 52% stake in Cogknit for ₹135.15 crore",
                    datetime(2026, 10, 15, 8, 8, tzinfo=UTC),
                    datetime(2026, 10, 15, 8, 20, tzinfo=UTC),
                ),
                (
                    "Zen Technologies Ltd Share Historical Prices and Data | IIFL Capital",
                    datetime(2024, 7, 11, 16, 35, tzinfo=UTC),
                    now,
                ),
            ],
        )
        full = loaded(session, story.id)
        assert first_reported(full, WEEK) == datetime(2026, 10, 15, 8, 8, tzinfo=UTC)
        assert not stale_story(full, WEEK)
        # The Telegram item's status line dates the first media report the same way.
        line = status_line(full, settings, now)
        assert line.startswith("Thu 15 Oct 13:38") and "2024" not in line
        # A story of nothing but old pages is old news, and stale.
        old = add_story(session, "APOLLO.NS", [(APOLLO_AUGUST[0], APOLLO_AUGUST[3], now)])
        assert stale_story(loaded(session, old.id), WEEK)
        assert first_reported(loaded(session, old.id), WEEK) == APOLLO_AUGUST[3]


# ---------------------------------------------------------------- the page


@pytest.fixture
def page(tmp_path: Path, settings: Settings, database: Path) -> TestClient:
    watchlist = tmp_path / "watchlist.yaml"
    shutil.copy(FIXTURES / "watchlist.yaml", watchlist)
    app = create_app(
        settings,
        make_read_only_session_factory(make_read_only_engine(database)),
        watchlist_path=watchlist,
        companies_path=tmp_path / "companies.json",
        allowed_hosts=frozenset({"testserver"}),
        scan_inbox=tmp_path / "inbox",
    )
    return TestClient(app)


def scan_done(session: Session, symbol: str, started: datetime) -> None:
    session.add(
        WatchScan(
            request_id=f"r-{started.timestamp()}",
            symbol=symbol,
            requested_at=started,
            started_at=started,
            updated_at=started,
            finished_at=started + timedelta(seconds=5),
            status="done",
            steps=ondemand.new_steps(),
            errors=[],
        )
    )
    session.commit()


def test_a_story_first_seen_today_but_reported_in_august_is_not_this_week(
    page: TestClient, db: sessionmaker[Session]
) -> None:
    now = utcnow()
    august = now - timedelta(days=75)
    with db() as session:
        # Story 194's shape: the old order win, seen today, called high positive just now...
        add_story(
            session,
            "APOLLO.NS",
            [(APOLLO_AUGUST[0], august, now - timedelta(minutes=10))],
            called_at=now - timedelta(minutes=9),
        )
        # ...and this week's real story on the stock, called before it.
        add_story(
            session,
            "APOLLO.NS",
            [
                (
                    "Apollo Micro Systems lists 1.4 crore shares at ₹114 following warrant "
                    "conversion",
                    now - timedelta(days=4),
                    now - timedelta(days=4),
                )
            ],
            call=("primary", "neutral", "low"),
        )
        # A 5-day-old report the last scan turned up: shown, but not "new".
        add_story(
            session,
            "APOLLO.NS",
            [
                (
                    "Apollo Micro Systems board to consider preferential allotment",
                    now - timedelta(days=5),
                    now - timedelta(minutes=10),
                )
            ],
            call=("primary", "neutral", "low"),
        )
        scan_done(session, "APOLLO.NS", now - timedelta(days=2))
        scan_done(session, "APOLLO.NS", now - timedelta(minutes=11))
    ondemand.write_heartbeat(page.app.state.scan_inbox, "idle", now, now)  # type: ignore[attr-defined]

    body = page.get("/watchlist/scan", params={"symbol": "APOLLO.NS"}).text
    latest = body.split('data-panel="latest"')[1].split('data-panel="headlines"')[0]
    headlines = body.split('data-panel="headlines"')[1].split('data-panel="filings"')[0]
    assert "Rs 213.39 crore" not in latest and "Rs 213.39 crore" not in headlines
    assert "preferential allotment" in latest and "watch-new" not in latest  # 5 days: not new
    # Newest report first: the 4-day-old story above the 5-day-old one.
    assert latest.index("1.4 crore shares") < latest.index("preferential allotment")
    assert "Latest (2)" in body

    watchlist_page = page.get("/watchlist").text
    card = watchlist_page.split('data-card-symbol="APOLLO.NS"')[1].split("data-card-symbol=")[0]
    assert "Rs 213.39 crore" not in card
    assert "1.4 crore shares" in card or "preferential allotment" in card
    news = watchlist_page.split('id="watch-news-list"')[1].split("</aside>")[0]
    assert "Rs 213.39 crore" not in news and "1.4 crore shares" in news


def test_a_report_from_this_scan_s_reach_is_new(
    page: TestClient, db: sessionmaker[Session]
) -> None:
    now = utcnow()
    with db() as session:
        scan_done(session, "APOLLO.NS", now - timedelta(days=2))
        scan_done(session, "APOLLO.NS", now - timedelta(minutes=11))
        add_story(
            session,
            "APOLLO.NS",
            [
                (
                    "Apollo Micro Systems wins Rs 120 crore order from BEL",
                    now - timedelta(hours=3),
                    now - timedelta(minutes=10),
                )
            ],
        )
    ondemand.write_heartbeat(page.app.state.scan_inbox, "idle", now, now)  # type: ignore[attr-defined]
    body = page.get("/watchlist/scan", params={"symbol": "APOLLO.NS"}).text
    assert body.count("watch-new") == 1


def test_old_dates_carry_their_year(
    settings: Settings, page: TestClient, db: sessionmaker[Session]
) -> None:
    this_year = datetime.now(UTC).year
    assert _in_zone(datetime(2020, 12, 15, 21, 17, tzinfo=UTC), settings) == "16 Dec 2020 02:47"
    assert re.fullmatch(
        r"\d\d \w\w\w \d\d:\d\d", _in_zone(datetime.now(UTC) - timedelta(days=3), settings)
    )
    assert _day(datetime(2020, 12, 15, 21, 17, tzinfo=UTC), settings) == "16 Dec 2020"
    assert _day(datetime(this_year, 1, 2, 12, tzinfo=UTC).date(), settings, "%a %d %b").endswith(
        "02 Jan"
    )
    # On a page: story 131's old page, alone, read on the story's own page.
    with db() as session:
        story = add_story(
            session, "APOLLO.NS", [(APOLLO_2020[0], APOLLO_2020[3], utcnow())], call=None
        )
    assert "16 Dec 2020" in page.get(f"/watchlist/story/{story.id}").text


# ---------------------------------------------------------------- the rest of the watchlist


def test_scoring_skips_a_call_on_old_news(db: sessionmaker[Session]) -> None:
    now = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
    with db() as session:
        old = add_story(session, "APOLLO.NS", [(APOLLO_AUGUST[0], APOLLO_AUGUST[3], now)])
        fresh = add_story(
            session,
            "HAL.NS",
            [("HAL wins Rs 5,000 crore engine order", now - timedelta(hours=2), now)],
        )
        stories = {c.story_id for c in scorable_calls(session, WEEK)}
    assert fresh.id in stories and old.id not in stories


def test_old_news_is_never_called(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    now = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
    with db() as session:
        old = add_story(
            session, "APOLLO.NS", [(APOLLO_AUGUST[0], APOLLO_AUGUST[3], now)], call=None
        )
        fresh = add_story(
            session, "HAL.NS", [("HAL wins Rs 5,000 crore engine order", now, now)], call=None
        )
        wanted = {
            given.story_id
            for given in stories_to_call(
                session, {s.symbol: s for s in watchlist.stocks}, settings, now
            )
        }
    assert fresh.id in wanted and old.id not in wanted


def test_old_news_stored_today_doesn_t_silence_a_price_move(db: sessionmaker[Session]) -> None:
    now = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
    last_close = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
    with db() as session:
        add_story(session, "APOLLO.NS", [(APOLLO_AUGUST[0], APOLLO_AUGUST[3], now)])
        add_story(session, "HAL.NS", [("HAL wins Rs 5,000 crore engine order", now, now)])
        found = stories_since(session, ["APOLLO.NS", "HAL.NS"], last_close, WEEK)
    assert found == {"APOLLO.NS": False, "HAL.NS": True}


# "A story since the last close" is any story with an item first reported since then, not only
# a story that began then (user, 2026-10-19): news that joins an older story - Zen's Cogknit
# acquisition inside a 13 Oct share-price note - is no "moved, no story yet".
CLOSE = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)  # Friday 15:30 IST
BEGAN = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
MONDAY = datetime(2026, 10, 19, 5, 0, tzinfo=UTC)  # 10:30 IST


def test_news_that_joins_an_older_story_counts_as_a_story_since_the_close(
    db: sessionmaker[Session],
) -> None:
    with db() as session:
        add_story(
            session,
            "ZENTEC.NS",
            [
                ("Zen Technologies Share Price Rises 5.39%", BEGAN, BEGAN),
                ("Zen Technologies to acquire 52% stake in Cogknit", MONDAY, MONDAY),
            ],
        )
        joined = add_story(session, "BDL.NS", [("BDL share price rise streak", BEGAN, BEGAN)])
        session.add(
            WatchFiling(
                key="bdl-order",
                exchange="NSE",
                symbol="BDL.NS",
                company="Bharat Dynamics Limited",
                subject="Bagging/Receiving of orders/contracts",
                description="Bharat Dynamics Limited has informed the Exchange about an order",
                link="",
                filed_at=MONDAY,
                first_seen_at=MONDAY,
                kind="filing",
                story=joined,
            )
        )
        # Joined today only by a report from before the close, and by an August page.
        add_story(
            session,
            "HAL.NS",
            [
                ("HAL share price today", BEGAN, BEGAN),
                ("HAL wins engine order", CLOSE - timedelta(hours=2), MONDAY),
                (APOLLO_AUGUST[0], APOLLO_AUGUST[3], MONDAY),
            ],
        )
        found = stories_since(session, ["ZENTEC.NS", "BDL.NS", "HAL.NS"], CLOSE, WEEK)
    assert found == {"ZENTEC.NS": True, "BDL.NS": True, "HAL.NS": False}


def test_the_catch_up_replay_counts_news_that_joined_an_older_story(
    db: sessionmaker[Session], settings: Settings, watchlist: WatchlistFile
) -> None:
    """The away summary's price moves: Prime Focus down 9% at 10:30, more than twice its
    typical day, on news BASIS had by then inside a story begun on the 13th."""
    typical = {"PFOCUS.NS": Typical(0.04, None)}
    start, end = MONDAY - timedelta(hours=1), MONDAY + timedelta(hours=1)
    with db() as session:
        session.add(
            WatchPrice(symbol="PFOCUS.NS", polled_at=MONDAY, price=91.0, previous_close=100.0)
        )
        story = add_story(
            session,
            "PFOCUS.NS",
            [
                ("Prime Focus share price today", BEGAN, BEGAN),
                ("Income tax searches at Prime Focus", MONDAY - timedelta(minutes=30), MONDAY),
            ],
        )
        session.commit()
        assert moves_in_period(session, watchlist, typical, settings, start, end) == []
        # Had BASIS seen the report only after the move, the move would have been news.
        later = max(story.articles, key=lambda a: a.published_at)
        later.first_seen_at = MONDAY + timedelta(minutes=10)
        session.commit()
        events = moves_in_period(session, watchlist, typical, settings, start, end)
    assert [e.key for e in events] == ["price:PFOCUS.NS:2026-10-19"]
