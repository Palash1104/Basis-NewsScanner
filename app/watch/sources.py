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
from datetime import UTC, datetime
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


def google_news_queries(items: Sequence[WatchItem], settings: Settings) -> list[FeedConfig]:
    """Every stock alias, quoted, OR-ed together a few at a time, over the last day.

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
                url=f"https://news.google.com/rss/search?q={quote_plus(query)}+when:1d"
                f"&{settings.watch.google_news_edition}",
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
        description, subject = description.strip(), subject.strip()
        quoted = _QUOTED.search(description)
        kind: FilingKind = "filing"
        if subject.casefold() == "news verification":
            kind = "company_reply" if "is attached" in description else "clarification_sought"
        found.append(
            Announcement(
                company=company,
                subject=subject or "(no subject)",
                description=description,
                link=(entry.get("link") or "").strip(),
                filed_at=filed.astimezone(UTC),
                kind=kind,
                quoted_headline=quoted.group(1).strip() if quoted else None,
            )
        )
    return found


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
