"""Where the watchlist scanner reads from: the watch feeds, batched Google News searches, and
NSE's announcements feed.

Every fetch is a conditional GET where the server allows it. Measured 2026-10-08: the ET and
Business Standard feeds and NSE answer 304 when nothing has changed; CNBC-TV18, Livemint,
BusinessLine and Business Today always send the whole feed (35-218 KB).
"""

import hashlib
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import httpx

from app.config import FeedConfig, Settings, WatchItem
from app.net import request_with_retries
from app.pipeline.fetch import FetchedArticle, SourceResolver, html_to_text, parse_feed

CheckStatus = Literal["ok", "not_modified", "error"]
FilingKind = Literal["filing", "clarification_sought", "company_reply"]

# The exchange is in India, and its feed writes times as "07-Oct-2026 15:33:17", no zone.
NSE_TIMEZONE = ZoneInfo("Asia/Kolkata")
NSE_TIME_FORMAT = "%d-%b-%Y %H:%M:%S"
# "The Exchange has sought clarification from Prime Focus Limited with respect to recent news
# item captioned Prime Focus shares tank 8% after Income Tax raids at Mumbai offices:
# Exclusive.  The response from the Company is awaited."
_QUOTED = re.compile(r"news item captioned\s+(.+?)\.?\s+The response", re.I | re.S)


@dataclass
class Validators:
    """What a server said about its last answer, for the next conditional GET."""

    etag: str | None = None
    last_modified: str | None = None

    def headers(self) -> dict[str, str]:
        headers = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        return headers


@dataclass
class Fetched:
    """One conditional fetch. `content` is set only when the server sent a new answer."""

    url: str
    status: CheckStatus
    http_status: int | None = None
    content: bytes | None = None
    content_type: str | None = None
    error: str | None = None
    elapsed_ms: int = 0


async def conditional_get(
    client: httpx.AsyncClient,
    url: str,
    validators: dict[str, Validators],
    settings: Settings,
) -> Fetched:
    """GET `url`, sending what the server said last time. Never raises."""
    started = time.monotonic()
    known = validators.get(url, Validators())
    result = Fetched(url=url, status="error")
    try:
        response = await request_with_retries(
            client,
            "GET",
            url,
            max_attempts=settings.http.max_attempts,
            backoff_base=settings.http.backoff_base_seconds,
            headers=known.headers(),
        )
        result.http_status = response.status_code
        if response.status_code == 304:
            result.status = "not_modified"
        elif response.is_success:
            result.status = "ok"
            result.content = response.content
            result.content_type = response.headers.get("content-type")
            validators[url] = Validators(
                response.headers.get("etag"), response.headers.get("last-modified")
            )
        else:
            result.error = f"HTTP {response.status_code}"
    except Exception as exc:  # one broken source must never stop a scan
        result.error = f"{type(exc).__name__}: {exc}"
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


@dataclass
class FeedRead:
    feed: FeedConfig
    fetched: Fetched
    articles: list[FetchedArticle] = field(default_factory=list)


async def read_feed(
    client: httpx.AsyncClient,
    feed: FeedConfig,
    validators: dict[str, Validators],
    settings: Settings,
    resolver: SourceResolver,
    now: datetime,
) -> FeedRead:
    fetched = await conditional_get(client, feed.url, validators, settings)
    read = FeedRead(feed, fetched)
    if fetched.content is not None:
        try:
            read.articles = parse_feed(
                fetched.content,
                feed,
                now,
                resolver,
                settings.pipeline.snippet_max_chars,
                fetched.content_type,
            )
        except Exception as exc:  # a malformed feed is an error for this feed only
            fetched.status = "error"
            fetched.error = f"{type(exc).__name__}: {exc}"
            # Fetch it whole next time rather than trusting validators for a bad answer.
            validators.pop(feed.url, None)
    return read


GOOGLE_NEWS_CAP = 100  # Google News returns at most this many items per search


