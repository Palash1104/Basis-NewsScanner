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
from datetime import datetime, timedelta
from typing import Any

import httpx
import numpy as np
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import FeedConfig, Settings, WatchlistFile
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
from app.watch.group import StoryGrouper
from app.watch.match import Match, Matcher
from app.watch.power import keep_awake, on_ac_power
from app.watch.prices import SnapshotProvider, in_market_hours, price_symbols
from app.watch.sources import (
    Announcement,
    FeedRead,
    Fetched,
    Validators,
    conditional_get,
    google_news_queries,
    parse_nse_announcements,
    read_feed,
    watched_announcements,
)

log = logging.getLogger(__name__)

JOBS = ("feeds", "google_news", "prices")
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
    errors: list[dict[str, Any]] = field(default_factory=list)

    def line(self) -> str:
        if self.job == "prices":
            text = f"prices: {self.prices} symbols polled"
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
            results.append(result)
            every = self.interval(job, now)
            if job != "prices" and result.entries == 0 and result.errors and every:
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
            else:
                result = self.poll_prices(now)
        except Exception as exc:  # the loop must outlive any one bad pass
            log.exception("watch job %s failed", job)
            result = JobResult(
                job, errors=[{"stage": job, "error": f"{type(exc).__name__}: {exc}"}]
            )
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
        reads, nse = asyncio.run(self._fetch(self.feeds, include_nse=True, now=now))
        return self._process("feeds", reads, nse, now)

    def scan_google_news(self, now: datetime) -> JobResult:
        reads, _ = asyncio.run(self._fetch(self.queries, include_nse=False, now=now))
        return self._process("google_news", reads, None, now)

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
        self, job: str, reads: list[FeedRead], nse: Fetched | None, now: datetime
    ) -> JobResult:
        result = JobResult(job)
        checks = [self._feed_check(read.feed, read.fetched, read.articles, now) for read in reads]
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
                    exchange="NSE",
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
        keys = [found.key for _, found in announcements]
        if not keys:
            return []
        known = set(session.scalars(select(WatchFiling.key).where(WatchFiling.key.in_(keys))))
        fresh: dict[str, tuple[str, Announcement]] = {}
        for symbol, found in announcements:
            if found.key not in known:
                fresh.setdefault(found.key, (symbol, found))
        # Oldest first, so a story's seed is the earliest filing.
        return sorted(fresh.values(), key=lambda pair: pair[1].filed_at)

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
    )


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
