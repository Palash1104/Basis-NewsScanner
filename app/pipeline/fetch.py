"""Fetch RSS/Atom feeds concurrently and parse entries into FetchedArticle records."""

import asyncio
import html
import io
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

import feedparser
import httpx

from app.config import FeedConfig, Region, Settings
from app.models import utcnow
from app.net import Sleep, make_client, request_with_retries
from app.pipeline.dedupe import normalize_source, normalize_url

log = logging.getLogger(__name__)

# Feeds sometimes mislabel local times as UTC, which puts entries in the future.
FUTURE_TOLERANCE = timedelta(minutes=10)

_BLOCK_TAGS = frozenset(
    {
        "p",
        "br",
        "div",
        "li",
        "ul",
        "ol",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "figure",
        "figcaption",
        "img",
        "table",
        "section",
        "article",
    }
)
_SKIP_TAGS = frozenset({"script", "style"})
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class FetchedArticle:
    url: str  # normalized
    source_name: str
    source_region: Region
    source_weight: int
    title: str
    snippet: str
    published_at: datetime
    fetched_at: datetime
    feed_url: str  # which feed produced it; not stored


@dataclass
class FeedResult:
    feed: FeedConfig
    articles: list[FetchedArticle] = field(default_factory=list)
    status_code: int | None = None
    error: str | None = None
    elapsed_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


class FeedParseError(Exception):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    """Strip tags and entities and collapse whitespace."""
    if "<" in value or "&" in value:
        parser = _TextExtractor()
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
        if "&" in value:  # some feeds double-escape ("S&amp;amp;P")
            value = html.unescape(value)
    return _WHITESPACE.sub(" ", value).strip()


def truncate(text: str, max_chars: int) -> str:
    """Cut to at most `max_chars`, preferring a word boundary, adding an ellipsis."""
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1]
    boundary = cut.rfind(" ")
    if boundary >= max_chars * 0.7:
        cut = cut[:boundary]
    return cut.rstrip(" ,;:-") + "…"


class SourceResolver:
    """Maps outlet names (and aliases) to the configured outlet name, region and weight.

    Used for aggregator entries (Google News) whose outlet comes from the entry itself.
    A Google News search feed for one outlet (e.g. site:reuters.com) is named after that
    outlet, so its name and aliases count here too.
    """

    def __init__(self, feeds: Sequence[FeedConfig]) -> None:
        self._outlets: dict[str, tuple[str, Region, int]] = {}
        for feed in feeds:
            for name in (feed.name, *feed.aliases):
                key = normalize_source(name)
                current = self._outlets.get(key)
                if current is None or feed.weight > current[2]:
                    self._outlets[key] = (feed.name, feed.region, feed.weight)

    def resolve(self, outlet: str, default_region: Region) -> tuple[str, Region, int]:
        known = self._outlets.get(normalize_source(outlet))
        return known if known is not None else (outlet.strip(), default_region, 1)

    def is_known(self, outlet: str) -> bool:
        return normalize_source(outlet) in self._outlets


def _entry_published(entry: Any, fetched_at: datetime) -> datetime:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if not value:
            continue
        try:
            published = datetime(*value[:6], tzinfo=UTC)  # feedparser normalizes to UTC
        except (TypeError, ValueError):
            continue
        return fetched_at if published > fetched_at + FUTURE_TOLERANCE else published
    return fetched_at


def _strip_outlet_suffix(title: str, outlet: str) -> str:
    """Google News titles end with ' - Outlet', sometimes with extra words after the outlet
    name ('- GSMArena.com news'), and outlet names can themselves contain ' - '."""
    suffix = f" - {outlet}"
    if title.casefold().endswith(suffix.casefold()):
        return title[: -len(suffix)].strip()
    head, sep, tail = title.rpartition(" - ")
    outlet_key = normalize_source(outlet)
    if sep and outlet_key and normalize_source(tail).startswith(outlet_key):
        return head.strip()
    return title


