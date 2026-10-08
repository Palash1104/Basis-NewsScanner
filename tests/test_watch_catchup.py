"""Catching up after the laptop was off: the gap, the sources, and what none could reach."""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, WatchlistFile, load_watchlist_file
from app.db import init_db, make_engine, make_session_factory
from app.models import (
    WatchAlert,
    WatchArticle,
    WatchCall,
    WatchCursor,
    WatchFiling,
    WatchPrice,
    WatchRun,
    WatchStory,
)
from app.watch.alerts import Outgoing, deliver, followups
from app.watch.catchup import Gap, SourceCoverage, find_gap, possible_gaps
from app.watch.prices import backfill_rows
from app.watch.scan import BSE_CURSOR
from app.watch.sources import (
    NSE_TIMEZONE as IST,
)
from app.watch.sources import (
    Announcement,
    google_news_window,
    parse_nse_api,
    read_bse_announcements,
)
from tests.test_watch_scan import NSE_URL, PF_RAID, RSS, Clock, Web, make_watcher

# Thursday 2026-10-08 06:00 UTC = 11:30 IST.
NOW = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)
EVERY = timedelta(minutes=10)

# NSE's API rows for the two Prime Focus notices, as it returned them on 2026-10-08.
API_ROWS = [
    {
        "symbol": "PFOCUS",
        "sm_name": "Prime Focus Limited",
        "desc": "News Verification",
        "attchmntText": "The Exchange has sought clarification from Prime Focus Limited with "
        "respect to recent news item captioned Prime Focus shares tank 8% after Income Tax "
        "raids at Mumbai offices: Exclusive.  The response from the Company is attached.",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/PFOCUS_07102026152825_"
        "PFLReplytoNSE07102026signed.pdf",
        "an_dt": "07-Oct-2026 15:33:17",
    },
    {
        "symbol": "PFOCUS",
        "sm_name": "Prime Focus Limited",
        "desc": "News Verification",
        "attchmntText": "The Exchange has sought clarification from Prime Focus Limited with "
        "respect to recent news item captioned Prime Focus shares tank 8% after Income Tax "
        "raids at Mumbai offices: Exclusive.  The response from the Company is awaited.",
        "attchmntFile": "-",
        "an_dt": "07-Oct-2026 11:05:31",
    },
]


