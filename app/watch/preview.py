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
    url: str | None = None


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

    def judge(
        title: str,
        snippet: str,
        source: str,
        published: datetime,
        where: str,
        url: str | None = None,
    ) -> None:
        key = " ".join(title.lower().split())
        if key in seen:
            return
        for match in matcher.match(title, snippet):
            seen.add(key)
            result.counts[match.verdict] += 1
            result.rows.append(
                PreviewRow(
                    title, source, published, match.verdict, match.reason, match.alias, where, url
                )
            )

    since = now - timedelta(days=DAYS)
    for title, snippet, source, published, url in session.execute(
        select(
            Article.title, Article.snippet, Article.source_name, Article.published_at, Article.url
        ).where(Article.published_at >= since)
    ):
        judge(title, snippet or "", source, published, "stored", url)
    for title, snippet, source, published, url in session.execute(
        select(
            WatchArticle.title,
            WatchArticle.snippet,
            WatchArticle.source_name,
            WatchArticle.published_at,
            WatchArticle.url,
        ).where(WatchArticle.published_at >= since)
    ):
        judge(title, snippet or "", source, published, "stored", url)

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
                judge(
                    article.title,
                    "",
                    article.source_name,
                    article.published_at,
                    "Google News",
                    article.url,
                )
        except Exception as exc:
            result.search_error = f"{type(exc).__name__}: {exc}"
        result.ticker = check_tickers(tickers or [item.symbol], now, fetch)
    return result


# ---------------------------------------------------------------- the company, from Yahoo
# What the preview shows about a company beyond its headlines (user's design, 2026-10-17): a
# year of daily closes for the price, its range and the charts, and Yahoo's own profile -
# sector, industry, market cap, what the company does. Asked only once the ticker check has
# passed, and never needed: a profile Yahoo won't give leaves those fields empty.

CRORE = 10_000_000


@dataclass
class Profile:
    closes: list[tuple[datetime, float]] = field(default_factory=list)  # a year, oldest first
    currency: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None
    summary: str | None = None
    error: str | None = None


def yahoo_profile(timeout: float) -> Callable[[str], Profile]:
    """A year of daily closes, then Yahoo's profile (`Ticker.info`), each asked on its own so
    one failing leaves the other."""
    import yfinance as yf

    from app.assets import _metadata

    def fetch(symbol: str) -> Profile:
        ticker = yf.Ticker(symbol)
        profile = Profile()
        try:
            frame = ticker.history(period="1y", interval="1d", auto_adjust=False, timeout=timeout)
            if "Close" in frame:
                profile.closes = [
                    (stamp.to_pydatetime(), float(close))
                    for stamp, close in frame["Close"].dropna().items()
                ]
            profile.currency = _metadata(ticker.get_history_metadata()).get("currency")
        except Exception as exc:
            profile.error = f"prices: {type(exc).__name__}: {exc}"
        try:
            info = ticker.info or {}
            profile.sector = info.get("sector") or None
            profile.industry = info.get("industry") or None
            cap = info.get("marketCap")
            profile.market_cap = float(cap) if isinstance(cap, int | float) and cap > 0 else None
            profile.summary = info.get("longBusinessSummary") or None
            profile.currency = profile.currency or info.get("currency")
        except Exception as exc:
            profile.error = f"profile: {type(exc).__name__}: {exc}"
        return profile

    return fetch


@dataclass(frozen=True)
class Facts:
    """A profile as the preview shows it: every figure already written out."""

    price: str | None = None
    change: str | None = None
    up: bool = True
    low: str | None = None
    high: str | None = None
    market_cap: str | None = None
    sector: str | None = None
    industry: str | None = None
    summary: str | None = None
    overview: list[float] = field(default_factory=list)  # three months of closes
    history: list[float] = field(default_factory=list)  # the year
    returns: list[tuple[str, str, bool]] = field(default_factory=list)  # (label, move, up)
    first_day: datetime | None = None
    last_day: datetime | None = None


def money(value: float, currency: str | None) -> str:
    figure = f"{value:,.0f}" if abs(value) >= 10_000 else f"{value:,.2f}"
    if currency == "INR":
        return f"₹{figure}"
    return f"{figure} {currency}" if currency else figure


def market_cap(value: float | None, currency: str | None) -> str | None:
    if value is None:
        return None
    if currency == "INR":
        return f"₹{value / CRORE:,.0f} Cr"
    return f"{value / 1e9:,.1f}B {currency or ''}".strip()


def facts(profile: Profile | None, summary_chars: int = 320) -> Facts:
    if profile is None:
        return Facts()
    closes = profile.closes
    currency = profile.currency
    price = change = low = high = None
    up = True
    returns: list[tuple[str, str, bool]] = []
    if closes:
        last_at, last = closes[-1]
        price = money(last, currency)
        if len(closes) > 1:
            previous = closes[-2][1]
            move = last / previous - 1
            change, up = f"{move:+.1%}", move >= 0
        values = [close for _, close in closes]
        low, high = money(min(values), currency), money(max(values), currency)
        for label, days in (("1 week", 7), ("1 month", 30), ("3 months", 91), ("1 year", 365)):
            then = [close for at, close in closes if at <= last_at - timedelta(days=days)]
            if then:
                move = last / then[-1] - 1
                returns.append((label, f"{move:+.1%}", move >= 0))
    summary = profile.summary
    if summary and len(summary) > summary_chars:
        summary = summary[:summary_chars].rsplit(" ", 1)[0].rstrip(",;") + "…"
    recent = [c for at, c in closes if closes and at >= closes[-1][0] - timedelta(days=91)]
    return Facts(
        price=price,
        change=change,
        up=up,
        low=low,
        high=high,
        market_cap=market_cap(profile.market_cap, currency),
        sector=profile.sector,
        industry=profile.industry,
        summary=summary,
        overview=recent,
        history=[close for _, close in closes],
        returns=returns,
        first_day=closes[0][0] if closes else None,
        last_day=closes[-1][0] if closes else None,
    )


def chart_points(values: Sequence[float], width: int, height: int, pad: int = 4) -> str:
    """An SVG polyline's points for `values`, left to right, the highest at the top."""
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    step = (width - 2 * pad) / (len(values) - 1)
    return " ".join(
        f"{pad + i * step:.1f},{pad + (high - v) / span * (height - 2 * pad):.1f}"
        for i, v in enumerate(values)
    )
