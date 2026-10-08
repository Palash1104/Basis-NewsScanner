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
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import FeedConfig, Settings, WatchlistFile
from app.llm.client import LLMClient
from app.models import (
    FeedCheck,
    WatchArticle,
    WatchFiling,
    WatchMatch,
    WatchPrice,
    WatchRun,
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
    away_summaries,
    deliver,
    detect_now,
    feed_warnings,
    followups,
    group_layout,
    mark_summarised,
    move_alerts,
    news_alerts,
    retry_pending,
)
from app.watch.analyse import analyse, read_replies
from app.watch.catchup import Gap, SourceCoverage, find_gap, possible_gaps
from app.watch.group import StoryGrouper
from app.watch.match import Match, Matcher
from app.watch.moves import Typical, typical_moves
from app.watch.power import keep_awake, on_ac_power
from app.watch.prices import (
    MINUTE_HISTORY,
    HistoryProvider,
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

# analyse comes after the news jobs, so what they just stored is called in the same tick;
# alerts come last, so a call or a price made this tick is alerted this tick.
JOBS = ("feeds", "google_news", "analyse", "prices", "alerts")
FEED_HEALTH_EVERY = timedelta(minutes=10)
TYPICAL_RETRY = timedelta(hours=1)  # after failing to fetch the bars thresholds come from
RETRY_AFTER_FAILURE = timedelta(minutes=1)  # after a pass in which every source failed
NSE_FEED_NAME = "NSE announcements"
# Two sightings are one article when the outlet is the same and the titles this close: a
# CNBC-TV18 story reached through its own RSS and through Google News.
SAME_ARTICLE_WINDOW = timedelta(hours=48)


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
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.watchlist = watchlist
        self.stocks = watchlist.stocks
        self.matcher = Matcher(self.stocks)
        self.feeds = list(feeds)
        self.queries = google_news_queries(self.stocks, settings)
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
        self._feed_health_at: datetime | None = None
        self._bse_read_at: datetime | None = None
        self._bse_error: str | None = None
        # Set by `newsdesk watch --since`: catch up from here on the next feed scan.
        self.force_since: datetime | None = None
        # The feeds whose health is tracked: not the one-off catch-up searches.
        self._checked = {feed.url for feed in [*self.feeds, *self.queries]}

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
        for job in JOBS:
            now = self.clock()
            if job == "prices" and not in_market_hours(now, self.settings):
                continue
            if not force and not self.due(job, now):
                continue
            self.last_run[job] = now
            result = self.run_job(job, now)
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
        shared = self._read_bse(gap, now)
        result = self._process("feeds", reads, nse, now, history, shared)
        if gap is not None:
            self._record_catch_up(gap, reads, searches, nse, history, result, now, shared)
        return result

    def _read_bse(self, gap: Gap | None, now: datetime) -> SharedRead | None:
        """New rows in the PEAD tool's shared file since the last read (with a minute's
        overlap), or since the gap's start after a gap, or the story window on a first read."""
        path = self.settings.watch.bse_announcements_db
        isins = {stock.isin: stock.symbol for stock in self.stocks if stock.isin}
        if not path or not isins:
            return None
        if gap is not None:
            since = gap.start
        elif self._bse_read_at is not None:
            since = self._bse_read_at - timedelta(minutes=1)
        else:
            since = now - timedelta(hours=self.settings.watch.story_window_hours)
        shared = read_bse_announcements(Path(path), isins, since)
        if shared.error is None:
            self._bse_read_at = now
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
            if self._feed_health_at is None or now - self._feed_health_at >= FEED_HEALTH_EVERY:
                self._feed_health_at = now
                outgoing += feed_warnings(session, self.settings, now)
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
            rows.append(
                WatchPrice(
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
            )
            result.prices += 1
        with self.session_factory() as session:
            session.add_all(rows)
            session.commit()
        return result

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
    ) -> JobResult:
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
                [text for _, _, _, text, _ in window]
                + [c.text for c in candidates]
                + [_filing_text(found) for _, found in fresh_filings]
            )

            session.add_all(checks)
            new_articles = self._store_articles(session, candidates, now)
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
            feed_name=feed.name,
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
        self, session: Session, candidates: Sequence[_Candidate], now: datetime
    ) -> list[WatchArticle]:
        created: list[WatchArticle] = []
        for candidate in candidates:
            entry = candidate.entry
            article = self._existing(session, entry, now)
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
        shared: SharedRead | None = None,
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
        if shared is not None:
            # The PEAD tool catches up on its own when it starts; BSE is covered only if it
            # has written since the gap began.
            alive = shared.newest_fetch is not None and shared.newest_fetch >= gap.start
            note = shared.error or (
                ""
                if alive
                else "the PEAD tool hasn't written since "
                + (
                    shared.newest_fetch.astimezone(self.settings.tz).strftime("%a %d %b %H:%M")
                    if shared.newest_fetch
                    else "it was set up"
                )
            )
            sources.append(
                SourceCoverage("bse", "BSE (via the PEAD tool)", gap.start if alive else None, note)
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

    def _window_members(
        self, session: Session, now: datetime
    ) -> list[tuple[datetime, int, set[str], str, str]]:
        """Every grouped article and filing still inside the story window, oldest first:
        (first seen, story id, stocks, text, kind)."""
        since = now - timedelta(hours=self.settings.watch.story_window_hours)
        members: list[tuple[datetime, int, set[str], str, str]] = []
        for article in session.scalars(
            select(WatchArticle).where(
                WatchArticle.story_id.is_not(None), WatchArticle.first_seen_at >= since
            )
        ):
            symbols = {m.symbol for m in article.matches}
            members.append(
                (
                    article.first_seen_at,
                    article.story_id,  # type: ignore[arg-type]
                    symbols,
                    article_text(article.title, article.snippet),
                    "article",
                )
            )
        for filing in session.scalars(
            select(WatchFiling).where(
                WatchFiling.story_id.is_not(None), WatchFiling.first_seen_at >= since
            )
        ):
            members.append(
                (
                    filing.first_seen_at,
                    filing.story_id,
                    {filing.symbol},
                    _stored_filing_text(filing),
                    "filing",
                )  # type: ignore[arg-type]
            )
        members.sort(key=lambda member: member[0])
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
        window: list[tuple[datetime, int, set[str], str, str]],
        articles: Sequence[WatchArticle],
        filings: Sequence[WatchFiling],
        now: datetime,
    ) -> int:
        """Place each new article and filing in a story; returns how many stories it made."""
        grouper: StoryGrouper[int] = StoryGrouper(
            self.settings.grouping.embedding_threshold,
            self.settings.grouping.seed_threshold,
            timedelta(hours=self.settings.watch.story_window_hours),
        )
        if self.embedder is not None:
            for seen, story_id, symbols, text, _ in window:
                grouper.add(story_id, self._vectors[text], symbols, seen)
        created = 0

        def place(text: str, symbols: set[str], headline: str, seen: datetime) -> WatchStory:
            nonlocal created
            story_id = (
                grouper.match(self._vectors[text], symbols, seen)
                if self.embedder is not None
                else None
            )
            story = session.get(WatchStory, story_id) if story_id is not None else None
            if story is None:
                story = WatchStory(first_seen_at=seen, headline=headline)
                session.add(story)
                session.flush()
                created += 1
            if self.embedder is not None:
                grouper.add(story.id, self._vectors[text], symbols, seen)
            return story

        for article in articles:
            text = article_text(article.title, article.snippet)
            article.story = place(
                text, {m.symbol for m in article.matches}, article.title, article.first_seen_at
            )
        for filing in filings:
            story = self._quoted_story(session, filing, now)
            if story is None:
                story = place(
                    _stored_filing_text(filing),
                    {filing.symbol},
                    f"{filing.company}: {filing.subject}",
                    filing.first_seen_at,
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
            return None
        quoted = normalize_title(filing.quoted_headline)
        since = now - timedelta(hours=self.settings.watch.story_window_hours)
        threshold = self.settings.dedupe.syndication_title_similarity
        for article in session.scalars(
            select(WatchArticle)
            .join(WatchMatch)
            .where(
                WatchMatch.symbol == filing.symbol,
                WatchArticle.first_seen_at >= since,
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
            )
            .order_by(WatchFiling.filed_at)
        )
        return earlier.story if earlier is not None else None


WATCH_FEEDS = "watch_feeds.yaml"
TICK_SECONDS = 15  # short, so a laptop waking from sleep is noticed quickly


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
    """Run due jobs until stopped. A job's failure is logged and recorded, never fatal."""
    while not should_stop():
        try:
            # Held only while jobs run: a scan that starts finishes before the laptop sleeps.
            with keep_awake():
                for result in watcher.tick():
                    log.info("watch %s", result.line())
        except Exception:  # anything tick itself didn't catch: log it and keep going
            log.exception("watch tick failed")
        sleep(TICK_SECONDS)


def _match_row(match: Match) -> WatchMatch:
    return WatchMatch(
        symbol=match.symbol, verdict=match.verdict, reason=match.reason, alias=match.alias
    )


def _filing_text(found: Announcement) -> str:
    return article_text(found.subject, found.description)


def _stored_filing_text(filing: WatchFiling) -> str:
    return article_text(filing.subject, filing.description)
