"""The watchlist scanner: `newsdesk watch`, a resident process beside the 3-hourly pipeline.

Three jobs, each run when due:

  feeds         every 10 min   the watch feeds and NSE's announcements, conditional GETs
  google_news   20 min in market hours, hourly otherwise   batched searches for every alias
  prices        every 5 min in NSE market hours            one intraday poll per symbol

Every headline goes through the matcher (app/watch/match.py). One that names a watchlist stock
- keep, mention or drop - is stored with every verdict, every feed that carried it and when
BASIS first saw it; one that names none is not kept. Filings by watchlist companies are
stored. Articles and filings are grouped into stories across stocks (app/watch/group.py).

Step 2 of the watchlist: no LLM call is made here. It writes only the watch_* tables and
feed_checks, in one short transaction per job: everything slow - the network, the embedding
model - happens before the write begins, so the pipeline is never kept waiting.

It also scans one stock when its card is opened on /watchlist (app/watch/ondemand.py): the
page leaves a request in an inbox folder, and `serve_scan_requests` runs it between jobs.
"""

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from rapidfuzz import fuzz
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import FeedConfig, Settings, WatchItem, WatchlistFile
from app.llm.client import LLMClient
from app.models import (
    FeedCheck,
    WatchArticle,
    WatchCursor,
    WatchFiling,
    WatchMatch,
    WatchPrice,
    WatchRun,
    WatchScan,
    WatchSighting,
    WatchStory,
    utcnow,
)
from app.net import make_client
from app.pipeline.dedupe import normalize_source, normalize_title
from app.pipeline.embed import Embedder, article_text
from app.pipeline.fetch import FetchedArticle, SourceResolver
from app.watch.alerts import (
    Sender,
    arrived_late,
    away_summaries,
    deliver,
    detect_now,
    followups,
    group_layout,
    mark_summarised,
    move_alerts,
    news_alerts,
    retry_pending,
)
from app.watch.analyse import analyse, kept_symbols, read_replies, stale_after
from app.watch.catchup import Gap, SourceCoverage, find_gap, possible_gaps
from app.watch.companies import LIST_FILE as COMPANY_LIST_FILE
from app.watch.companies import due as company_lists_due
from app.watch.companies import filing_keys, load_companies, pead_master_path
from app.watch.companies import refresh as refresh_company_list
from app.watch.events import (
    Amount,
    amounts,
    amounts_conflict,
    is_commentary,
    is_event,
    is_order_news,
)
from app.watch.group import StoryGrouper
from app.watch.match import Match, Matcher
from app.watch.moves import Typical, typical_moves
from app.watch.ondemand import (
    HEARTBEAT_EVERY,
    ScanRequest,
    clear_heartbeat,
    new_steps,
    pending_requests,
    profile_to_json,
    remove_request,
    with_step,
    write_heartbeat,
)
from app.watch.power import keep_awake, on_ac_power
from app.watch.preview import Profile
from app.watch.prices import (
    MINUTE_HISTORY,
    HistoryProvider,
    Snapshot,
    SnapshotProvider,
    YahooHistory,
    backfill_rows,
    in_market_hours,
    price_symbols,
)
from app.watch.sources import (
    GOOGLE_NEWS_CAP,
    Announcement,
    FeedRead,
    Fetched,
    SharedRead,
    Validators,
    conditional_get,
    fetch_nse_history,
    google_news_queries,
    google_news_window,
    parse_nse_announcements,
    read_bse_announcements,
    read_feed,
    watched_announcements,
)

log = logging.getLogger(__name__)
# watch_cursors row for the PEAD tool's shared file: the last seq processed.
BSE_CURSOR = "pead_announcements"
# A filing that quotes a headline at least this long, word for word, joins its story.
QUOTE_MIN_WORDS = 6

# analyse comes after the news jobs, so what they just stored is called in the same tick;
# alerts come last, so a call or a price made this tick is alerted this tick.
JOBS = ("feeds", "google_news", "analyse", "prices", "alerts")
TYPICAL_RETRY = timedelta(hours=1)  # after failing to fetch the bars thresholds come from
RETRY_AFTER_FAILURE = timedelta(minutes=1)  # after a pass in which every source failed
NSE_FEED_NAME = "NSE announcements"
# Two sightings are one article when the outlet is the same and the titles this close: a
# CNBC-TV18 story reached through its own RSS and through Google News.
SAME_ARTICLE_WINDOW = timedelta(hours=48)


@dataclass(frozen=True)
class _Member:
    """An article or filing already in a story, still inside the story window."""

    seen: datetime  # when BASIS first saw it
    story_id: int
    symbols: frozenset[str]
    text: str  # what is embedded: headline and snippet
    kind: str  # article | filing
    headline: str  # what amounts and order words are read from
    published: datetime  # the publisher's time, or the exchange's for a filing


@dataclass
class _OrderStories:
    """Stories about an order or contract, to place order news that similarity can't."""

    within: timedelta
    _stories: dict[int, tuple[set[str], list[datetime], set[Amount]]] = field(default_factory=dict)

    def add(
        self, story_id: int, symbols: Iterable[str], when: datetime, stated: Iterable[Amount]
    ) -> None:
        held = self._stories.setdefault(story_id, (set(), [], set()))
        held[0].update(symbols)
        held[1].append(when)
        held[2].update(stated)

    def find(self, symbols: Iterable[str], when: datetime, stated: Iterable[Amount]) -> int | None:
        """The order story for one of these stocks reported closest to `when`, within the
        window, whose amounts don't disagree; on a tie, the one with more items."""
        wanted, stated = set(symbols), set(stated)
        best: tuple[float, int, int] | None = None
        for story_id, (held_symbols, times, held_amounts) in self._stories.items():
            if not held_symbols & wanted or amounts_conflict(held_amounts, stated):
                continue
            distance = min(abs((when - at).total_seconds()) for at in times)
            if distance > self.within.total_seconds():
                continue
            rank = (distance, -len(times), story_id)
            if best is None or rank < best:
                best = rank
        return best[2] if best is not None else None


@dataclass
class _Candidate:
    """A fetched headline that names a watchlist stock, before it is stored."""

    entry: FetchedArticle
    feed: FeedConfig
    matches: list[Match]

    @property
    def text(self) -> str:
        return article_text(self.entry.title, self.entry.snippet)


@dataclass
class JobResult:
    job: str
    entries: int = 0
    new_articles: int = 0
    new_filings: int = 0
    stories_created: int = 0
    prices: int = 0
    calls: int = 0  # watch_calls rows written (analyse)
    errors: list[dict[str, Any]] = field(default_factory=list)
    # Articles not stored because they were old news when this pass first saw them, by
    # source ("Google News" for every search) - see `_store_articles`.
    stale: dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        if self.job == "prices":
            text = f"prices: {self.prices} symbols polled"
        elif self.job == "analyse":
            text = f"analyse: {self.entries} stories called, {self.calls} calls written"
        else:
            text = (
                f"{self.job}: {self.entries} entries, {self.new_articles} new watchlist articles, "
                f"{self.new_filings} new filings, {self.stories_created} new stories"
            )
        if self.stale:
            sources = ", ".join(f"{n} from {source}" for source, n in sorted(self.stale.items()))
            text += f", {sum(self.stale.values())} older than a week skipped ({sources})"
        return text + (f", {len(self.errors)} errors" if self.errors else "")