def google_news_window(since: datetime, now: datetime) -> str:
    """The search operator covering `since` to now. Verified 2026-10-08: `when:6h` returns
    only the last 6 hours and `after:/before:` dates reach back at least 10 days. Hours are
    exact; past three days the date form is used, which may start up to a day early."""
    hours = int((now - since).total_seconds() // 3600) + 1
    if hours <= 72:
        return f"when:{hours}h"
    return f"after:{(since - timedelta(days=1)).date().isoformat()}"


def google_news_queries(
    items: Sequence[WatchItem], settings: Settings, window: str = "when:1d"
) -> list[FeedConfig]:
    """Every stock alias, quoted, OR-ed together a few at a time, over `window` (the last
    day; a catch-up passes the missed period).

    Verified 2026-10-07: an OR query of five names returns results for all five (72 items),
    so the whole watchlist costs a handful of requests. The matcher then judges each
    headline exactly as it judges the feeds' - a search hit is not a keep.
    """
    terms: dict[str, None] = {}
    for item in items:
        if item.type == "stock":
            for alias in (*item.aliases.strong, *item.aliases.weak):
                terms.setdefault(alias, None)
    size = settings.watch.google_news_terms_per_query
    ordered = list(terms)
    queries = []
    for start in range(0, len(ordered), size):
        chunk = ordered[start : start + size]
        query = " OR ".join(f'"{term}"' for term in chunk)
        queries.append(
            FeedConfig(
                name="Google News",
                url=f"https://news.google.com/rss/search?q={quote_plus(query)}+"
                f"{quote_plus(window)}&{settings.watch.google_news_edition}",
                region="IN",
                weight=1,
            )
        )
    return queries


# ---------------------------------------------------------------- NSE announcements


@dataclass(frozen=True)
class Announcement:
    company: str
    subject: str
    description: str
    link: str
    filed_at: datetime  # UTC
    kind: FilingKind
    quoted_headline: str | None

    @property
    def key(self) -> str:
        """The feed has no ids; the same announcement always hashes the same."""
        raw = f"NSE|{self.company}|{self.filed_at.isoformat()}|{self.subject}|{self.description}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def company_key(name: str) -> str:
    """'Data Patterns (India) Limited' and 'DATA PATTERNS (INDIA) LTD' compare equal."""
    key = name.casefold().replace("&", " and ")
    key = re.sub(r"\blimited\b", "ltd", re.sub(r"[^\w\s]", " ", key))
    return " ".join(key.split())


def parse_nse_announcements(content: bytes) -> list[Announcement]:
    """NSE's Online_announcements.xml: one item per announcement, the title the company's
    name (no symbol), the description ending "|SUBJECT: <subject>"."""
    parsed = feedparser.parse(content)
    found = []
    for entry in parsed.entries:
        company = html_to_text(entry.get("title") or "")
        raw = html_to_text(entry.get("description") or entry.get("summary") or "")
        stamp = (entry.get("published") or "").strip()
        if not company or not stamp:
            continue
        try:
            filed = datetime.strptime(stamp, NSE_TIME_FORMAT).replace(tzinfo=NSE_TIMEZONE)
        except ValueError:
            continue
        description, _, subject = raw.partition("|SUBJECT:")
        found.append(
            _announcement(
                company,
                subject.strip(),
                description.strip(),
                (entry.get("link") or "").strip(),
                filed,
            )
        )
    return found


def _announcement(
    company: str, subject: str, description: str, link: str, filed: datetime
) -> Announcement:
    quoted = _QUOTED.search(description)
    kind: FilingKind = "filing"
    if subject.casefold() == "news verification":
        kind = "company_reply" if "is attached" in description else "clarification_sought"
    return Announcement(
        company=company,
        subject=subject or "(no subject)",
        description=description,
        link=link,
        filed_at=filed.astimezone(UTC),
        kind=kind,
        quoted_headline=quoted.group(1).strip() if quoted else None,
    )


# NSE's announcements API, per company and date range. Verified 2026-10-08 with no cookies:
# PFOCUS over the last 30 days returned 22 rows back to 08 Sep. Its fields match the RSS:
# `an_dt` is the RSS pubDate, `desc` the subject, `attchmntText` the description. The RSS
# holds only today, so a catch-up over earlier days reads this instead.
NSE_API_URL = (
    "https://www.nseindia.com/api/corporate-announcements"
    "?index=equities&symbol={symbol}&from_date={from_date}&to_date={to_date}"
)
NSE_API_HEADERS = {
    # NSE answers its API to a browser-like request; the Referer is the page that calls it.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
    "Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}


def parse_nse_api(rows: Sequence[dict]) -> list[Announcement]:
    found = []
    for row in rows:
        stamp = str(row.get("an_dt") or "").strip()
        try:
            filed = datetime.strptime(stamp, NSE_TIME_FORMAT).replace(tzinfo=NSE_TIMEZONE)
        except ValueError:
            continue
        link = str(row.get("attchmntFile") or "")
        found.append(
            _announcement(
                html_to_text(str(row.get("sm_name") or "")),
                html_to_text(str(row.get("desc") or "")).strip(),
                html_to_text(str(row.get("attchmntText") or "")).strip(),
                link if link.startswith("http") else "",
                filed,
            )
        )
    return found


async def fetch_nse_history(
    client: httpx.AsyncClient,
    symbol: str,
    since: datetime,
    now: datetime,
    settings: Settings,
) -> tuple[list[Announcement], str | None]:
    """One company's announcements from `since`'s date to today; (found, error)."""
    url = NSE_API_URL.format(
        symbol=quote_plus(symbol),
        from_date=since.astimezone(NSE_TIMEZONE).strftime("%d-%m-%Y"),
        to_date=now.astimezone(NSE_TIMEZONE).strftime("%d-%m-%Y"),
    )
    try:
        response = await request_with_retries(
            client,
            "GET",
            url,
            max_attempts=settings.http.max_attempts,
            backoff_base=settings.http.backoff_base_seconds,
            headers=NSE_API_HEADERS,
        )
        if not response.is_success:
            return [], f"HTTP {response.status_code}"
        body = response.json()
        rows = body if isinstance(body, list) else (body or {}).get("data") or []
        return parse_nse_api(rows), None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def watched_announcements(
    announcements: Sequence[Announcement], items: Sequence[WatchItem]
) -> list[tuple[WatchItem, Announcement]]:
    """The announcements made by watchlist companies, matched on NSE's own company name."""
    by_name = {company_key(item.nse_name): item for item in items if item.nse_name}
    return [
        (item, found)
        for found in announcements
        if (item := by_name.get(company_key(found.company))) is not None
    ]
