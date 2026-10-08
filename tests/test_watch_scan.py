"""The watchlist scanner (step 2): sources, storage, grouping, prices and the report.

The network is a MockTransport and embeddings come from FakeEmbedder, as everywhere else.
The NSE items are copied from the real feed of 2026-10-07 (the two Prime Focus notices);
the HAL filing and the news headlines are made up in the shape of real ones.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.cli import feed_checks
from app.config import Settings, WatchlistFile, load_watchlist_file
from app.db import init_db, make_engine, make_session_factory
from app.models import (
    FeedCheck,
    WatchArticle,
    WatchFiling,
    WatchPrice,
    WatchRun,
)
from app.pipeline.fetch import FeedResult, SourceResolver
from app.watch.group import StoryGrouper
from app.watch.prices import Snapshot, in_market_hours
from app.watch.report import watch_report
from app.watch.scan import Watcher
from app.watch.sources import (
    Validators,
    company_key,
    conditional_get,
    google_news_queries,
    parse_nse_announcements,
)
from tests.conftest import make_feed, read_fixture
from tests.fakes import FakeEmbedder

# Thursday 2026-10-08, 11:30 IST: market hours.
T0 = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)
FEED_URL = "https://markets.example.com/rss.xml"
NSE_URL = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
RSS = {"Content-Type": "application/rss+xml; charset=utf-8"}  # as the real feeds send it

NSE_XML = read_fixture("nse_announcements.xml").decode("utf-8")


def rss(*items: tuple[str, str, str]) -> str:
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link><description>{snippet}</description>"
        "<pubDate>Thu, 08 Oct 2026 05:00:00 GMT</pubDate></item>"
        for title, link, snippet in items
    )
    return f'<rss version="2.0"><channel><title>Markets</title>{body}</channel></rss>'


def google_news(*items: tuple[str, str, str]) -> str:
    """Google News items: (title, outlet, link). Titles carry the ' - Outlet' suffix."""
    body = "".join(
        f"<item><title>{title} - {outlet}</title><link>{link}</link>"
        f'<source url="https://{outlet.lower().replace(" ", "")}.com">{outlet}</source>'
        "<pubDate>Thu, 08 Oct 2026 05:00:00 GMT</pubDate></item>"
        for title, outlet, link in items
    )
    return f'<rss version="2.0"><channel><title>Google News</title>{body}</channel></rss>'


HAL_ORDER = (
    "HAL bags Rs 2,000 crore order for Dhruv helicopters",
    "https://markets.example.com/hal-order",
    "Hindustan Aeronautics won the order from the Army.",
)
PF_RAID = (
    "Prime Focus shares tank 8% after Income Tax raids at Mumbai offices: Exclusive",
    "https://markets.example.com/pf-raid",
    "",
)
RUSSIA = (
    "Russia partners Adani Defence, HAL, BDL for missiles, fighter upgrades, defence "
    "production: Report",
    "https://markets.example.com/russia",
    "",
)
TOP_PICKS = ("HAL, BEL, BDL shares: top picks", "https://markets.example.com/picks", "")
UNRELATED = ("Sensex falls 300 points as IT stocks drag", "https://markets.example.com/sensex", "")


@pytest.fixture
def db(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(tmp_path / "watch.db")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def watchlist() -> WatchlistFile:
    return load_watchlist_file()


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Web:
    """A MockTransport whose answers a test can change between passes. NSE answers 304 to a
    request carrying If-Modified-Since, as the real one does."""

    def __init__(self) -> None:
        self.feed = rss(HAL_ORDER, PF_RAID, RUSSIA, TOP_PICKS, UNRELATED)
        self.google = google_news()
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "news.google.com":
            return httpx.Response(200, text=self.google, headers=RSS)
        if str(request.url) == NSE_URL:
            if request.headers.get("if-modified-since"):
                return httpx.Response(304)
            return httpx.Response(
                200,
                text=NSE_XML,
                headers={**RSS, "Last-Modified": "Thu, 08 Oct 2026 05:59:00 GMT"},
            )
        if str(request.url) == FEED_URL:
            return httpx.Response(200, text=self.feed, headers=RSS)
        return httpx.Response(404)


class FakeSnapshots:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.calls: list[str] = []

    def snapshot(self, symbol: str) -> Snapshot:
        self.calls.append(symbol)
        if symbol in self.fail:
            raise RuntimeError("no data")
        return Snapshot(
            symbol,
            price=97.0,
            previous_close=100.0,
            last_trade_at=T0 - timedelta(seconds=4),
            newest_bar_at=T0 - timedelta(seconds=30),
            day_open=100.0,
            day_high=100.5,
            day_low=96.0,
        )


def make_watcher(
    settings: Settings,
    db: sessionmaker[Session],
    watchlist: WatchlistFile,
    web: Web,
    clock: Clock,
    snapshots: FakeSnapshots | None = None,
    embed: bool = True,
) -> Watcher:
    feed = make_feed(name="Markets Daily", url=FEED_URL, region="IN")
    other = make_feed(name="Other Outlet", url="https://other.example.com/rss", region="IN")
    return Watcher(
        settings,
        db,
        watchlist,
        [feed],
        SourceResolver([feed, other]),
        FakeEmbedder() if embed else None,
        snapshots,
        transport=httpx.MockTransport(web),
        clock=clock,
    )


def verdicts(session: Session, title_start: str) -> dict[str, str]:
    article = session.scalars(
        select(WatchArticle).where(WatchArticle.title.startswith(title_start))
    ).one()
    return {m.symbol.removesuffix(".NS"): m.verdict for m in article.matches}


# ---------------------------------------------------------------- sources


def test_nse_announcements_are_parsed_and_classified() -> None:
    found = {(a.company, a.filed_at): a for a in parse_nse_announcements(NSE_XML.encode())}
    asked = found[("Prime Focus Limited", datetime(2026, 10, 7, 5, 35, 31, tzinfo=UTC))]
    reply = found[("Prime Focus Limited", datetime(2026, 10, 7, 10, 3, 17, tzinfo=UTC))]
    assert asked.kind == "clarification_sought" and reply.kind == "company_reply"
    assert asked.subject == "News Verification"
    assert asked.quoted_headline == PF_RAID[0]  # word for word, the trailing period dropped
    assert reply.link.endswith("signed.pdf") and asked.link == ""
    assert asked.key != reply.key
    tata = next(a for a in found.values() if a.company == "Tata Steel Limited")
    assert tata.kind == "filing" and tata.quoted_headline is None


def test_company_names_compare_the_way_nse_writes_them() -> None:
    assert company_key("Data Patterns (India) Limited") == company_key("DATA PATTERNS (INDIA) LTD")
    assert company_key("Hindustan Aeronautics Limited") == company_key(
        "HINDUSTAN AERONAUTICS LIMITED"
    )
    assert company_key("Bharat Dynamics Limited") != company_key("Bharat Forge Limited")


def test_google_news_batches_every_alias_quoted(
    settings: Settings, watchlist: WatchlistFile
) -> None:
    queries = google_news_queries(watchlist.watchlist, settings)
    aliases = {a for s in watchlist.stocks for a in (*s.aliases.strong, *s.aliases.weak)}
    assert len(queries) == -(-len(aliases) // settings.watch.google_news_terms_per_query)
    joined = " ".join(httpx.URL(q.url).params["q"] for q in queries)
    for alias in aliases:
        assert f'"{alias}"' in joined
    assert all(q.is_google_news and "when:1d" in httpx.URL(q.url).params["q"] for q in queries)
    assert "Brent" not in joined  # commodities are never searched


def test_a_conditional_get_remembers_what_the_server_said(settings: Settings) -> None:
    import asyncio

    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        if request.url.path == "/gone":
            return httpx.Response(404)
        return httpx.Response(200, text="<rss/>", headers={"ETag": '"v1"'})

    async def go(url: str, validators: dict[str, Validators]):  # noqa: ANN202
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await conditional_get(client, url, validators, settings)

    validators: dict[str, Validators] = {}
    first = asyncio.run(go("https://a.example/feed", validators))
    second = asyncio.run(go("https://a.example/feed", validators))
    gone = asyncio.run(go("https://a.example/gone", validators))
    assert first.status == "ok" and first.content == b"<rss/>"
    assert second.status == "not_modified" and second.content is None
    assert seen[1]["if-none-match"] == '"v1"'
    assert gone.status == "error" and gone.error == "HTTP 404"


# ---------------------------------------------------------------- grouping


def test_a_story_only_takes_items_that_share_a_stock() -> None:
    grouper: StoryGrouper[int] = StoryGrouper(0.55, 0.45, timedelta(hours=72))
    vector = np.array([1.0, 0.0], dtype=np.float32)
    grouper.add(1, vector, {"HAL.NS"}, T0)
    assert grouper.match(vector, {"BDL.NS"}, T0) is None  # same words, another company
    assert grouper.match(vector, {"BDL.NS", "HAL.NS"}, T0) == 1
    assert grouper.match(vector, {"HAL.NS"}, T0 + timedelta(hours=73)) is None  # too old
    sideways = np.array([0.6, 0.8], dtype=np.float32)  # cosine 0.6 to the story
    assert grouper.match(sideways, {"HAL.NS"}, T0) == 1
    grouper.add(1, sideways, {"BDL.NS"}, T0)
    assert grouper.match(vector, {"BDL.NS"}, T0) == 1  # the story now touches BDL too


# ---------------------------------------------------------------- the scan


def test_a_feed_pass_stores_what_names_a_watchlist_stock(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    web, clock = Web(), Clock(T0)
    watcher = make_watcher(settings, db, watchlist, web, clock)
    result = watcher.run_job("feeds", T0)

    assert result.new_articles == 4  # the Sensex headline names no watchlist stock
    assert result.new_filings == 3  # Tata Steel is not on the watchlist
    with db() as session:
        assert verdicts(session, "HAL bags") == {"HAL": "keep"}
        assert verdicts(session, "HAL, BEL, BDL shares") == {"HAL": "mention", "BDL": "mention"}
        assert verdicts(session, "Russia partners") == {"HAL": "keep", "BDL": "keep"}
        article = session.scalars(
            select(WatchArticle).where(WatchArticle.url.endswith("pf-raid"))
        ).one()
        assert article.first_seen_at == T0 and article.source_name == "Markets Daily"
        assert [(s.feed_name, s.via) for s in article.sightings] == [("Markets Daily", "rss")]

        # NSE's notices quote the media headline: both join the raid story, word for word.
        notices = session.scalars(
            select(WatchFiling)
            .where(WatchFiling.symbol == "PFOCUS.NS")
            .order_by(WatchFiling.filed_at)
        ).all()
        assert [f.kind for f in notices] == ["clarification_sought", "company_reply"]
        assert {f.story_id for f in notices} == {article.story_id}
        hal = session.scalars(select(WatchFiling).where(WatchFiling.symbol == "HAL.NS")).one()
        assert hal.story_id is not None and hal.subject.startswith("Bagging")

        checks = {c.feed_name: c for c in session.scalars(select(FeedCheck))}
        assert checks["Markets Daily"].status == "ok" and checks["Markets Daily"].entries == 5
        assert checks["Markets Daily"].new_entries is None  # nothing to compare with yet
        assert checks["NSE announcements"].entries == 4
        run = session.scalars(select(WatchRun)).one()
        assert run.job == "feeds" and run.new_articles == 4 and run.new_filings == 3


def test_a_second_pass_adds_nothing_and_nse_answers_304(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    web, clock = Web(), Clock(T0)
    watcher = make_watcher(settings, db, watchlist, web, clock)
    watcher.run_job("feeds", T0)
    later = T0 + timedelta(minutes=10)
    clock.now = later
    result = watcher.run_job("feeds", later)

    assert (result.new_articles, result.new_filings) == (0, 0)
    nse_requests = [r for r in web.requests if str(r.url) == NSE_URL]
    assert nse_requests[-1].headers["if-modified-since"] == "Thu, 08 Oct 2026 05:59:00 GMT"
    with db() as session:
        latest = {
            c.feed_name: c
            for c in session.scalars(select(FeedCheck).where(FeedCheck.checked_at == later))
        }
        assert latest["NSE announcements"].status == "not_modified"
        assert latest["Markets Daily"].new_entries == 0
        assert session.scalar(select(WatchArticle.id).where(WatchArticle.id > 4)) is None


def test_google_news_and_the_outlets_own_feed_are_one_article(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    web, clock = Web(), Clock(T0)
    watcher = make_watcher(settings, db, watchlist, web, clock)
    watcher.run_job("feeds", T0)
    web.google = google_news(
        (HAL_ORDER[0], "Markets Daily", "https://news.google.com/rss/articles/abc"),
        (
            "Bharat Dynamics wins Rs 500 crore missile contract",
            "Other Outlet",
            "https://news.google.com/rss/articles/def",
        ),
    )
    later = T0 + timedelta(minutes=20)
    clock.now = later
    result = watcher.run_job("google_news", later)

    assert result.new_articles == 1  # only the BDL contract is new
    with db() as session:
        hal = session.scalars(
            select(WatchArticle).where(WatchArticle.title.startswith("HAL bags"))
        ).one()
        assert hal.first_seen_at == T0  # BASIS saw it first through the outlet's own feed
        assert [s.via for s in hal.sightings] == ["rss", "google_news"]
        bdl = session.scalars(
            select(WatchArticle).where(WatchArticle.title.startswith("Bharat"))
        ).one()
        assert bdl.source_name == "Other Outlet" and bdl.sightings[0].via == "google_news"
        assert verdicts(session, "Bharat") == {"BDL": "keep"}


def test_the_outlets_snippet_can_change_a_google_news_verdict(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    """Google News carries no snippet. A weak alias with no context there is a drop; the
    outlet's own feed brings the snippet, and its context makes it a keep."""
    web, clock = Web(), Clock(T0)
    web.feed = rss()
    web.google = google_news(
        (
            "HAL chief meets Japanese delegation",
            "Markets Daily",
            "https://news.google.com/rss/articles/x",
        )
    )
    watcher = make_watcher(settings, db, watchlist, web, clock)
    watcher.run_job("google_news", T0)
    with db() as session:
        assert verdicts(session, "HAL chief") == {"HAL": "drop"}

    web.feed = rss(
        (
            "HAL chief meets Japanese delegation",
            "https://markets.example.com/hal-chief",
            "The aircraft maker discussed fighter jet co-production.",
        )
    )
    clock.now = T0 + timedelta(minutes=10)
    watcher.run_job("feeds", clock.now)
    with db() as session:
        assert verdicts(session, "HAL chief") == {"HAL": "keep"}
        assert session.scalars(select(WatchArticle)).all()[0].snippet.startswith("The aircraft")


