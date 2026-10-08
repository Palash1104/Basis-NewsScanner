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
from app.models import WatchFiling, WatchPrice, WatchRun
from app.watch.catchup import Gap, SourceCoverage, find_gap, possible_gaps
from app.watch.prices import backfill_rows
from app.watch.sources import (
    Announcement,
    google_news_window,
    parse_nse_api,
    read_bse_announcements,
)
from tests.test_watch_scan import NSE_URL, RSS, Clock, Web, make_watcher

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

PEAD_SCHEMA = (
    "CREATE TABLE announcements (id TEXT PRIMARY KEY, exchange TEXT, company TEXT, code TEXT, "
    "isin TEXT, category TEXT, headline TEXT, attachment_url TEXT, exchange_time TEXT, "
    "fetched_at TEXT)"
)
PFOCUS_ISIN = "INE367G01020"


def pead_file(path: Path, rows: list[tuple[str, ...]]) -> Path:
    """The PEAD tool's shared file as it writes it: IST times without a zone."""
    conn = sqlite3.connect(path)
    conn.execute(PEAD_SCHEMA)
    conn.executemany("INSERT INTO announcements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def bse_row(
    id_: str, isin: str | None, exchange_time: str, fetched_at: str, exchange: str = "BSE"
) -> tuple[str, ...]:
    return (
        id_,
        exchange,
        "Prime Focus Ltd",
        "532748",
        isin,
        "Company Update",
        "Prime Focus Ltd has informed the Exchange about an investor meet",
        f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{id_}.pdf",
        exchange_time,
        fetched_at,
    )


def with_bse(settings: Settings, path: Path) -> Settings:
    watch = settings.watch.model_copy(update={"bse_announcements_db": str(path)})
    return settings.model_copy(update={"watch": watch})


def test_bse_rows_are_read_by_isin_from_the_shared_file(tmp_path: Path) -> None:
    path = pead_file(
        tmp_path / "announcements.db",
        [
            bse_row("a", PFOCUS_ISIN, "2026-10-08T10:05:00", "2026-10-08T10:06:10"),
            bse_row("b", "INE000000000", "2026-10-08T10:07:00", "2026-10-08T10:08:00"),
            bse_row("c", PFOCUS_ISIN, "2026-10-08T10:09:00", "2026-10-08T10:10:00", "NSE"),
            bse_row("d", PFOCUS_ISIN, "2026-10-07T15:00:00", "2026-10-07T15:01:00"),
            bse_row("e", None, "2026-10-08T10:11:00", "2026-10-08T10:12:00"),
        ],
    )
    since = datetime(2026, 10, 8, 4, 0, tzinfo=UTC)  # 09:30 IST
    shared = read_bse_announcements(path, {PFOCUS_ISIN: "PFOCUS.NS"}, since)
    assert shared.error is None
    # Only the watchlist company's BSE row written since then: not another company, not
    # the NSE copy (BASIS reads NSE itself), not an older one, not one without an ISIN.
    assert [(symbol, a.exchange) for symbol, a in shared.found] == [("PFOCUS.NS", "BSE")]
    found = shared.found[0][1]
    assert found.filed_at == datetime(2026, 10, 8, 4, 35, tzinfo=UTC)  # 10:05 IST
    assert found.subject == "Company Update" and found.kind == "filing"
    assert found.link.endswith("/a.pdf")
    assert shared.newest_fetch == datetime(2026, 10, 8, 4, 42, tzinfo=UTC)
    # NSE and BSE copies hash apart.
    assert found.key != Announcement(**{**found.__dict__, "exchange": "NSE"}).key


def test_a_missing_or_broken_shared_file_is_an_error_not_a_crash(tmp_path: Path) -> None:
    since = datetime(2026, 10, 8, 4, 0, tzinfo=UTC)
    missing = read_bse_announcements(tmp_path / "nope.db", {PFOCUS_ISIN: "PFOCUS.NS"}, since)
    assert missing.found == [] and missing.error and "hasn't run" in missing.error
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all, just bytes" * 10)
    bad = read_bse_announcements(broken, {PFOCUS_ISIN: "PFOCUS.NS"}, since)
    assert bad.found == [] and bad.error


def test_the_shared_file_is_opened_read_only(tmp_path: Path) -> None:
    path = pead_file(tmp_path / "announcements.db", [])
    before = path.stat().st_mtime_ns
    read_bse_announcements(path, {PFOCUS_ISIN: "PFOCUS.NS"}, NOW - timedelta(days=1))
    assert path.stat().st_mtime_ns == before
    assert not (tmp_path / "announcements.db-journal").exists()


def test_a_feed_pass_stores_bse_filings_once(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, tmp_path: Path
) -> None:
    path = pead_file(
        tmp_path / "announcements.db",
        [bse_row("a", PFOCUS_ISIN, "2026-10-08T10:05:00", "2026-10-08T10:06:10")],
    )
    clock = Clock(NOW)
    watcher = make_watcher(with_bse(settings, path), db, watchlist, CatchUpWeb(), clock)
    watcher.run_job("feeds", NOW)
    watcher.run_job("feeds", NOW + EVERY)  # the same row again: the minute's overlap
    with db() as session:
        filings = session.scalars(select(WatchFiling).where(WatchFiling.exchange == "BSE")).all()
        assert [(f.symbol, f.subject) for f in filings] == [("PFOCUS.NS", "Company Update")]
        assert filings[0].story_id is not None


def test_a_catch_up_flags_bse_when_the_pead_tool_was_not_running(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, tmp_path: Path
) -> None:
    # The PEAD tool last wrote at 09:00 IST on the 7th, well before the gap began.
    path = pead_file(
        tmp_path / "announcements.db",
        [bse_row("a", PFOCUS_ISIN, "2026-10-07T08:59:00", "2026-10-07T09:00:00")],
    )
    watcher = make_watcher(with_bse(settings, path), db, watchlist, CatchUpWeb(), Clock(NOW))
    watcher.history = FakeHistory()
    with db() as session:
        session.add(
            WatchRun(job="feeds", started_at=NOW - timedelta(hours=10), entries=900, errors=[])
        )
        session.commit()
    watcher.run_job("feeds", NOW)
    with db() as session:
        run = session.scalars(select(WatchRun).where(WatchRun.job == "catchup")).one()
        details = run.details or {}
    gaps = details["possible_gaps"]
    assert [g["what"] for g in gaps] == ["BSE filings"]
    assert "hasn't written since Wed 07 Oct 09:00" in gaps[0]["why"]