@pytest.fixture
def db(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(tmp_path / "catchup.db")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def watchlist() -> WatchlistFile:
    return load_watchlist_file()


# ---------------------------------------------------------------- the gap


def test_a_scanner_that_kept_up_has_no_gap() -> None:
    assert find_gap(NOW - timedelta(minutes=12), NOW, EVERY, 7) is None
    assert find_gap(None, NOW, EVERY, 7) is None  # never scanned: nothing to resume from


def test_a_gap_runs_from_the_last_completed_scan_and_is_capped() -> None:
    gap = find_gap(NOW - timedelta(hours=10), NOW, EVERY, 7)
    assert gap is not None and gap.start == NOW - timedelta(hours=10) and not gap.capped
    long = find_gap(NOW - timedelta(days=12), NOW, EVERY, 7)
    assert long is not None and long.capped and long.start == NOW - timedelta(days=7)


def test_google_news_is_searched_over_exactly_the_missed_period() -> None:
    assert google_news_window(NOW - timedelta(hours=10, minutes=20), NOW) == "when:11h"
    assert google_news_window(NOW - timedelta(days=5), NOW) == "after:2026-10-02"


def test_what_no_source_reached_is_a_possible_gap() -> None:
    gap = Gap(NOW - timedelta(hours=10), NOW, NOW - timedelta(hours=10))
    covered = [
        SourceCoverage("news", "Livemint", NOW - timedelta(hours=7)),
        SourceCoverage("news", "Google News", None, "5 searches failed"),
        SourceCoverage("filings", "NSE", gap.start),
        SourceCoverage("prices", "Yahoo", gap.start),
    ]
    gaps = possible_gaps(gap, covered)
    assert [(g["what"], g["from"], g["to"]) for g in gaps] == [
        ("news", gap.start.isoformat(), (NOW - timedelta(hours=7)).isoformat())
    ]
    assert "5 searches failed" in gaps[0]["why"]
    # One feed reaching back far enough covers it: news is a union of sources.
    covered.append(SourceCoverage("news", "BusinessLine", NOW - timedelta(hours=80)))
    assert possible_gaps(gap, covered) == []


# ---------------------------------------------------------------- the sources


def test_nse_api_rows_become_the_same_announcements_as_the_rss() -> None:
    found = parse_nse_api(API_ROWS)
    assert [a.kind for a in found] == ["company_reply", "clarification_sought"]
    assert found[1].filed_at == datetime(2026, 10, 7, 5, 35, 31, tzinfo=UTC)
    assert found[1].link == "" and found[0].link.endswith("signed.pdf")


def test_backfill_samples_minute_bars_against_the_previous_close(settings: Settings) -> None:
    open_ = datetime(2026, 10, 7, 3, 45, tzinfo=UTC)  # 09:15 IST
    bars = [(open_ + timedelta(minutes=i), 100.0 - i) for i in range(12)]  # 09:15-09:26
    rows = backfill_rows("PFOCUS.NS", bars, {date(2026, 10, 6): 110.0}, 5, settings)
    assert [r.polled_at.astimezone(settings.tz).strftime("%H:%M") for r in rows] == [
        "09:16",
        "09:21",
        "09:26",
        "09:27",  # the last bar, whatever its minute
    ]
    assert all(r.previous_close == 110.0 and r.backfill for r in rows)
    assert rows[-1].day_low == 89.0 and rows[-1].day_high == 100.0


# ---------------------------------------------------------------- end to end


class CatchUpWeb(Web):
    """Adds NSE's API: the Prime Focus notices for PFOCUS, nothing for anyone else."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.nseindia.com":
            self.requests.append(request)
            rows = API_ROWS if request.url.params.get("symbol") == "PFOCUS" else []
            return httpx.Response(200, text=json.dumps({"data": rows}))
        return super().__call__(request)


class FakeHistory:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def session_closes(self, symbol: str, days: int) -> dict[date, float]:
        return {date(2026, 10, 7): 100.0}

    def minute_closes(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        self.calls.append(symbol)
        opening = datetime(2026, 10, 8, 3, 45, tzinfo=UTC)
        return [(opening + timedelta(minutes=i), 100.0 - i * 0.5) for i in range(30)]


def test_a_catch_up_reads_every_source_and_records_what_it_reached(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    web, clock = CatchUpWeb(), Clock(NOW)
    watcher = make_watcher(settings, db, watchlist, web, clock)
    watcher.history = FakeHistory()
    with db() as session:  # the last completed scan, before the laptop slept
        session.add(
            WatchRun(job="feeds", started_at=NOW - timedelta(hours=10), entries=900, errors=[])
        )
        session.commit()

    watcher.run_job("feeds", NOW)

    searched = [r.url for r in web.requests if r.url.host == "news.google.com"]
    assert searched and all("when:11h" in str(url.params["q"]) for url in searched)
    asked = {r.url.params["symbol"] for r in web.requests if r.url.host == "www.nseindia.com"}
    assert asked == {s.nse_symbol for s in watchlist.stocks}
    with db() as session:
        filings = session.scalars(
            select(WatchFiling).where(WatchFiling.symbol == "PFOCUS.NS")
        ).all()
        # The RSS and the API both carried the two notices: stored once each.
        assert len(filings) == 2
        backfilled = session.scalars(select(WatchPrice).where(WatchPrice.backfill)).all()
        assert backfilled and {row.symbol for row in backfilled} >= {"HAL.NS", "^NSEI"}
        run = session.scalars(select(WatchRun).where(WatchRun.job == "catchup")).one()
        details = run.details or {}
        assert details["gap"]["start"] == (NOW - timedelta(hours=10)).isoformat()
        assert details["possible_gaps"] == []  # Google News reached the whole gap
        kinds = {source["kind"] for source in details["sources"]}
        assert kinds == {"news", "filings", "prices"}
        assert details["summary_sent"] is False


def test_no_catch_up_when_the_scanner_kept_up(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    web = CatchUpWeb()
    watcher = make_watcher(settings, db, watchlist, web, Clock(NOW))
    with db() as session:
        session.add(
            WatchRun(job="feeds", started_at=NOW - timedelta(minutes=10), entries=900, errors=[])
        )
        session.commit()
    watcher.run_job("feeds", NOW)
    assert not [r for r in web.requests if r.url.host in ("news.google.com", "www.nseindia.com")]
    with db() as session:
        assert session.scalars(select(WatchRun).where(WatchRun.job == "catchup")).first() is None


def test_rss_and_nse_mocks_still_answer(settings: Settings) -> None:
    """The shared Web mock serves RSS with the real content type (a guard for the above)."""
    response = Web()(httpx.Request("GET", NSE_URL))
    assert response.headers["content-type"] == RSS["Content-Type"]


# ---------------------------------------------------------------- BSE, via the PEAD tool

# The PEAD tool's table as it creates it (pead_tool.py publish_announcements).
PEAD_SCHEMA = (
    "CREATE TABLE announcements (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "id TEXT NOT NULL UNIQUE, exchange TEXT, company TEXT, code TEXT, isin TEXT, "
    "category TEXT, headline TEXT, attachment_url TEXT, exchange_time TEXT, fetched_at TEXT)"
)
PFOCUS_ISIN = "INE367G01020"
FAR_BACK = datetime(2026, 1, 1, tzinfo=UTC)


def pead_write(path: Path, rows: list[tuple[str, ...]]) -> Path:
    """Append rows the way the PEAD tool does: one insert per announcement, in order.
    Exchange times are IST without a zone; fetched_at carries its offset."""
    conn = sqlite3.connect(path)
    conn.execute(PEAD_SCHEMA.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
    conn.executemany(
        "INSERT OR IGNORE INTO announcements (id, exchange, company, code, isin, category, "
        "headline, attachment_url, exchange_time, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def bse_row(
    id_: str,
    isin: str | None,
    exchange_time: str,
    fetched_at: str,
    exchange: str = "BSE",
    category: str = "Company Update",
    company: str = "Prime Focus Ltd",
    headline: str | None = None,
) -> tuple[str, ...]:
    return (
        id_,
        exchange,
        company,
        "532748",
        isin,
        category,
        headline or f"{company} has informed the Exchange about {id_}",
        f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{id_}.pdf",
        exchange_time,
        fetched_at,
    )


def with_bse(settings: Settings, path: Path) -> Settings:
    watch = settings.watch.model_copy(update={"bse_announcements_db": str(path)})
    return settings.model_copy(update={"watch": watch})


def test_bse_rows_are_read_by_seq_after_the_cursor_never_by_exchange_time(
    tmp_path: Path,
) -> None:
    path = pead_write(
        tmp_path / "announcements.db",
        [
            bse_row("a", PFOCUS_ISIN, "2026-10-08T10:05:00", "2026-10-08T10:06:10+05:30"),
            bse_row("b", "INE000000000", "2026-10-08T10:07:00", "2026-10-08T10:08:00+05:30"),
            bse_row("c", PFOCUS_ISIN, "2026-10-08T10:09:00", "2026-10-08T10:10:00+05:30", "NSE"),
            bse_row("e", None, "2026-10-08T10:11:00", "2026-10-08T10:12:00+05:30"),
        ],
    )
    isins = {PFOCUS_ISIN: "PFOCUS.NS"}
    first = read_bse_announcements(path, isins, None, FAR_BACK)
    assert first.error is None and first.position == 4
    # Only the watchlist company's BSE row: not another company, not the NSE copy (BASIS
    # reads NSE itself), not one without an ISIN.
    assert [(symbol, a.exchange) for symbol, a in first.found] == [("PFOCUS.NS", "BSE")]
    found = first.found[0][1]
    assert found.filed_at == datetime(2026, 10, 8, 4, 35, tzinfo=UTC)  # 10:05 IST
    assert found.subject == "Company Update" and found.kind == "filing"
    assert found.link.endswith("/a.pdf")
    assert found.key != Announcement(**{**found.__dict__, "exchange": "NSE"}).key

    # The PEAD tool backfills a filing from 02:00, long before anything read so far: it is
    # written after them, so it is read, however old its exchange time.
    pead_write(
        path, [bse_row("f", PFOCUS_ISIN, "2026-10-08T02:00:00", "2026-10-08T10:30:00+05:30")]
    )
    late = read_bse_announcements(path, isins, first.position, FAR_BACK)
    assert [a.link.rsplit("/", 1)[1] for _, a in late.found] == ["f.pdf"]
    assert late.found[0][1].filed_at == datetime(2026, 10, 7, 20, 30, tzinfo=UTC)
    assert late.position == 5
    again = read_bse_announcements(path, isins, late.position, FAR_BACK)
    assert again.found == [] and again.position == 5


def test_a_first_read_keeps_a_week_and_a_cursor_read_keeps_everything(tmp_path: Path) -> None:
    path = pead_write(
        tmp_path / "announcements.db",
        [bse_row("old", PFOCUS_ISIN, "2026-09-20T10:00:00", "2026-10-08T09:30:00+05:30")],
    )
    isins = {PFOCUS_ISIN: "PFOCUS.NS"}
    week = NOW - timedelta(days=7)
    assert read_bse_announcements(path, isins, None, week).found == []  # first read: a week
    # Behind a cursor nothing is dropped for its age: it is new to BASIS.
    assert len(read_bse_announcements(path, isins, 0, week).found) == 1
    # A cursor past the end means the file was recreated: read it again from the start.
    recreated = read_bse_announcements(path, isins, 99, FAR_BACK)
    assert len(recreated.found) == 1 and recreated.position == 1


def test_a_missing_or_broken_shared_file_is_an_error_not_a_crash(tmp_path: Path) -> None:
    missing = read_bse_announcements(tmp_path / "nope.db", {PFOCUS_ISIN: "PFOCUS.NS"}, 3, NOW)
    assert missing.found == [] and missing.position is None
    assert missing.error and "hasn't run" in missing.error
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all, just bytes" * 10)
    bad = read_bse_announcements(broken, {PFOCUS_ISIN: "PFOCUS.NS"}, 3, NOW)
    assert bad.found == [] and bad.position is None and bad.error


def test_the_shared_file_is_opened_read_only(tmp_path: Path) -> None:
    path = pead_write(tmp_path / "announcements.db", [])
    before = path.stat().st_mtime_ns
    read_bse_announcements(path, {PFOCUS_ISIN: "PFOCUS.NS"}, None, FAR_BACK)
    assert path.stat().st_mtime_ns == before
    assert not (tmp_path / "announcements.db-journal").exists()


def test_a_feed_pass_stores_bse_filings_once_and_keeps_its_place(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, tmp_path: Path
) -> None:
    path = pead_write(
        tmp_path / "announcements.db",
        [bse_row("a", PFOCUS_ISIN, "2026-10-08T11:20:00", "2026-10-08T11:21:00+05:30")],
    )
    watcher = make_watcher(with_bse(settings, path), db, watchlist, CatchUpWeb(), Clock(NOW))
    watcher.run_job("feeds", NOW)
    # A new scanner (a restart) carries on from the stored cursor: nothing is read twice.
    again = make_watcher(with_bse(settings, path), db, watchlist, CatchUpWeb(), Clock(NOW))
    again.run_job("feeds", NOW + EVERY)
    with db() as session:
        filings = session.scalars(select(WatchFiling).where(WatchFiling.exchange == "BSE")).all()
        assert [(f.symbol, f.subject) for f in filings] == [("PFOCUS.NS", "Company Update")]
        assert filings[0].story_id is not None
        assert session.get(WatchCursor, BSE_CURSOR).position == 1
        # Read within minutes of filing: not late, so no summary run.
        assert session.scalars(select(WatchRun).where(WatchRun.job == "late")).all() == []


def test_filings_the_pead_tool_backfills_after_a_catch_up_go_into_a_summary_not_alerts(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, tmp_path: Path
) -> None:
    """The case (user, 2026-10-08): BASIS catches up at 09:00, the PEAD tool is started at
    09:30 and writes filings from 02:00 and from before the gap began. BASIS reads them by
    write order, so none is skipped, and they go into one "while you were away" summary -
    not alerts of their own - while a filing that is fresh still gets its alert."""

    def ist(day: int, hour: int, minute: int) -> datetime:
        return datetime(2026, 10, day, hour, minute, tzinfo=IST).astimezone(UTC)

    isin = {stock.symbol: stock.isin for stock in watchlist.stocks}
    path = pead_write(tmp_path / "announcements.db", [])
    settings = with_bse(settings, path)
    clock = Clock(ist(8, 9, 0))
    watcher = make_watcher(settings, db, watchlist, CatchUpWeb(), clock)
    watcher.history = FakeHistory()
    sent: list[str] = []
    watcher.sender = sent.extend
    with db() as session:  # the last completed scan, at 22:00 the night before
        session.add(WatchRun(job="feeds", started_at=ist(7, 22, 0), entries=900, errors=[]))
        session.commit()

    watcher.run_job("feeds", ist(8, 9, 0))  # the catch-up: the PEAD tool isn't running yet
    watcher.run_job("alerts", ist(8, 9, 11))
    for minute in (10, 20, 30):  # BASIS keeps scanning; nothing new in the file
        watcher.run_job("feeds", ist(8, 9, minute))
    sent.clear()

    # 09:30: the PEAD tool starts and catches up on the night, then reads a fresh one.
    pead_write(
        path,
        [
            bse_row(
                "night",
                isin["BDL.NS"],
                "2026-10-08T02:00:00",
                "2026-10-08T09:30:05+05:30",
                category="Board Meeting",
                company="Bharat Dynamics Ltd",
            ),
            bse_row(
                "eve",
                isin["ZENTEC.NS"],
                "2026-10-07T21:30:00",
                "2026-10-08T09:30:05+05:30",
                category="Insider Trading / SAST",
                company="Zen Technologies Ltd",
            ),
            bse_row(
                "fresh",
                isin["ASTRAMICRO.NS"],
                "2026-10-08T09:35:00",
                "2026-10-08T09:36:00+05:30",
                category="Award of Order",
                company="Astra Microwave Products Ltd",
            ),
        ],
    )
    watcher.run_job("feeds", ist(8, 9, 40))  # an ordinary pass, not a catch-up

    with db() as session:
        new = {
            f.symbol: f
            for f in session.scalars(
                select(WatchFiling).where(WatchFiling.first_seen_at == ist(8, 9, 40))
            )
        }
        # Every row was read, the ones filed hours before the catch-up included.
        assert set(new) == {"BDL.NS", "ZENTEC.NS", "ASTRAMICRO.NS"}
        assert new["ZENTEC.NS"].filed_at == ist(7, 21, 30)  # before the gap began at 22:00
        assert session.get(WatchCursor, BSE_CURSOR).position == 3
        late_runs = session.scalars(select(WatchRun).where(WatchRun.job == "late")).all()
        assert len(late_runs) == 1 and late_runs[0].new_filings == 2
        # Each story is rated high, so an instant alert is what it would get if it were fresh.
        for filing in new.values():
            session.add(
                WatchCall(
                    story_id=filing.story_id,
                    symbol=filing.symbol,
                    relevance="primary",
                    sentiment="positive",
                    materiality="high",
                    event_type="order_win",
                    reason=f"{filing.subject} at {filing.symbol}.",
                    summary="A filing. It matters.",
                    article_count=0,
                    filing_count=1,
                    read_reply=False,
                    trigger="new",
                    model="m",
                    prompt_version="watch-v1",
                    created_at=ist(8, 9, 41),
                )
            )
        session.commit()

    watcher.run_job("alerts", ist(8, 9, 52))
    alerts = [m for m in sent if m.startswith("<b>BASIS · watchlist alert</b>")]
    summaries = [m for m in sent if m.startswith("<b>BASIS · while you were away</b>")]
    # Only the fresh filing gets an alert of its own.
    assert len(alerts) == 1 and "Astra Microwave" in alerts[0]
    assert len(summaries) == 1
    summary = summaries[0]
    assert "BSE filings the PEAD tool caught up on, first seen at" in summary
    assert "Bharat Dynamics" in summary and "Zen Technologies" in summary
    assert "Astra Microwave" not in summary
    # Newest first, each at its original time.
    assert summary.index("<b>02:00</b>") < summary.index("<b>Wed 07 Oct 21:30</b>")
    with db() as session:
        kinds = sorted(a.kind for a in session.scalars(select(WatchAlert)))
        # One summary for the 09:00 catch-up, one for the late filings: one news alert.
        assert kinds == ["away", "away", "news"]
        late_run = session.scalars(select(WatchRun).where(WatchRun.job == "late")).one()
        assert (
            session.scalars(select(WatchAlert).where(WatchAlert.key == f"away:{late_run.id}"))
            .one()
            .sent_at
            is not None
        )


def test_a_late_filing_on_an_alerted_story_is_no_follow_up(
    settings: Settings, db: sessionmaker[Session]
) -> None:
    now = NOW
    with db() as session:
        story = WatchStory(first_seen_at=now - timedelta(hours=2), headline="Prime Focus raided")
        session.add(story)
        session.flush()
        session.add(
            WatchAlert(
                kind="news",
                key=f"news:{story.id}",
                text="x",
                story_id=story.id,
                symbols=["PFOCUS.NS"],
                created_at=now - timedelta(hours=2),
                sent_at=now - timedelta(hours=2),
                attempts=1,
            )
        )
        session.add(
            WatchFiling(
                key="late",
                exchange="BSE",
                symbol="PFOCUS.NS",
                company="Prime Focus Ltd",
                subject="Company Update",
                description="Backfilled by the PEAD tool.",
                filed_at=now - timedelta(hours=3),
                first_seen_at=now - timedelta(minutes=30),
                kind="filing",
                story_id=story.id,
            )
        )
        session.commit()
        assert followups(session, settings, {"PFOCUS.NS": "Prime Focus"}, now) == []


def regulation_30_reply(headline: str) -> str:
    return (
        "Prime Focus Ltd - 532748 - Disclosure under Regulation 30 of SEBI (Listing "
        "Obligations and Disclosure Requirements) Regulations, 2015. With reference to the "
        f"email received from the Exchange today regarding the article {headline}, we wish "
        "to inform that proceedings were initiated by the authorities at certain premises "
        "and the management has been extending full cooperation to the officials. The "
        "management believes the matter will not have any material bearing on operations, "
        "financial position, liquidity or continuity of business, and will keep "
        "stakeholders informed of further developments as required under applicable law."
    )


def high_call(story_id: int, reason: str, at: datetime, filings: int = 0) -> WatchCall:
    return WatchCall(
        story_id=story_id,
        symbol="PFOCUS.NS",
        relevance="primary",
        sentiment="negative",
        materiality="high",
        event_type="regulatory",
        reason=reason,
        summary="Tax searches at Prime Focus. It matters.",
        article_count=1,
        filing_count=filings,
        read_reply=False,
        trigger="new",
        model="m",
        prompt_version="watch-v1",
        created_at=at,
    )


def test_a_late_reply_to_an_alerted_story_is_an_update_to_it_in_the_summary(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, tmp_path: Path
) -> None:
    """The raid story was alerted at 10:40. The PEAD tool, started later, backfills a BSE
    reply filed at 08:00 that quotes the headline. The reply joins that story, and the
    summary shows it as an update to it - "Update: <the story> (alerted 10:40)" with the
    filing under it - not as a filing of its own, and no follow-up is sent for it."""

    def ist(hour: int, minute: int) -> datetime:
        return datetime(2026, 10, 8, hour, minute, tzinfo=IST).astimezone(UTC)

    path = pead_write(tmp_path / "announcements.db", [])
    watcher = make_watcher(with_bse(settings, path), db, watchlist, Web(), Clock(ist(10, 31)))
    sent: list[str] = []
    watcher.sender = sent.extend
    watcher.run_job("feeds", ist(10, 31))
    with db() as session:
        raid = session.scalars(
            select(WatchArticle).where(WatchArticle.url.endswith("pf-raid"))
        ).one()
        story_id = raid.story_id
        assert story_id is not None
        session.add(high_call(story_id, "Tax searches at its Mumbai offices.", ist(10, 32)))
        session.commit()
    news = Outgoing("news", f"news:{story_id}", ("x",), story_id, ("PFOCUS.NS",))
    deliver(db, [news], lambda messages: None, ist(10, 40))  # the instant alert
    watcher.run_job("feeds", ist(10, 41))
    watcher.run_job("feeds", ist(10, 51))

    reply = regulation_30_reply(PF_RAID[0])
    pead_write(
        path,
        [
            bse_row(
                "reply",
                PFOCUS_ISIN,
                "2026-10-08T08:00:00",
                "2026-10-08T10:55:00+05:30",
                headline=reply,
            )
        ],
    )
    watcher.run_job("feeds", ist(11, 1))
    with db() as session:
        filing = session.scalars(select(WatchFiling).where(WatchFiling.exchange == "BSE")).one()
        assert filing.story_id == story_id  # it quotes the headline: the story it answers
        filings = len(session.get(WatchStory, story_id).filings)
        session.add(
            high_call(
                story_id, "The company confirmed the search; work goes on.", ist(11, 2), filings
            )
        )
        session.commit()

    watcher.run_job("alerts", ist(11, 12))
    summaries = [m for m in sent if m.startswith("<b>BASIS · while you were away</b>")]
    assert len(summaries) == 1
    body = summaries[0]
    assert f"• <b>08:00</b> · Update: {PF_RAID[0]} (alerted 10:40)" in body
    assert "  08:00 · BSE · Company Update: Prime Focus Ltd - 532748 - Disclosure" in body
    assert "the company confirmed the search; work goes on." in body.lower()
    assert body.count("• ") == 1  # one item: the update, not the filing again
    assert not [m for m in sent if m.startswith("<b>BASIS · watchlist update</b>")]


def test_a_filing_quoting_a_headline_word_for_word_joins_its_story(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, tmp_path: Path
) -> None:
    path = pead_write(tmp_path / "announcements.db", [])
    watcher = make_watcher(with_bse(settings, path), db, watchlist, Web(), Clock(NOW))
    watcher.run_job("feeds", NOW)
    pead_write(
        path,
        [
            # Names the raid headline in full: joins the raid story.
            bse_row(
                "q",
                PFOCUS_ISIN,
                "2026-10-08T11:31:00",
                "2026-10-08T11:32:00+05:30",
                headline=regulation_30_reply(PF_RAID[0]),
            ),
            # Only part of it: left to the grouper.
            bse_row(
                "p",
                PFOCUS_ISIN,
                "2026-10-08T11:33:00",
                "2026-10-08T11:34:00+05:30",
                category="Board Meeting",
                headline="Prime Focus Ltd - Board meeting on Mumbai offices",
            ),
        ],
    )
    watcher.run_job("feeds", NOW + EVERY)
    with db() as session:
        raid = session.scalars(
            select(WatchArticle).where(WatchArticle.url.endswith("pf-raid"))
        ).one()
        by_subject = {
            f.subject: f.story_id
            for f in session.scalars(select(WatchFiling).where(WatchFiling.exchange == "BSE"))
        }
    assert by_subject["Company Update"] == raid.story_id
    assert by_subject["Board Meeting"] != raid.story_id
