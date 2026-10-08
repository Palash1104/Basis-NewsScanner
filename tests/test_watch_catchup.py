"""Catching up after the laptop was off: the gap, the sources, and what none could reach."""

import json
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
from app.watch.sources import google_news_window, parse_nse_api
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