def test_stories_group_across_passes_and_stocks(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    web, clock = Web(), Clock(T0)
    web.feed = rss(HAL_ORDER)
    watcher = make_watcher(settings, db, watchlist, web, clock)
    watcher.run_job("feeds", T0)
    # The same event from another outlet, in the next pass: it joins the story.
    web.feed = rss(
        HAL_ORDER,
        (
            "Army orders Dhruv helicopters from HAL in Rs 2,000 crore deal",
            "https://markets.example.com/hal-2",
            "Hindustan Aeronautics won the order.",
        ),
        ("BDL bags Rs 2,000 crore order for missiles", "https://markets.example.com/bdl", ""),
    )
    clock.now = T0 + timedelta(minutes=10)
    watcher.run_job("feeds", clock.now)
    with db() as session:
        by_title = {a.title: a.story_id for a in session.scalars(select(WatchArticle))}
        assert (
            by_title[HAL_ORDER[0]]
            == by_title["Army orders Dhruv helicopters from HAL in Rs 2,000 crore deal"]
        )
        # Worded alike, but about BDL: a story only takes items sharing one of its stocks.
        assert by_title["BDL bags Rs 2,000 crore order for missiles"] != by_title[HAL_ORDER[0]]


# ---------------------------------------------------------------- prices and scheduling


def test_market_hours_are_weekdays_from_the_open_to_just_after_the_close(
    settings: Settings,
) -> None:
    ist = settings.tz

    def at(day: int, hour: int, minute: int) -> datetime:
        return datetime(2026, 10, day, hour, minute, tzinfo=ist)

    assert not in_market_hours(at(8, 9, 14), settings)
    assert in_market_hours(at(8, 9, 15), settings)
    assert in_market_hours(at(8, 15, 34), settings)
    assert not in_market_hours(at(8, 15, 36), settings)
    assert not in_market_hours(at(10, 11, 0), settings)  # a Saturday


def test_prices_are_polled_for_stocks_indices_and_the_benchmark(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    snapshots = FakeSnapshots(fail={"ZENTEC.NS"})
    watcher = make_watcher(settings, db, watchlist, Web(), Clock(T0), snapshots)
    result = watcher.run_job("prices", T0)

    expected = [s.symbol for s in watchlist.stocks] + ["NIFTY_IND_DEFENCE.NS", "^NSEI"]
    assert snapshots.calls == expected
    assert result.prices == len(expected) - 1 and len(result.errors) == 1
    with db() as session:
        rows = {row.symbol: row for row in session.scalars(select(WatchPrice))}
        assert (
            rows["ZENTEC.NS"].error == "RuntimeError: no data" and rows["ZENTEC.NS"].price is None
        )
        assert rows["HAL.NS"].price == 97.0 and rows["HAL.NS"].last_trade_at is not None


def test_the_tick_runs_each_job_when_it_is_due(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    settings.watch.feed_stale_hours = 48  # scheduling only: no feed warning in the way
    clock = Clock(T0)
    watcher = make_watcher(settings, db, watchlist, Web(), clock, FakeSnapshots())
    watcher.find_gap = lambda now: None  # type: ignore[method-assign]  # jumps aren't gaps here
    assert [r.job for r in watcher.tick()] == ["feeds", "google_news", "prices"]
    clock.now = T0 + timedelta(minutes=5)
    assert [r.job for r in watcher.tick()] == ["prices"]
    clock.now = T0 + timedelta(minutes=10)
    assert [r.job for r in watcher.tick()] == ["feeds", "prices"]
    clock.now = T0 + timedelta(minutes=20)
    assert [r.job for r in watcher.tick()] == ["feeds", "google_news", "prices"]
    clock.now = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)  # 19:30 IST: no prices, hourly news
    assert [r.job for r in watcher.tick()] == ["feeds", "google_news"]
    clock.now += timedelta(minutes=30)
    assert [r.job for r in watcher.tick()] == ["feeds"]


def test_a_pass_where_nothing_answered_is_retried_a_minute_later(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    """No network yet, right after a wake: don't wait a whole interval to try again."""
    settings.http.max_attempts = 1  # no real backoff waits in a test
    clock = Clock(datetime(2026, 10, 8, 14, 0, tzinfo=UTC))  # evening: no prices
    offline = httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("no route"))
    )
    watcher = make_watcher(settings, db, watchlist, Web(), clock)
    watcher.transport = offline
    assert [r.job for r in watcher.tick()] == ["feeds", "google_news"]
    clock.now += timedelta(minutes=1)
    watcher.transport = httpx.MockTransport(Web())
    assert [r.job for r in watcher.tick()] == ["feeds", "google_news"]
    clock.now += timedelta(minutes=1)
    assert watcher.tick() == []  # this time it worked: back to the normal interval


# ---------------------------------------------------------------- health, wake, report


def test_the_pipeline_records_a_check_for_every_feed() -> None:
    ok = FeedResult(make_feed(name="A", url="https://a.example/rss"), status_code=200)
    broken = FeedResult(
        make_feed(name="B", url="https://b.example/rss"), status_code=403, error="HTTP 403"
    )
    rows = feed_checks([ok, broken], T0)
    assert [(r.feed_name, r.status, r.http_status, r.kind) for r in rows] == [
        ("A", "ok", 200, "pipeline"),
        ("B", "error", 403, "pipeline"),
    ]


def test_the_report_counts_volumes_multi_stock_stories_and_first_sources(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    web, clock = Web(), Clock(T0)
    web.feed = rss(HAL_ORDER)
    # No embedder: every item is its own story (but NSE's reply still joins its request), so
    # the counts don't depend on how the fake embedder's hashes happen to collide.
    watcher = make_watcher(settings, db, watchlist, web, clock, FakeSnapshots(), embed=False)
    watcher.run_job("feeds", T0)  # a backlog pass: the first after a start
    web.feed = rss(HAL_ORDER, RUSSIA, TOP_PICKS)
    clock.now = T0 + timedelta(minutes=10)
    watcher.run_job("feeds", clock.now)
    watcher.run_job("prices", clock.now)
    with db() as session:
        text = watch_report(session, settings, watchlist, T0 + timedelta(hours=1), 1)

    assert "| HAL | 2 / 1 / 0 |" in text  # HAL order + Russia kept, the picks list a mention
    assert "| Bharat Dynamics | 1 / 1 / 0 |" in text
    assert "1 of 4 stories with a keep are a keep for two or more stocks" in text
    assert "Russia partners" in text
    # The HAL order and both filings came in the backlog pass; Russia is the one counted.
    assert "1 story counted, 3 left out as backlog" in text
    assert "| Markets Daily | 1 |" in text
    assert "| HAL.NS | 1 | 0 |" in text  # one poll, no errors
    assert "None: the task never ran" in text


# ---------------------------------------------------------------- config


def test_groups_must_exist_and_hold_only_stocks(tmp_path: Path) -> None:
    def load(text: str) -> Callable[[], WatchlistFile]:
        path = tmp_path / "w.yaml"
        path.write_text(text, encoding="utf-8")
        return lambda: load_watchlist_file(path, assets=[])

    stock = "{type: stock, symbol: HAL.NS, name: HAL, aliases: {strong: [Hindustan Aeronautics]}"
    with pytest.raises(ValueError, match="not defined under `groups`"):
        load(f"watchlist:\n  - {stock}, group: defence}}\n")()
    with pytest.raises(ValueError, match="only stocks belong to a peer group"):
        load(
            "groups: {defence: {name: Defence}}\nwatchlist:\n"
            "  - {type: commodity, symbol: GC=F, group: defence}\n"
        )()
    good = load(
        f"groups: {{defence: {{name: Defence}}}}\nwatchlist:\n  - {stock}, group: defence}}\n"
    )()
    assert good.stocks[0].group == "defence"


def test_the_watchlist_puts_seven_defence_names_in_one_group(watchlist: WatchlistFile) -> None:
    defence = [s.symbol for s in watchlist.stocks if s.group == "defence"]
    assert len(defence) == 7 and "PFOCUS.NS" not in defence
    assert watchlist.groups["defence"].index == "NIFTY_IND_DEFENCE.NS"