def parse_feed(
    content: bytes,
    feed: FeedConfig,
    fetched_at: datetime,
    resolver: SourceResolver,
    snippet_max_chars: int,
    content_type: str | None = None,
) -> list[FetchedArticle]:
    headers = {"content-type": content_type} if content_type else {}
    parsed = feedparser.parse(io.BytesIO(content), response_headers=headers)
    if not parsed.entries and parsed.get("bozo"):
        raise FeedParseError(str(parsed.get("bozo_exception") or "unparseable feed"))

    articles: list[FetchedArticle] = []
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        title = html_to_text(entry.get("title") or "")
        if not link or not title:
            continue

        source_name, region, weight = feed.name, feed.region, feed.weight
        if feed.is_google_news:
            outlet = html_to_text((entry.get("source") or {}).get("title") or "")
            if not outlet:
                continue
            source_name, region, weight = resolver.resolve(outlet, feed.region)
            title = _strip_outlet_suffix(title, outlet)
            # Google News descriptions are lists of related headlines, not a snippet.
            snippet = ""
        else:
            raw = entry.get("summary") or ""
            if not raw and entry.get("content"):
                raw = entry["content"][0].get("value") or ""
            snippet = truncate(html_to_text(raw), snippet_max_chars)

        articles.append(
            FetchedArticle(
                url=normalize_url(link),
                source_name=source_name,
                source_region=region,
                source_weight=weight,
                title=title,
                snippet=snippet,
                published_at=_entry_published(entry, fetched_at),
                fetched_at=fetched_at,
                feed_url=feed.url,
            )
        )
    return articles


async def fetch_feed(
    client: httpx.AsyncClient,
    feed: FeedConfig,
    settings: Settings,
    resolver: SourceResolver,
    sleep: Sleep = asyncio.sleep,
) -> FeedResult:
    """Fetch and parse one feed. Never raises: failures are returned in FeedResult.error."""
    started = time.monotonic()
    result = FeedResult(feed=feed)
    try:
        response = await request_with_retries(
            client,
            "GET",
            feed.url,
            max_attempts=settings.http.max_attempts,
            backoff_base=settings.http.backoff_base_seconds,
            sleep=sleep,
        )
        result.status_code = response.status_code
        if not response.is_success:
            result.error = f"HTTP {response.status_code}"
        else:
            result.articles = parse_feed(
                response.content,
                feed,
                utcnow(),
                resolver,
                settings.pipeline.snippet_max_chars,
                response.headers.get("content-type"),
            )
    except Exception as exc:  # one broken feed must never crash a run
        result.error = f"{type(exc).__name__}: {exc}"
    result.elapsed_seconds = time.monotonic() - started
    if result.ok:
        log.info("feed %s (%s): %d entries", feed.name, feed.url, len(result.articles))
    else:
        log.warning("feed %s (%s) failed: %s", feed.name, feed.url, result.error)
    return result


async def fetch_all(
    feeds: Sequence[FeedConfig],
    settings: Settings,
    resolver: SourceResolver | None = None,
    client: httpx.AsyncClient | None = None,
    sleep: Sleep = asyncio.sleep,
) -> list[FeedResult]:
    """Fetch every feed concurrently."""
    resolver = resolver or SourceResolver(feeds)
    if client is not None:
        return list(
            await asyncio.gather(*(fetch_feed(client, f, settings, resolver, sleep) for f in feeds))
        )
    async with make_client(settings.http) as owned:
        return list(
            await asyncio.gather(*(fetch_feed(owned, f, settings, resolver, sleep) for f in feeds))
        )


def filter_recent(
    articles: Sequence[FetchedArticle], lookback_hours: float, now: datetime
) -> list[FetchedArticle]:
    cutoff = now - timedelta(hours=lookback_hours)
    return [article for article in articles if article.published_at >= cutoff]
