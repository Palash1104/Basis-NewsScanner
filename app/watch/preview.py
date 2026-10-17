"""What a stock's aliases would catch, before it is added to the watchlist (user, 2026-10-16).

The "Add a stock" panel on /watchlist shows, for a draft entry:

* whether Yahoo has the ticker (one request, as `validate-tickers` makes), so an entry whose
  prices can never be read is not added;
* the matcher's verdict on real headlines that name it: everything BASIS has stored in the
  last 30 days (the main feeds' and the watchlist scan's), and one Google News search over
  the same 30 days, because a company not yet on the watchlist has rarely been stored.

These are the only requests the web app makes, and only when the user asks for a preview.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import FeedConfig, Settings, WatchItem
from app.models import Article, WatchArticle
from app.pipeline.fetch import SourceResolver, parse_feed
from app.watch.match import Matcher

log = logging.getLogger(__name__)

DAYS = 30
SHOWN = 40
ORDER = {"keep": 0, "mention": 1, "drop": 2}


@dataclass(frozen=True)
class PreviewRow:
    title: str
    source: str
    published: datetime
    verdict: str  # keep | mention | drop
    reason: str
    alias: str | None
    where: str  # stored | Google News


@dataclass
class TickerCheck:
    ok: bool
    detail: str
    symbol: str | None = None  # the ticker Yahoo answered to


@dataclass
class Preview:
    rows: list[PreviewRow] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: {"keep": 0, "mention": 0, "drop": 0})
    searched: int = 0  # headlines the Google News search returned
    search_error: str | None = None
    ticker: TickerCheck | None = None

    @property
    def shown(self) -> list[PreviewRow]:
        return sorted(self.rows, key=lambda r: (ORDER[r.verdict], -r.published.timestamp()))[:SHOWN]


def check_ticker(symbol: str, now: datetime, fetch: Callable | None = None) -> TickerCheck:
    """Whether Yahoo has recent daily prices for `symbol`. Never raises."""
    from app.assets import NoPriceData, yahoo_fetch

    try:
        sample = (fetch or yahoo_fetch(10))(symbol)
    except NoPriceData as exc:
        return TickerCheck(False, f"Yahoo has no prices for {symbol}: {exc}")
    except Exception as exc:
        return TickerCheck(False, f"Yahoo could not be asked ({type(exc).__name__}: {exc})")
    if not sample.closes:
        return TickerCheck(False, f"Yahoo has no daily prices for {symbol} in the last 5 days")
    at, close = sample.closes[-1]
    exchange = sample.metadata.get("exchangeName") or "?"
    currency = sample.metadata.get("currency") or ""
    age = now - at
    if age > timedelta(days=7):
        return TickerCheck(False, f"Yahoo's latest price for {symbol} is {age.days} days old")
    return TickerCheck(
        True, f"{symbol} on Yahoo: last close {close:,.2f} {currency} ({exchange})", symbol
    )


def check_tickers(
    symbols: Sequence[str], now: datetime, fetch: Callable | None = None
) -> TickerCheck:
    """The first of `symbols` Yahoo has recent prices for. A company on BSE can be under its
    scrip id or its numeric code there, so both are asked before giving up."""
    failures = []
    for symbol in symbols:
        found = check_ticker(symbol, now, fetch)
        if found.ok:
            return found
        failures.append(found.detail)
    if len(failures) == 1:
        return TickerCheck(False, failures[0])
    return TickerCheck(False, f"Yahoo has no recent prices under {' or '.join(symbols)}")


def search_url(item: WatchItem, settings: Settings, now: datetime) -> str:
    terms = [*item.aliases.strong, *item.aliases.weak]
    query = " OR ".join(f'"{term}"' for term in terms)
    window = f"after:{(now - timedelta(days=DAYS)).date().isoformat()}"
    return (
        f"https://news.google.com/rss/search?q={quote_plus(query)}+{quote_plus(window)}"
        f"&{settings.watch.google_news_edition}"
    )


def preview(
    item: WatchItem,
    session: Session,
    settings: Settings,
    now: datetime,
    live: bool = True,
    transport: httpx.BaseTransport | None = None,
    fetch: Callable | None = None,
    tickers: Sequence[str] | None = None,
) -> Preview:
    """The draft's verdicts on stored and searched headlines, and its Yahoo check."""
    result = Preview()
    matcher = Matcher([item])
    seen: set[str] = set()

    def judge(title: str, snippet: str, source: str, published: datetime, where: str) -> None:
        key = " ".join(title.lower().split())
        if key in seen:
            return
        for match in matcher.match(title, snippet):
            seen.add(key)
            result.counts[match.verdict] += 1
            result.rows.append(
                PreviewRow(
                    title, source, published, match.verdict, match.reason, match.alias, where
                )
            )

    since = now - timedelta(days=DAYS)
    for title, snippet, source, published in session.execute(
        select(Article.title, Article.snippet, Article.source_name, Article.published_at).where(
            Article.published_at >= since
        )
    ):
        judge(title, snippet or "", source, published, "stored")
    for title, snippet, source, published in session.execute(
        select(
            WatchArticle.title,
            WatchArticle.snippet,
            WatchArticle.source_name,
            WatchArticle.published_at,
        ).where(WatchArticle.published_at >= since)
    ):
        judge(title, snippet or "", source, published, "stored")

    if live:
        feed = FeedConfig(
            name="Google News", url=search_url(item, settings, now), region="IN", weight=1
        )
        try:
            with httpx.Client(
                timeout=settings.http.timeout_seconds,
                headers={"User-Agent": settings.http.user_agent},
                follow_redirects=True,
                transport=transport,
            ) as client:
                response = client.get(feed.url)
            response.raise_for_status()
            articles = parse_feed(
                response.content, feed, now, SourceResolver([]), settings.pipeline.snippet_max_chars
            )
            result.searched = len(articles)
            for article in articles:
                judge(article.title, "", article.source_name, article.published_at, "Google News")
        except Exception as exc:
            result.search_error = f"{type(exc).__name__}: {exc}"
        result.ticker = check_tickers(tickers or [item.symbol], now, fetch)
    return result