class Watcher:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        watchlist: WatchlistFile,
        feeds: Sequence[FeedConfig],
        resolver: SourceResolver,
        embedder: Embedder | None,
        snapshots: SnapshotProvider | None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utcnow,
        history: HistoryProvider | None = None,
        llm: LLMClient | None = None,
        sender: Sender | None = None,
        watchlist_path: Path | None = None,
        companies_path: Path | None = None,
        scan_inbox: Path | None = None,
        profile_fetch: Callable[[str], Profile] | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.feeds = list(feeds)
        self._use_watchlist(watchlist)
        # config/watchlist.yaml, re-read when it changes (an edit on the BASIS page, or by
        # hand), so adding a stock needs no restart. None in tests.
        self.watchlist_path = watchlist_path
        self._watchlist_mtime = _mtime(watchlist_path) if watchlist_path else None
        # The NSE and BSE company lists behind the page's "Add a stock"
        # (app/watch/companies.py), kept a day fresh here and by the web server. None in tests.
        self.companies_path = companies_path
        self.resolver = resolver
        self.embedder = embedder
        self.snapshots = snapshots
        self.transport = transport
        self.clock = clock
        self.validators: dict[str, Validators] = {}
        # Entry URLs each feed carried at its last check, to count what is new in it.
        self.carried: dict[str, set[str]] = {}
        self._vectors: dict[str, np.ndarray] = {}
        self.last_run: dict[str, datetime] = {}
        self.history = history
        self.llm = llm
        # (story, articles, filings) whose call came back invalid: not asked again as is.
        self._bad_inputs: set[tuple[int, int, int]] = set()
        self._calls_paused_until: datetime | None = None  # the watch lane's budget is spent
        self.sender = sender
        self._typical: tuple[date, dict[str, Typical]] | None = None
        self._typical_failed_at: datetime | None = None
        self._bse_error: str | None = None
        # Set by `newsdesk watch --since`: catch up from here on the next feed scan.
        self.force_since: datetime | None = None
        # The page's requests to scan one stock (app/watch/ondemand.py), and Yahoo's profile
        # for the dialog it opens. None in tests that don't scan on demand.
        self.scan_inbox = scan_inbox
        self.profile_fetch = profile_fetch
        # What the scanner is doing, for its heartbeat: a job, a scan, or idle - and since when.
        self.activity: tuple[str, datetime] = ("idle", clock())

    def _use_watchlist(self, watchlist: WatchlistFile) -> None:
        """Everything that depends on the list of stocks."""
        self.watchlist = watchlist
        self.stocks = watchlist.stocks
        self.matcher = Matcher(self.stocks)
        self.queries = google_news_queries(self.stocks, self.settings)
        # The feeds whose health is tracked: not the one-off catch-up searches.
        self._checked = {feed.url for feed in [*self.feeds, *self.queries]}
        self._typical = None  # typical moves cover the old list's stocks

    def refresh_watchlist(self) -> bool:
        """Re-read config/watchlist.yaml if it changed since it was last read. A file that
        doesn't load keeps the list in use, and says so once per change. True when the
        list was replaced."""
        if self.watchlist_path is None:
            return False
        mtime = _mtime(self.watchlist_path)
        if mtime is None or mtime == self._watchlist_mtime:
            return False
        self._watchlist_mtime = mtime
        from app.config import load_watchlist_file

        try:
            fresh = load_watchlist_file(self.watchlist_path)
        except Exception as exc:
            log.warning(
                "watchlist.yaml changed but doesn't load; still using the old list: %s", exc
            )
            return False
        before = [s.symbol for s in self.stocks]
        self._use_watchlist(fresh)
        after = [s.symbol for s in self.stocks]
        log.info(
            "watchlist reloaded: %d stocks (added %s, removed %s)",
            len(after),
            sorted(set(after) - set(before)) or "none",
            sorted(set(before) - set(after)) or "none",
        )
        return True

    def refresh_companies(self, now: datetime) -> str | None:
        """Download the NSE, NSE SME and BSE company lists that are a day old (or missing), so
        the page's "Add a stock" offers yesterday's listings; a list that failed is tried
        again after a couple of hours (app/watch/companies.py). Returns what happened, or None
        when nothing was due."""
        if self.companies_path is None:
            return None
        sources = company_lists_due(self.companies_path, now)
        if not sources:
            return None
        line = refresh_company_list(
            self.companies_path,
            self.settings.http.user_agent,
            now=now,
            lock_dir=self.settings.resolve_path(self.settings.paths.lock_dir),
            sources=sources,
            pead_master=pead_master_path(self.settings.watch.bse_announcements_db),
        )
        log.info(line)
        return line

    # ------------------------------------------------------------ scheduling

    def interval(self, job: str, now: datetime) -> timedelta | None:
        """How often `job` runs at `now`, or None when it doesn't run at all then."""
        watch = self.settings.watch
        market = in_market_hours(now, self.settings)
        if job == "feeds":
            return timedelta(minutes=watch.feeds_every_minutes)
        if job == "google_news":
            minutes = (
                watch.google_news_every_minutes if market else watch.google_news_quiet_every_minutes
            )
            return timedelta(minutes=minutes)
        if job == "prices":
            return timedelta(minutes=watch.prices_every_minutes) if market else None
        if job in ("analyse", "alerts"):
            return timedelta(minutes=1)
        raise ValueError(job)

    def due(self, job: str, now: datetime) -> bool:
        every = self.interval(job, now)
        if every is None:
            return False
        last = self.last_run.get(job)
        return last is None or now - last >= every

    def tick(self, force: bool = False) -> list[JobResult]:
        """Run every job that is due (all of them with `force`, prices still only in market
        hours). One job failing never stops the others."""
        results = []
        self.refresh_watchlist()
        self.refresh_companies(self.clock())
        self.serve_scan_requests()
        for job in JOBS:
            now = self.clock()
            if job == "prices" and not in_market_hours(now, self.settings):
                continue
            if not force and not self.due(job, now):
                continue
            self.last_run[job] = now
            self.activity = (job, now)
            result = self.run_job(job, now)
            self.activity = ("idle", self.clock())
            # A scan the page asked for waits for no more than the job in hand.
            self.serve_scan_requests()
            if job in ("analyse", "alerts") and not result.entries and not result.errors:
                continue  # checked every minute; a pass that found nothing to do is quiet
            results.append(result)
            every = self.interval(job, now)
            news = job in ("feeds", "google_news")
            if news and result.entries == 0 and result.errors and every:
                # Nothing answered - no network yet, typically right after a wake. Try again
                # in a minute rather than a whole interval later.
                self.last_run[job] = now - every + RETRY_AFTER_FAILURE
        return results

    def run_job(self, job: str, now: datetime) -> JobResult:
        run = WatchRun(job=job, started_at=now, on_ac=on_ac_power(), errors=[])
        try:
            if job == "feeds":
                result = self.scan_feeds(now)
            elif job == "google_news":
                result = self.scan_google_news(now)
            elif job == "analyse":
                result = self.analyse_stories(now)
            elif job == "alerts":
                result = self.send_alerts(now)
            else:
                result = self.poll_prices(now)
        except Exception as exc:  # the loop must outlive any one bad pass
            log.exception("watch job %s failed", job)
            result = JobResult(
                job, errors=[{"stage": job, "error": f"{type(exc).__name__}: {exc}"}]
            )
        if job in ("analyse", "alerts") and not result.entries and not result.errors:
            return result  # checked every minute; only a pass that did something is recorded
        run.entries = result.entries
        run.new_articles = result.new_articles
        run.new_filings = result.new_filings
        run.errors = result.errors
        if result.stale:
            run.details = {"stale_skipped": result.stale}
        run.finished_at = self.clock()
        with self.session_factory() as session:
            session.add(run)
            session.commit()
        return result

    # ------------------------------------------------------------ jobs

    def scan_feeds(self, now: datetime) -> JobResult:
        """The watch feeds and NSE's announcements. After the laptop was off or asleep, also
        the catch-up: Google News over the missed period, NSE's API for the missed days and
        1-minute prices (app/watch/catchup.py)."""
        gap = self.find_gap(now)
        searches = (
            google_news_queries(self.stocks, self.settings, google_news_window(gap.start, now))
            if gap is not None
            else []
        )
        reads, nse = asyncio.run(self._fetch([*self.feeds, *searches], include_nse=True, now=now))
        history: dict[str, tuple[list[Announcement], str | None]] = {}
        if gap is not None:
            history = asyncio.run(self._nse_history(gap, now))
        shared = self._read_bse(now)
        result = self._process("feeds", reads, nse, now, history, shared, record_late=gap is None)
        if gap is not None:
            self._record_catch_up(gap, reads, searches, nse, history, result, now)
        return result

    def _read_bse(self, now: datetime) -> SharedRead | None:
        """The rows the PEAD tool wrote since the last one processed (`watch_cursors`). It
        catches up on its own after a restart, so BASIS needs no BSE catch-up of its own: what
        it backfills is simply read on the next pass. Matched on the watchlist's ISINs and BSE
        codes and on what the company lists give each ticker (a split changes the ISIN)."""
        path = self.settings.watch.bse_announcements_db
        listed = load_companies(self.companies_path) if self.companies_path else []
        isins, codes = filing_keys(self.stocks, listed)
        if not path or not (isins or codes):
            return None
        with self.session_factory() as session:
            cursor = session.get(WatchCursor, BSE_CURSOR)
            after = cursor.position if cursor else None
        floor = now - timedelta(days=self.settings.watch.catch_up_max_days)
        shared = read_bse_announcements(Path(path), isins, after, floor, codes)
        if shared.error != self._bse_error:  # said once, not every pass
            if shared.error:
                log.warning("BSE filings unavailable: %s", shared.error)
            else:
                log.info("BSE filings: reading the PEAD tool's shared file")
            self._bse_error = shared.error
        return shared

    def find_gap(self, now: datetime) -> Gap | None:
        """What the scanner missed: everything since its last completed feed scan started."""
        every = timedelta(minutes=self.settings.watch.feeds_every_minutes)
        if self.force_since is not None:
            since, self.force_since = self.force_since, None
            return Gap(since, now, since)
        with self.session_factory() as session:
            last = session.scalar(
                select(WatchRun.started_at)
                .where(WatchRun.job == "feeds", WatchRun.entries > 0, WatchRun.started_at < now)
                .order_by(WatchRun.started_at.desc())
                .limit(1)
            )
        return find_gap(last, now, every, self.settings.watch.catch_up_max_days)

    async def _nse_history(
        self, gap: Gap, now: datetime
    ) -> dict[str, tuple[list[Announcement], str | None]]:
        """Each company's NSE announcements over the gap's days, one request per company."""
        stocks = [stock for stock in self.stocks if stock.nse_symbol]
        async with make_client(self.settings.http, transport=self.transport) as client:
            found = await asyncio.gather(
                *(
                    fetch_nse_history(client, stock.nse_symbol or "", gap.start, now, self.settings)
                    for stock in stocks
                )
            )
        return {stock.symbol: pair for stock, pair in zip(stocks, found, strict=True)}

    def scan_google_news(self, now: datetime) -> JobResult:
        reads, _ = asyncio.run(self._fetch(self.queries, include_nse=False, now=now))
        return self._process("google_news", reads, None, now)

    def analyse_stories(self, now: datetime) -> JobResult:
        """The watchlist call on every story that needs one (app/watch/analyse.py), after
        reading any company reply that has arrived. Paused once the watch lane's daily
        budget is spent, until the quota day turns."""
        result = JobResult("analyse")
        if self.llm is None or (self._calls_paused_until and now < self._calls_paused_until):
            return result
        for note in read_replies(self.session_factory, self.settings):
            log.info("watch reply: %s", note)
        outcome = analyse(
            self.session_factory,
            self.llm,
            self.settings,
            {stock.symbol: stock for stock in self.stocks},
            now,
            self._bad_inputs,
        )
        result.entries = len(outcome.called)
        result.calls = outcome.calls_written
        result.errors = [
            {"stage": "analyse", "story_id": story_id, "error": error}
            for story_id, error in outcome.failed
        ]
        if outcome.stopped:
            result.errors.append({"stage": "analyse", "error": outcome.stopped})
            if self.llm.limiter is not None:
                self._calls_paused_until = self.llm.limiter.next_reset()
        for note in outcome.notes:
            log.info("watch call: %s", note)
        return result

    def typical_moves(self, now: datetime) -> dict[str, Typical] | None:
        """Each stock's and index's typical day (app/watch/moves.py), worked out once a day
        from hourly bars. None when they couldn't be fetched (tried again in an hour)."""
        today = now.astimezone(self.settings.tz).date()
        if self._typical and self._typical[0] == today:
            return self._typical[1]
        if self.history is None or (
            self._typical_failed_at and now - self._typical_failed_at < TYPICAL_RETRY
        ):
            return None
        groups, ungrouped = group_layout(self.watchlist)
        index_of = {s: index for index, members in groups.values() for s in members}
        symbols = [*ungrouped, *index_of, *(i for i in index_of.values() if i)]
        try:
            bars = {s: self.history.hourly_bars(s, 120) for s in dict.fromkeys(symbols)}
        except Exception as exc:
            log.warning("typical moves unavailable: %s", exc)
            self._typical_failed_at = now
            return None
        tz = self.settings.tz
        found = typical_moves(bars, index_of, today, lambda moment: moment.astimezone(tz).date())
        self._typical = (today, found)
        return found

    def send_alerts(self, now: datetime) -> JobResult:
        """Everything Telegram should hear about now (app/watch/alerts.py)."""
        result = JobResult("alerts")
        names = {stock.symbol: stock.name or stock.symbol for stock in self.stocks}
        typical = self.typical_moves(now)
        with self.session_factory() as session:
            outgoing = retry_pending(session, now)
            outgoing += news_alerts(session, self.settings, names, now)
            outgoing += followups(session, self.settings, names, now)
            if typical and in_market_hours(now, self.settings):
                events = detect_now(session, self.watchlist, typical, self.settings, now)
                outgoing += move_alerts(events, session, names, self.settings)
            summaries, empty = away_summaries(session, self.watchlist, typical, self.settings, now)
            outgoing += summaries
            # Feed health is not sent to Telegram (user, 2026-10-16): it is on /runs.
        if empty:
            mark_summarised(self.session_factory, empty, "nothing to report")
        unique = list({item.key: item for item in outgoing}.values())
        delivered = deliver(self.session_factory, unique, self.sender, now)
        runs = [int(item.key.split(":")[1]) for item in summaries]
        if runs:
            with self.session_factory() as session:
                done = session.scalars(select(WatchRun).where(WatchRun.id.in_(runs))).all()
            mark_summarised(self.session_factory, done, "summary sent or queued")
        result.entries = len(delivered.sent)
        result.errors = [{"stage": "alerts", "key": key, "error": e} for key, e in delivered.failed]
        return result

    def poll_prices(self, now: datetime) -> JobResult:
        result = JobResult("prices")
        if self.snapshots is None:
            return result
        indices = [group.index for group in self.watchlist.groups.values() if group.index]
        rows = []
        for symbol in price_symbols(
            [stock.symbol for stock in self.stocks], indices, self.settings.watch.benchmark
        ):
            polled = self.clock()
            try:
                snap = self.snapshots.snapshot(symbol)
            except Exception as exc:  # one symbol failing is recorded, the rest still polled
                error = f"{type(exc).__name__}: {exc}"
                rows.append(WatchPrice(symbol=symbol, polled_at=polled, error=error))
                result.errors.append({"stage": "prices", "symbol": symbol, "error": error})
                continue
            rows.append(_price_row(symbol, polled, snap))
            result.prices += 1
        with self.session_factory() as session:
            session.add_all(rows)
            session.commit()
        return result

    # ------------------------------------------------------------ one stock, on demand

    def serve_scan_requests(self) -> int:
        """Run every scan the /watchlist page has asked for (app/watch/ondemand.py), oldest
        first; returns how many ran.

        Called only from the scanner's own loop - between its jobs, and every couple of
        seconds while it waits for the next tick - and never from a thread of its own. That is
        deliberate: a scan stores, groups and moves the BSE cursor exactly as a regular pass
        does, and two passes writing at once would race on both (each reads a story's members
        and the cursor, then writes on top of what it read)."""
        if self.scan_inbox is None:
            return 0
        return sum(self.scan_stock(request) for request in pending_requests(self.scan_inbox))

    def scan_stock(self, request: ScanRequest) -> bool:
        """One requested scan: its row opened `running` before the request file goes (so a
        second click finds one or the other), each step written as it ends, then done or
        failed. False when the request was dropped instead."""
        inbox = self.scan_inbox
        assert inbox is not None
        now = self.clock()
        with self.session_factory() as session:
            opened = session.scalar(select(WatchScan.id).where(WatchScan.request_id == request.id))
        if opened is not None:  # only its file was left behind
            remove_request(inbox, request)
            return False
        stock = next((s for s in self.stocks if s.symbol == request.symbol), None)
        max_age = timedelta(minutes=self.settings.watch.scan_request_max_age_minutes)
        if stock is None or now - request.requested_at > max_age:
            log.info(
                "scan of %s dropped: %s",
                request.symbol,
                "not a stock on the watchlist" if stock is None else "asked too long ago",
            )
            remove_request(inbox, request)
            return False
        self.activity = (f"scan {stock.symbol}", now)
        with self.session_factory() as session:
            scan = WatchScan(
                request_id=request.id,
                symbol=stock.symbol,
                requested_at=request.requested_at,
                started_at=now,
                updated_at=now,
                status="running",
                steps=new_steps(),
                errors=[],
            )
            session.add(scan)
            session.commit()
            progress = _ScanProgress(self.session_factory, scan.id, self.clock)
        remove_request(inbox, request)
        try:
            outcome = self._on_demand_pass(stock, now, progress)
            progress.finish("done", outcome)
        except Exception as exc:  # the scanner outlives any one scan
            log.exception("scan of %s failed", stock.symbol)
            outcome = _ScanOutcome(
                errors=[{"stage": "scan", "error": f"{type(exc).__name__}: {exc}"}]
            )
            progress.finish("failed", outcome)
        finally:
            self.activity = ("idle", self.clock())
        with self.session_factory() as session:
            # A pass of its own on the runs page, and a backlog for the lead-time table: what it
            # "first saw" may have been out for days.
            session.add(
                WatchRun(
                    job="on_demand",
                    started_at=now,
                    finished_at=self.clock(),
                    on_ac=on_ac_power(),
                    entries=outcome.entries,
                    new_articles=outcome.new_articles,
                    new_filings=outcome.new_filings,
                    errors=outcome.errors,
                    details={
                        "symbol": stock.symbol,
                        "request_id": request.id,
                        "scan": progress.scan_id,
                    }
                    | ({"stale_skipped": outcome.stale} if outcome.stale else {}),
                )
            )
            session.commit()
        log.info("scan of %s: %s", stock.symbol, outcome.line())
        return True

    def _on_demand_pass(
        self, stock: WatchItem, now: datetime, progress: "_ScanProgress"
    ) -> "_ScanOutcome":
        """Google News and NSE's API for this one stock over the last few days, what the PEAD
        tool has written for BSE, a fresh price with Yahoo's profile, then the usual call on
        its stories. Stored and grouped exactly as a regular pass stores and groups."""
        since = now - timedelta(days=self.settings.watch.on_demand_days)
        outcome = _ScanOutcome()

        progress.step("google_news", "running")
        queries = google_news_queries([stock], self.settings, google_news_window(since, now))
        reads, _ = asyncio.run(self._fetch(queries, include_nse=False, now=now))
        found = sum(len(read.articles) for read in reads)
        failed = [read.fetched.error for read in reads if read.fetched.error]
        outcome.entries += found
        progress.step(
            "google_news",
            "failed" if failed and not found else "done",
            f"{found} results over {self.settings.watch.on_demand_days} days"
            + (f"; {len(failed)} of {len(reads)} searches failed: {failed[0]}" if failed else ""),
        )

        history: dict[str, tuple[list[Announcement], str | None]] = {}
        if stock.nse_symbol:
            progress.step("nse", "running")
            announced, error = asyncio.run(self._nse_for(stock.nse_symbol, since, now))
            history[stock.symbol] = (announced, error)
            progress.step(
                "nse",
                "failed" if error else "done",
                error or f"{len(announced)} announcements since {self._day(since)}",
            )
        else:
            progress.step("nse", "skipped", "not listed on NSE")

        progress.step("bse", "running")
        shared = self._read_bse(now)
        if shared is None:
            progress.step("bse", "skipped", "BSE filings are off (no PEAD file or codes)")
        elif shared.error:
            progress.step("bse", "skipped", shared.error)
        else:
            mine = sum(1 for symbol, _ in shared.found if symbol == stock.symbol)
            wrote = (
                f"; the PEAD tool last wrote at {self._local(shared.last_write)}"
                if shared.last_write
                else ""
            )
            progress.step("bse", "done", f"{mine} new for it from the PEAD tool{wrote}")

        # BSE first, exactly as a regular pass stores it: what the PEAD tool backfilled late
        # gets its usual summary. Then this stock's own finds, which never start one: opening a
        # card sends nothing to Telegram that wouldn't have been sent anyway (the alerts job
        # applies the usual rules to what is stored here, keyed so nothing goes twice).
        if shared is not None and shared.error is None:
            outcome.errors += self._process("on_demand", [], None, now, shared=shared).errors
        own = self._process("on_demand", reads, None, now, history=history, record_late=False)
        outcome.errors += own.errors
        outcome.stale = own.stale
        if skipped := sum(own.stale.values()):
            progress.step(
                "google_news",
                "failed" if failed and not found else "done",
                f"{found} results over {self.settings.watch.on_demand_days} days; {skipped} "
                f"older than a week skipped"
                + (
                    f"; {len(failed)} of {len(reads)} searches failed: {failed[0]}"
                    if failed
                    else ""
                ),
            )

        progress.step("price", "running")
        state, detail, profile = self._price_now(stock, outcome)
        if profile is not None:
            progress.update(profile=profile_to_json(profile))
        progress.step("price", state, detail)

        self._call_for(stock, now, progress, outcome)
        outcome.new_articles, outcome.new_filings, outcome.new_stories = self._found_at(stock, now)
        return outcome

    async def _nse_for(
        self, nse_symbol: str, since: datetime, now: datetime
    ) -> tuple[list[Announcement], str | None]:
        async with make_client(self.settings.http, transport=self.transport) as client:
            return await fetch_nse_history(client, nse_symbol, since, now, self.settings)

    def _price_now(
        self, stock: WatchItem, outcome: "_ScanOutcome"
    ) -> tuple[str, str, Profile | None]:
        """A fresh quote, stored like the prices job's, and Yahoo's profile - a year of daily
        closes, sector, industry, market cap - which the dialog shows without asking Yahoo
        itself."""
        parts, state = [], "done"
        if self.snapshots is not None:
            polled = self.clock()
            try:
                snap = self.snapshots.snapshot(stock.symbol)
                row = _price_row(stock.symbol, polled, snap)
                if snap.price is not None:
                    parts.append(f"last {snap.price:,.2f}")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                row = WatchPrice(symbol=stock.symbol, polled_at=polled, error=error)
                outcome.errors.append({"stage": "price", "error": error})
                parts.append(f"no quote ({error})")
                state = "failed"
            with self.session_factory() as session:
                session.add(row)
                session.commit()
        else:
            parts.append("no quote source")
        profile = None
        if self.profile_fetch is not None:
            try:
                profile = self.profile_fetch(stock.symbol)
                parts.append(
                    f"{len(profile.closes)} daily closes"
                    + (f"; profile incomplete ({profile.error})" if profile.error else "")
                )
            except Exception as exc:
                outcome.errors.append({"stage": "profile", "error": f"{type(exc).__name__}: {exc}"})
                parts.append("no Yahoo profile")
        return state, "; ".join(parts), profile

    def _call_for(
        self, stock: WatchItem, now: datetime, progress: "_ScanProgress", outcome: "_ScanOutcome"
    ) -> None:
        """The usual watch-lane call on this stock's stories, through the same limiter. With
        the lane's budget spent, the headlines stay stored and nothing is called."""
        if self.llm is None:
            progress.step("analyse", "skipped", "no LLM key, so nothing is called")
            return
        if self._calls_paused_until is not None and now < self._calls_paused_until:
            outcome.note = self._lane_spent()
            progress.step("analyse", "skipped", "the watch lane's budget is spent")
            return
        progress.step("analyse", "running")
        for note in read_replies(self.session_factory, self.settings):
            log.info("watch reply: %s", note)
        result = analyse(
            self.session_factory,
            self.llm,
            self.settings,
            {item.symbol: item for item in self.stocks},
            now,
            self._bad_inputs,
            only={stock.symbol},
        )
        outcome.calls = len(result.called)
        outcome.errors += [
            {"stage": "analyse", "story_id": story_id, "error": error}
            for story_id, error in result.failed
        ]
        if result.stopped:
            if result.quota_spent and self.llm.limiter is not None:
                self._calls_paused_until = self.llm.limiter.next_reset()
            outcome.note = (
                self._lane_spent() if result.quota_spent else f"Not called: {result.stopped}"
            )
            progress.step("analyse", "skipped", outcome.note)
            return
        stories = len(result.called)
        progress.step(
            "analyse", "done", f"{stories} {'story' if stories == 1 else 'stories'} called"
        )

    def _lane_spent(self) -> str:
        resume = (
            f" Calls resume at {self._local(self._calls_paused_until)}."
            if self._calls_paused_until
            else ""
        )
        return (
            "The watch lane's daily budget is spent, so nothing was called; the headlines are "
            f"stored and will be called once it resets.{resume}"
        )

    def _found_at(self, stock: WatchItem, now: datetime) -> tuple[int, int, int]:
        """What this scan stored for the stock: articles naming it (never a drop), its
        filings, and the stories it is in, all first seen at `now`."""
        with self.session_factory() as session:
            articles = session.scalar(
                select(func.count(distinct(WatchArticle.id)))
                .join(WatchMatch)
                .where(
                    WatchMatch.symbol == stock.symbol,
                    WatchMatch.verdict != "drop",
                    WatchArticle.first_seen_at == now,
                )
            )
            filings = session.scalar(
                select(func.count(WatchFiling.id)).where(
                    WatchFiling.symbol == stock.symbol, WatchFiling.first_seen_at == now
                )
            )
            stories = [
                story
                for story in session.scalars(
                    select(WatchStory).where(WatchStory.first_seen_at == now)
                )
                if stock.symbol in kept_symbols(story)
            ]
        return articles or 0, filings or 0, len(stories)

    def _day(self, moment: datetime) -> str:
        return moment.astimezone(self.settings.tz).strftime("%d %b")

    def _local(self, moment: datetime | None) -> str:
        return moment.astimezone(self.settings.tz).strftime("%H:%M") if moment else ""

    # ------------------------------------------------------------ fetching

    async def _fetch(
        self, feeds: Sequence[FeedConfig], include_nse: bool, now: datetime
    ) -> tuple[list[FeedRead], Fetched | None]:
        async with make_client(self.settings.http, transport=self.transport) as client:
            reads = asyncio.gather(
                *(
                    read_feed(client, feed, self.validators, self.settings, self.resolver, now)
                    for feed in feeds
                )
            )
            if not include_nse:
                return list(await reads), None
            nse = conditional_get(
                client, self.settings.watch.nse_announcements_url, self.validators, self.settings
            )
            done, nse_fetched = await asyncio.gather(reads, nse)
            return list(done), nse_fetched

    # ------------------------------------------------------------ storing

    def _process(
        self,
        job: str,
        reads: list[FeedRead],
        nse: Fetched | None,
        now: datetime,
        history: dict[str, tuple[list[Announcement], str | None]] | None = None,
        shared: SharedRead | None = None,
        record_late: bool = True,
    ) -> JobResult:
        """Match, store and group what a pass read. `record_late`: filings that arrived late
        get a "while you were away" summary of their own (`_record_late`). A catch-up's summary
        already takes them, and a scan of one stock never starts one."""
        result = JobResult(job)
        checks = [
            self._feed_check(read.feed, read.fetched, read.articles, now)
            for read in reads
            if read.feed.url in self._checked
        ]
        candidates: list[_Candidate] = []
        for read in reads:
            result.entries += len(read.articles)
            if read.fetched.error:
                result.errors.append({"feed": read.feed.name, "error": read.fetched.error})
            for entry in read.articles:
                matches = self.matcher.match(entry.title, entry.snippet)
                if matches:
                    candidates.append(_Candidate(entry, read.feed, matches))

        announcements: list[tuple[str, Announcement]] = []
        if nse is not None:
            parsed: list[Announcement] = []
            if nse.content is not None:
                try:
                    parsed = parse_nse_announcements(nse.content)
                except Exception as exc:
                    nse.status, nse.error = "error", f"{type(exc).__name__}: {exc}"
            if nse.error:
                result.errors.append({"feed": NSE_FEED_NAME, "error": nse.error})
            nse_feed = FeedConfig(
                name=NSE_FEED_NAME,
                url=self.settings.watch.nse_announcements_url,
                region="IN",
                weight=3,
            )
            checks.append(
                self._feed_check(
                    nse_feed,
                    nse,
                    [],
                    now,
                    entries=len(parsed),
                    newest=max((a.filed_at for a in parsed), default=None),
                )
            )
            result.entries += len(parsed)
            announcements = [
                (item.symbol, found) for item, found in watched_announcements(parsed, self.stocks)
            ]
        for symbol, (found, error) in (history or {}).items():
            announcements += [(symbol, item) for item in found]
            if error:
                result.errors.append({"stage": "nse history", "symbol": symbol, "error": error})
        if shared is not None:
            announcements += shared.found

        with self.session_factory() as session:
            window = self._window_members(session, now)
            fresh_filings = self._unseen_filings(session, announcements)
            # Everything slow happens before the first write: embed what this pass needs.
            self._warm(
                [member.text for member in window]
                + [c.text for c in candidates]
                + [_filing_text(found) for _, found in fresh_filings]
            )

            session.add_all(checks)
            new_articles = self._store_articles(session, candidates, now, result.stale)
            new_filings = [
                WatchFiling(
                    key=found.key,
                    exchange=found.exchange,
                    symbol=symbol,
                    company=found.company,
                    subject=found.subject,
                    description=found.description,
                    link=found.link,
                    filed_at=found.filed_at,
                    first_seen_at=now,
                    kind=found.kind,
                    quoted_headline=found.quoted_headline,
                )
                for symbol, found in fresh_filings
            ]
            session.add_all(new_filings)
            session.flush()
            result.stories_created = self._group(session, window, new_articles, new_filings, now)
            if shared is not None and shared.position is not None:
                # In the same transaction as the filings: a crash re-reads, never skips.
                session.merge(
                    WatchCursor(source=BSE_CURSOR, position=shared.position, updated_at=now)
                )
            if record_late:
                self._record_late(session, new_filings, now)
            session.commit()
        result.new_articles = len(new_articles)
        result.new_filings = len(new_filings)
        return result

    def _feed_check(
        self,
        feed: FeedConfig,
        fetched: Fetched,
        articles: Sequence[FetchedArticle],
        now: datetime,
        entries: int | None = None,
        newest: datetime | None = None,
    ) -> FeedCheck:
        new_entries: int | None = None
        if fetched.status == "ok" and entries is None:
            urls = {article.url for article in articles}
            before = self.carried.get(feed.url)
            new_entries = len(urls - before) if before is not None else None
            self.carried[feed.url] = urls
        elif fetched.status == "not_modified" and feed.url in self.carried:
            new_entries = 0
        return FeedCheck(
            kind="watch",
            feed_name=feed.label,
            feed_url=feed.url,
            checked_at=now,
            status=fetched.status,
            http_status=fetched.http_status,
            entries=len(articles) if entries is None else entries,
            new_entries=new_entries,
            newest_entry_at=newest
            if newest is not None
            else max((article.published_at for article in articles), default=None),
            error=fetched.error,
            elapsed_ms=fetched.elapsed_ms,
        )

    def _store_articles(
        self,
        session: Session,
        candidates: Sequence[_Candidate],
        now: datetime,
        stale: dict[str, int] | None = None,
    ) -> list[WatchArticle]:
        """Store what a pass read, or add a sighting to what it already had. An article
        published more than `watch.catch_up_max_days` before BASIS first sees it is not stored
        (user, 2026-10-19): a Google News search returns old pages whatever window it asks for -
        a 2020 price page, a 2024 results story - and stored, each was a story first seen today.
        It applies to every source and path - the feeds, the searches, a catch-up, a scan of
        one stock - and is counted, by source, in `stale`. Filings are not affected."""
        created: list[WatchArticle] = []
        oldest = now - stale_after(self.settings)
        turned_away: set[tuple[str, str]] = set()  # an article in several searches counts once
        for candidate in candidates:
            entry = candidate.entry
            article = self._existing(session, entry, now)
            if article is None and entry.published_at < oldest:
                source = "Google News" if candidate.feed.is_google_news else candidate.feed.name
                if stale is not None and (source, entry.url) not in turned_away:
                    turned_away.add((source, entry.url))
                    stale[source] = stale.get(source, 0) + 1
                continue
            if article is None:
                article = WatchArticle(
                    url=entry.url,
                    source_name=entry.source_name,
                    title=entry.title,
                    snippet=entry.snippet,
                    published_at=entry.published_at,
                    first_seen_at=now,
                    matches=[_match_row(m) for m in candidate.matches],
                )
                session.add(article)
                session.flush()
                created.append(article)
            elif entry.snippet and not article.snippet:
                # First seen through Google News, which carries no snippet; the outlet's own
                # feed has one, and it can change a verdict (a weak alias finds its context).
                article.snippet = entry.snippet
                rejudged = {m.symbol: m for m in self.matcher.match(article.title, entry.snippet)}
                for row in article.matches:
                    if (fresh := rejudged.pop(row.symbol, None)) is not None:
                        row.verdict, row.reason, row.alias = (
                            fresh.verdict,
                            fresh.reason,
                            fresh.alias,
                        )
                article.matches.extend(_match_row(m) for m in rejudged.values())
            via = "google_news" if candidate.feed.is_google_news else "rss"
            # One sighting per feed - but all the Google News searches are one channel, and
            # an article naming two stocks turns up in two of them.
            if not any(
                s.feed_url == candidate.feed.url or (via == "google_news" == s.via)
                for s in article.sightings
            ):
                article.sightings.append(
                    WatchSighting(
                        feed_name=candidate.feed.name,
                        feed_url=candidate.feed.url,
                        via=via,
                        seen_at=now,
                    )
                )
        return created

    def _existing(
        self, session: Session, entry: FetchedArticle, now: datetime
    ) -> WatchArticle | None:
        """The stored article this entry is: by URL, or the same outlet with a near-identical
        title, which is how a Google News link meets the outlet's own."""
        found = session.scalar(select(WatchArticle).where(WatchArticle.url == entry.url))
        if found is not None:
            return found
        source = normalize_source(entry.source_name)
        title = normalize_title(entry.title)
        threshold = self.settings.dedupe.syndication_title_similarity
        for other in session.scalars(
            select(WatchArticle).where(WatchArticle.first_seen_at >= now - SAME_ARTICLE_WINDOW)
        ):
            if (
                normalize_source(other.source_name) == source
                and fuzz.token_sort_ratio(title, normalize_title(other.title)) >= threshold
            ):
                return other
        return None

    def _unseen_filings(
        self, session: Session, announcements: Sequence[tuple[str, Announcement]]
    ) -> list[tuple[str, Announcement]]:
        """The announcements not stored yet. The same one arrives from the RSS and, in a
        catch-up, from the API: it is the same filing when the exchange, the company, the time
        and the subject agree. A company's BSE copy of an NSE filing is kept as its own row."""
        if not announcements:
            return []
        symbols = {symbol for symbol, _ in announcements}
        earliest = min(found.filed_at for _, found in announcements)
        known = {
            (exchange, symbol, filed_at, subject.casefold())
            for exchange, symbol, filed_at, subject in session.execute(
                select(
                    WatchFiling.exchange,
                    WatchFiling.symbol,
                    WatchFiling.filed_at,
                    WatchFiling.subject,
                ).where(WatchFiling.symbol.in_(symbols), WatchFiling.filed_at >= earliest)
            )
        }
        fresh: dict[tuple[str, str, datetime, str], tuple[str, Announcement]] = {}
        for symbol, found in announcements:
            identity = (found.exchange, symbol, found.filed_at, found.subject.casefold())
            if identity not in known:
                fresh.setdefault(identity, (symbol, found))
        # Oldest first, so a story's seed is the earliest filing.
        return sorted(fresh.values(), key=lambda pair: pair[1].filed_at)

    def _record_late(self, session: Session, filings: Sequence[WatchFiling], now: datetime) -> None:
        """Filings that reached BASIS more than an hour after they were filed - the PEAD
        tool backfilling BSE after it was started, typically - go into a "while you were
        away" summary, never an alert of their own (user, 2026-10-08). A catch-up's own
        summary already takes them; any other pass records a `late` run for one - except the
        very first pass, which has nothing to be late against (a new install reads the day's
        filings; nobody was away)."""
        away = timedelta(minutes=self.settings.watch.away_after_minutes)
        late = [filing for filing in filings if arrived_late(filing, away)]
        if not late:
            return
        earlier = session.scalar(
            select(WatchRun.id).where(WatchRun.job == "feeds", WatchRun.started_at < now).limit(1)
        )
        if earlier is None:
            return
        session.add(
            WatchRun(
                job="late",
                started_at=now,
                finished_at=now,
                new_filings=len(late),
                errors=[],
                details={
                    "gap": {
                        "start": min(f.filed_at for f in late).isoformat(),
                        "end": now.isoformat(),
                    },
                    "exchanges": sorted({f.exchange for f in late}),
                    "possible_gaps": [],
                    "summary_sent": False,
                },
            )
        )
        log.info(
            "%d filings arrived late (%s); they go into a summary",
            len(late),
            ", ".join(f"{f.exchange} {f.symbol}" for f in late),
        )

    # ------------------------------------------------------------ catching up

    def _record_catch_up(
        self,
        gap: Gap,
        reads: Sequence[FeedRead],
        searches: Sequence[FeedConfig],
        nse: Fetched | None,
        history: dict[str, tuple[list[Announcement], str | None]],
        result: JobResult,
        now: datetime,
    ) -> None:
        """What the catch-up reached, source by source, and what no source could: the
        "while you were away" summary is built from this row."""
        sources: list[SourceCoverage] = []
        search_urls = {feed.url for feed in searches}
        for read in reads:
            if read.feed.url in search_urls:
                continue
            fetched = read.fetched
            if fetched.status == "not_modified":
                # Unchanged since the last fetch, which was before the gap: nothing missed.
                reached: datetime | None = gap.start
            elif fetched.status == "ok" and read.articles:
                reached = min(article.published_at for article in read.articles)
            else:
                reached = None
            sources.append(
                SourceCoverage(
                    "news", f"{read.feed.name} ({read.feed.url})", reached, fetched.error or ""
                )
            )
        searched = [read for read in reads if read.feed.url in search_urls]
        failed = [read for read in searched if read.fetched.error]
        capped = [read for read in searched if len(read.articles) >= GOOGLE_NEWS_CAP]
        if failed or not searched:
            sources.append(
                SourceCoverage("news", "Google News", None, f"{len(failed)} searches failed")
            )
        elif capped:
            oldest = max(min(a.published_at for a in read.articles) for read in capped)
            sources.append(
                SourceCoverage(
                    "news", "Google News", oldest, f"{len(capped)} searches hit the 100-result cap"
                )
            )
        else:
            sources.append(SourceCoverage("news", "Google News", gap.start))

        midnight = now.astimezone(self.settings.tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        history_errors = [symbol for symbol, (_, error) in history.items() if error]
        rss_ok = nse is not None and nse.error is None
        if gap.start >= midnight and rss_ok:
            nse_reached: datetime | None = gap.start
        elif history and not history_errors:
            nse_reached = gap.start
        else:
            nse_reached = midnight if rss_ok else None
        sources.append(
            SourceCoverage(
                "filings",
                "NSE",
                nse_reached,
                f"API failed for {', '.join(history_errors)}" if history_errors else "",
            )
        )
        sources.append(self._backfill_prices(gap, now))

        details = {
            "gap": {"start": gap.start.isoformat(), "end": gap.end.isoformat()},
            "sources": [source.as_dict() for source in sources],
            "possible_gaps": possible_gaps(gap, sources),
            "summary_sent": False,
        }
        with self.session_factory() as session:
            session.add(
                WatchRun(
                    job="catchup",
                    started_at=now,
                    finished_at=self.clock(),
                    entries=result.entries,
                    new_articles=result.new_articles,
                    new_filings=result.new_filings,
                    errors=result.errors,
                    details=details,
                )
            )
            session.commit()
        log.info(
            "catch-up from %s: %d new articles, %d new filings, possible gaps: %s",
            gap.start.astimezone(self.settings.tz).strftime("%a %d %b %H:%M"),
            result.new_articles,
            result.new_filings,
            details["possible_gaps"] or "none",
        )

    def _backfill_prices(self, gap: Gap, now: datetime) -> SourceCoverage:
        """Prices for the missed sessions from Yahoo's 1-minute bars, one row every
        `prices_every_minutes` like the live polls, so "moved, no story yet" can be checked
        for the time the laptop was off."""
        if self.history is None:
            return SourceCoverage("prices", "Yahoo 1-minute bars", None, "no price source")
        start = max(gap.start, now - MINUTE_HISTORY)
        every = self.settings.watch.prices_every_minutes
        indices = [group.index for group in self.watchlist.groups.values() if group.index]
        symbols = price_symbols(
            [stock.symbol for stock in self.stocks], indices, self.settings.watch.benchmark
        )
        rows: list[WatchPrice] = []
        failures: list[str] = []
        for symbol in symbols:
            try:
                closes = self.history.session_closes(symbol, 40)
                bars = self.history.minute_closes(symbol, start, now)
            except Exception as exc:
                failures.append(f"{symbol}: {type(exc).__name__}")
                continue
            rows += backfill_rows(symbol, bars, closes, every, self.settings)
        with self.session_factory() as session:
            have = {
                (symbol, polled)
                for symbol, polled in session.execute(
                    select(WatchPrice.symbol, WatchPrice.polled_at).where(
                        WatchPrice.polled_at >= start
                    )
                )
            }
            session.add_all(row for row in rows if (row.symbol, row.polled_at) not in have)
            session.commit()
        note = f"{len(rows)} prices filled in"
        if failures:
            note += f"; failed: {', '.join(failures)}"
        reached = start if len(failures) < len(symbols) else None
        return SourceCoverage("prices", "Yahoo 1-minute bars", reached, note)

    # ------------------------------------------------------------ grouping

    def _window_members(self, session: Session, now: datetime) -> list[_Member]:
        """Every grouped article and filing still inside the story window, oldest first.
        Nothing first seen after `now`: a live pass has nothing newer, and a replay of a past
        pass (scripts/split_commentary_stories.py) must not see the future."""
        since = now - timedelta(hours=self.settings.watch.story_window_hours)
        members: list[_Member] = []
        for article in session.scalars(
            select(WatchArticle).where(
                WatchArticle.story_id.is_not(None),
                WatchArticle.first_seen_at >= since,
                WatchArticle.first_seen_at <= now,
            )
        ):
            members.append(
                _Member(
                    seen=article.first_seen_at,
                    story_id=article.story_id,  # type: ignore[arg-type]
                    symbols=frozenset(m.symbol for m in article.matches),
                    text=article_text(article.title, article.snippet),
                    kind="article",
                    headline=article.title,
                    published=min(article.published_at, article.first_seen_at),
                )
            )
        for filing in session.scalars(
            select(WatchFiling).where(
                WatchFiling.story_id.is_not(None),
                WatchFiling.first_seen_at >= since,
                WatchFiling.first_seen_at <= now,
            )
        ):
            members.append(
                _Member(
                    seen=filing.first_seen_at,
                    story_id=filing.story_id,  # type: ignore[arg-type]
                    symbols=frozenset({filing.symbol}),
                    text=_stored_filing_text(filing),
                    kind="filing",
                    headline=filing.subject,
                    published=filing.filed_at,
                )
            )
        members.sort(key=lambda member: member.seen)
        return members

    def _warm(self, texts: Sequence[str]) -> None:
        """Embed every text this pass needs that isn't cached, then forget the rest."""
        if self.embedder is None:
            return
        needed = dict.fromkeys(texts)
        missing = [text for text in needed if text not in self._vectors]
        if missing:
            for text, vector in zip(missing, self.embedder.embed(missing), strict=True):
                self._vectors[text] = vector
        self._vectors = {text: self._vectors[text] for text in needed}

    def _group(
        self,
        session: Session,
        window: list[_Member],
        articles: Sequence[WatchArticle],
        filings: Sequence[WatchFiling],
        now: datetime,
    ) -> int:
        """Place each new article and filing in a story; returns how many stories it made.

        Embedding similarity decides, as in the pipeline, with three additions
        (app/watch/events.py): items stating different money amounts never share a story;
        order news about one stock within `order_attach_hours` of an order story joins the one
        reported closest to it, before similarity is asked (an NSE order filing's text is the
        same for every order); and a story begun by share-price commentary - its headline is
        its first item's - takes no event: no filing, no article that `is_event` (user,
        2026-10-19). Commentary may still join an event's story."""
        grouper: StoryGrouper[int] = StoryGrouper(
            self.settings.grouping.embedding_threshold,
            self.settings.grouping.seed_threshold,
            timedelta(hours=self.settings.watch.story_window_hours),
        )
        orders = _OrderStories(timedelta(hours=self.settings.watch.order_attach_hours))
        begun = session.execute(
            select(WatchStory.id, WatchStory.headline).where(
                WatchStory.id.in_({member.story_id for member in window})
            )
        ).all()
        commentary = {story_id for story_id, headline in begun if is_commentary(headline)}
        for member in window:
            stated = amounts(member.headline)
            if self.embedder is not None:
                grouper.add(
                    member.story_id,
                    self._vectors[member.text],
                    member.symbols,
                    member.seen,
                    stated,
                    commentary=member.story_id in commentary,
                )
            if is_order_news(member.headline) and member.story_id not in commentary:
                orders.add(member.story_id, member.symbols, member.published, stated)
        created = 0

        def place(
            text: str,
            symbols: set[str],
            headline: str,
            seen: datetime,
            published: datetime,
            event: bool,
        ) -> WatchStory:
            nonlocal created
            stated = amounts(headline)
            order = is_order_news(headline)
            # Order news goes to the stock's order story reported closest to it first: the
            # words of two reports of one order differ more than the model allows for
            # ("Expands order book" / "Bags Rs 1,307 crore Navy contract"), and an NSE order
            # filing has no words of its own at all.
            story_id = orders.find(symbols, published, stated) if order else None
            if story_id is None and self.embedder is not None:
                story_id = grouper.match(self._vectors[text], symbols, seen, stated, event)
            story = session.get(WatchStory, story_id) if story_id is not None else None
            if story is None:
                story = WatchStory(first_seen_at=seen, headline=headline)
                session.add(story)
                session.flush()
                created += 1
            if self.embedder is not None:
                grouper.add(
                    story.id,
                    self._vectors[text],
                    symbols,
                    seen,
                    stated,
                    commentary=is_commentary(story.headline),
                )
            if order:
                orders.add(story.id, symbols, published, stated)
            return story

        # Oldest first by the publisher's time: a catch-up reads hours of news in one pass, and
        # the specific report ("Bags Rs 1,307 crore Navy contract") usually comes before the
        # vague one ("Expands order book") that should join it.
        for article in sorted(
            articles, key=lambda a: (min(a.published_at, a.first_seen_at), a.first_seen_at)
        ):
            text = article_text(article.title, article.snippet)
            article.story = place(
                text,
                {m.symbol for m in article.matches},
                article.title,
                article.first_seen_at,
                min(article.published_at, article.first_seen_at),
                is_event(article.title),
            )
        for filing in filings:
            story = self._quoted_story(session, filing, now)
            if story is None:
                story = place(
                    _stored_filing_text(filing),
                    {filing.symbol},
                    f"{filing.company}: {filing.subject}",
                    filing.first_seen_at,
                    filing.filed_at,
                    True,  # a filing is the company's own event, whatever it says
                )
            filing.story = story
        return created

    def _quoted_story(
        self, session: Session, filing: WatchFiling, now: datetime
    ) -> WatchStory | None:
        """NSE quotes the media headline it is asking about word for word ("...news item
        captioned Prime Focus shares tank 8%..."), which ties the filing to that story
        exactly, however differently the two are worded otherwise."""
        if not filing.quoted_headline:
            return self._story_named_in(session, filing, now)
        quoted = normalize_title(filing.quoted_headline)
        since = now - timedelta(hours=self.settings.watch.story_window_hours)
        threshold = self.settings.dedupe.syndication_title_similarity
        for article in session.scalars(
            select(WatchArticle)
            .join(WatchMatch)
            .where(
                WatchMatch.symbol == filing.symbol,
                WatchArticle.first_seen_at >= since,
                WatchArticle.first_seen_at <= now,
                WatchArticle.story_id.is_not(None),
            )
            .order_by(WatchArticle.first_seen_at)
        ):
            if fuzz.token_sort_ratio(quoted, normalize_title(article.title)) >= threshold:
                return article.story
        # An earlier notice about the same headline (the reply follows the request).
        earlier = session.scalar(
            select(WatchFiling)
            .where(
                WatchFiling.symbol == filing.symbol,
                WatchFiling.quoted_headline == filing.quoted_headline,
                WatchFiling.story_id.is_not(None),
                WatchFiling.id != filing.id,
                WatchFiling.first_seen_at <= now,
            )
            .order_by(WatchFiling.filed_at)
        )
        return earlier.story if earlier is not None else None

    def _story_named_in(
        self, session: Session, filing: WatchFiling, now: datetime
    ) -> WatchStory | None:
        """A filing whose own text carries a watchlist headline word for word - a BSE reply
        to a news item, say, whose wording BASIS has no parser for - joins that headline's
        story, so a reply lands on the story it answers (user, 2026-10-08). Only headlines of
        QUOTE_MIN_WORDS words or more count: a short one could turn up by chance."""
        text = f" {normalize_title(filing.description)} "
        since = now - timedelta(hours=self.settings.watch.story_window_hours)
        for article in session.scalars(
            select(WatchArticle)
            .join(WatchMatch)
            .where(
                WatchMatch.symbol == filing.symbol,
                WatchArticle.first_seen_at >= since,
                WatchArticle.first_seen_at <= now,
                WatchArticle.story_id.is_not(None),
            )
            .order_by(WatchArticle.first_seen_at)
        ):
            title = normalize_title(article.title)
            if len(title.split()) >= QUOTE_MIN_WORDS and f" {title} " in text:
                return article.story
        return None


WATCH_FEEDS = "watch_feeds.yaml"
TICK_SECONDS = 15  # short, so a laptop waking from sleep is noticed quickly
SCAN_CHECK_SECONDS = 2  # how often an idle scanner looks for a scan the page asked for
PROFILE_TIMEOUT = 20  # seconds, for each of Yahoo's two profile requests


def build_watcher(
    settings: Settings,
    session_factory: sessionmaker[Session],
    embedder: Embedder | None,
    snapshots: SnapshotProvider | None,
) -> Watcher:
    """The scanner over config/watchlist.yaml and config/watch_feeds.yaml. The main feeds
    are passed to the resolver too, so a Google News outlet is named as its own feed names
    it ("CNBC TV18" is CNBC-TV18)."""
    from app.config import CONFIG_DIR, load_feeds, load_watchlist_file
    from app.watch.preview import yahoo_profile

    watch_feeds = load_feeds(CONFIG_DIR / WATCH_FEEDS)
    resolver = SourceResolver([*load_feeds(include_disabled=True), *watch_feeds])
    return Watcher(
        settings,
        session_factory,
        load_watchlist_file(),
        watch_feeds,
        resolver,
        embedder,
        snapshots,
        history=YahooHistory(),
        llm=_watch_llm(settings, session_factory),
        sender=_telegram(settings),
        watchlist_path=CONFIG_DIR / "watchlist.yaml",
        companies_path=settings.resolve_path(COMPANY_LIST_FILE),
        scan_inbox=settings.resolve_path(settings.watch.scan_requests_dir),
        profile_fetch=yahoo_profile(PROFILE_TIMEOUT),
    )


def _telegram(settings: Settings) -> Sender | None:
    """Sends a list of messages to the user's chat, or None without credentials (alerts are
    then recorded with that as their error, never lost silently)."""
    from app.config import get_secret
    from app.delivery.telegram import send_messages

    token, chat_id = get_secret("TELEGRAM_BOT_TOKEN"), get_secret("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("watchlist alerts can't be sent: no Telegram credentials")
        return None

    def send(messages: list[str]) -> None:
        asyncio.run(send_messages(messages, token, chat_id, settings.http))

    return send


def _watch_llm(settings: Settings, session_factory: sessionmaker[Session]) -> LLMClient | None:
    """The client for the watchlist call, or None (the scanner then stores and groups but
    calls nothing) when it can't be built - no API key, say."""
    from app.llm.client import LLMConfigError, make_llm_client

    try:
        return make_llm_client(settings.llm, session_factory, settings.tz)
    except LLMConfigError as exc:
        log.warning("watchlist calls off: %s", exc)
        return None


def run_forever(
    watcher: Watcher,
    sleep: Callable[[float], None],
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Run due jobs until stopped. A job's failure is logged and recorded, never fatal.
    Between ticks it looks for scans the /watchlist page asked for every couple of seconds,
    so one starts within moments of the click, not at the next tick."""
    while not should_stop():
        try:
            # Held only while jobs run: a scan that starts finishes before the laptop sleeps.
            with keep_awake():
                for result in watcher.tick():
                    log.info("watch %s", result.line())
        except Exception:  # anything tick itself didn't catch: log it and keep going
            log.exception("watch tick failed")
        waited = 0.0
        while waited < TICK_SECONDS and not should_stop():
            try:
                if watcher.scan_inbox is not None and pending_requests(watcher.scan_inbox):
                    with keep_awake():
                        watcher.serve_scan_requests()
            except Exception:
                log.exception("scan request failed")
            sleep(SCAN_CHECK_SECONDS)
            waited += SCAN_CHECK_SECONDS


def start_heartbeat(watcher: Watcher, every: float = HEARTBEAT_EVERY) -> Callable[[], None]:
    """Write the scanner's heartbeat (app/watch/ondemand.py) every `every` seconds from a
    thread of its own, so the page can tell a scanner that isn't running from one in the middle
    of a long job. The thread writes that one file and nothing else - never the database.
    Returns a function that stops it and removes the file, so the page knows at once."""
    inbox = watcher.scan_inbox
    if inbox is None:
        return lambda: None
    stop = threading.Event()

    def beat() -> None:
        while True:
            activity, since = watcher.activity
            try:
                write_heartbeat(inbox, activity, since, utcnow())
            except OSError as exc:
                log.warning("scanner heartbeat not written: %s", exc)
            if stop.wait(every):
                return

    thread = threading.Thread(target=beat, name="heartbeat", daemon=True)
    thread.start()

    def finish() -> None:
        stop.set()
        thread.join(timeout=5)
        clear_heartbeat(inbox)

    return finish


def _mtime(path: Path | None) -> float | None:
    try:
        return path.stat().st_mtime if path is not None else None
    except OSError:
        return None


def _price_row(symbol: str, polled: datetime, snap: Snapshot) -> WatchPrice:
    return WatchPrice(
        symbol=symbol,
        polled_at=polled,
        price=snap.price,
        previous_close=snap.previous_close,
        last_trade_at=snap.last_trade_at,
        newest_bar_at=snap.newest_bar_at,
        day_open=snap.day_open,
        day_high=snap.day_high,
        day_low=snap.day_low,
    )


@dataclass
class _ScanOutcome:
    """What one scan of one stock found, for its row, its run and the log."""

    entries: int = 0
    new_articles: int = 0
    new_filings: int = 0
    new_stories: int = 0
    calls: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None
    stale: dict[str, int] = field(default_factory=dict)  # old news not stored, by source

    def line(self) -> str:
        skipped = sum(self.stale.values())
        return (
            f"{self.new_articles} new articles, {self.new_filings} filings, "
            f"{self.new_stories} new stories, {self.calls} calls"
            + (f", {skipped} older than a week skipped" if skipped else "")
            + (f", {len(self.errors)} errors" if self.errors else "")
            + (f" ({self.note})" if self.note else "")
        )


class _ScanProgress:
    """A scan's row, written as the scan goes: one short transaction per step, never held
    across a fetch or a call, so the page's status poll sees each step end."""

    def __init__(
        self, session_factory: sessionmaker[Session], scan_id: int, clock: Callable[[], datetime]
    ) -> None:
        self.session_factory = session_factory
        self.scan_id = scan_id
        self.clock = clock

    def step(self, key: str, state: str, detail: str = "") -> None:
        with self.session_factory() as session:
            scan = session.get(WatchScan, self.scan_id)
            if scan is not None:
                scan.steps = with_step(scan.steps, key, state, detail)
                scan.updated_at = self.clock()
                session.commit()

    def update(self, **fields: Any) -> None:
        with self.session_factory() as session:
            scan = session.get(WatchScan, self.scan_id)
            if scan is not None:
                for name, value in fields.items():
                    setattr(scan, name, value)
                scan.updated_at = self.clock()
                session.commit()

    def finish(self, status: str, outcome: _ScanOutcome) -> None:
        with self.session_factory() as session:
            scan = session.get(WatchScan, self.scan_id)
            if scan is None:
                return
            # A step a failure cut short reads as failed, not as still running.
            steps = scan.steps
            for step in steps:
                if step["state"] in ("running", "pending") and status == "failed":
                    steps = with_step(
                        steps, step["key"], "failed" if step["state"] == "running" else "skipped"
                    )
            scan.steps = steps
            scan.status = status
            scan.new_articles = outcome.new_articles
            scan.new_filings = outcome.new_filings
            scan.new_stories = outcome.new_stories
            scan.calls = outcome.calls
            scan.skipped_stale = sum(outcome.stale.values())
            scan.errors = outcome.errors
            scan.note = outcome.note
            scan.finished_at = scan.updated_at = self.clock()
            session.commit()


def _match_row(match: Match) -> WatchMatch:
    return WatchMatch(
        symbol=match.symbol, verdict=match.verdict, reason=match.reason, alias=match.alias
    )


def _filing_text(found: Announcement) -> str:
    return article_text(found.subject, found.description)


def _stored_filing_text(filing: WatchFiling) -> str:
    return article_text(filing.subject, filing.description)
