"""URL normalization, duplicate removal, and counting independent sources."""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rapidfuzz import fuzz

from app.config import DedupeSettings

TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ocid",
        "cmpid",
        "smid",
        "ref",
        "ref_src",
        "s_cid",
        "ncid",
        "taid",
        "sr_share",
        "traffic_source",
        "guccounter",
        "guce_referrer",
        "guce_referrer_sig",
        "_ga",
    }
)
TRACKING_PREFIXES = ("utm_", "at_")  # at_* is AT Internet tracking (used by BBC)

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
# Google News sometimes labels outlets by domain: "Moneycontrol.com", "cnbc.com".
_DOMAIN_SUFFIX = re.compile(r"\.(com|co\.in|co\.uk|in|org|net)$")


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, drop tracking params, fragment and trailing slashes."""
    url = url.strip()
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        return url
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    default_port = {"http": 80, "https": 443}.get(scheme)
    if parts.port is not None and parts.port != default_port:
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(key)
    )
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def normalize_source(name: str) -> str:
    """Comparison key for outlet names: 'The Hindu' == 'hindu', 'cnbc.com' == 'CNBC'."""
    key = name.casefold().strip()
    key = key.removeprefix("the ").removeprefix("www.")
    key = _DOMAIN_SUFFIX.sub("", key)
    return _NON_WORD.sub("", key)


def normalize_title(text: str) -> str:
    """Casefold, replace punctuation with spaces, collapse whitespace."""
    return " ".join(_NON_WORD.sub(" ", text.casefold()).split())


class ArticleLike(Protocol):
    url: str
    source_name: str
    title: str
    published_at: datetime


DropReason = Literal["duplicate_url", "same_source_similar_title"]


@dataclass(frozen=True)
class DroppedArticle[T]:
    article: T
    reason: DropReason
    matched_title: str | None = None


def dedupe_articles[T: ArticleLike](
    candidates: Sequence[T],
    existing: Sequence[ArticleLike],
    settings: DedupeSettings,
) -> tuple[list[T], list[DroppedArticle[T]]]:
    """Drop candidates whose URL is already known, or whose title closely matches another
    article from the same source within the dedupe window.

    `existing` are articles already stored. Candidates are processed oldest first, so the
    earliest copy wins.
    """
    window = timedelta(hours=settings.same_source_window_hours)
    seen_urls = {article.url for article in existing}
    by_source: dict[str, list[tuple[str, datetime, str]]] = defaultdict(list)
    for article in existing:
        by_source[normalize_source(article.source_name)].append(
            (normalize_title(article.title), article.published_at, article.title)
        )

    kept: list[T] = []
    dropped: list[DroppedArticle[T]] = []
    for article in sorted(candidates, key=lambda item: item.published_at):
        if article.url in seen_urls:
            dropped.append(DroppedArticle(article, "duplicate_url"))
            continue
        source_key = normalize_source(article.source_name)
        title_key = normalize_title(article.title)
        match = next(
            (
                other_title
                for other_key, other_time, other_title in by_source[source_key]
                if abs(article.published_at - other_time) <= window
                and fuzz.token_set_ratio(title_key, other_key)
                >= settings.same_source_title_similarity
            ),
            None,
        )
        if match is not None:
            dropped.append(DroppedArticle(article, "same_source_similar_title", match))
            continue
        kept.append(article)
        seen_urls.add(article.url)
        by_source[source_key].append((title_key, article.published_at, article.title))
    return kept, dropped


def count_independent_sources(articles: Sequence[ArticleLike], similarity: float) -> int:
    """Count distinct sources, treating near-identical titles from different sources
    (syndicated wire copy) as a single source.

    Near-identical uses token_sort_ratio (same words, any order), not token_set_ratio,
    which would also match a short headline contained in a longer, different one.
    Non-news articles (explainers, roundups) never count as a source.
    """
    news = [item for item in articles if not getattr(item, "non_news", False)]
    items = sorted(news, key=lambda item: item.published_at)
    sources = [normalize_source(item.source_name) for item in items]
    titles = [normalize_title(item.title) for item in items]

    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if (
                sources[i] != sources[j]
                and fuzz.token_sort_ratio(titles[i], titles[j]) >= similarity
            ):
                parent[find(j)] = find(i)

    clusters: dict[int, list[int]] = defaultdict(list)
    for index in range(len(items)):
        clusters[find(index)].append(index)

    counted: set[str] = set()
    syndicated: list[list[int]] = []
    for members in clusters.values():
        member_sources = {sources[i] for i in members}
        if len(member_sources) == 1:
            counted |= member_sources
        else:
            syndicated.append(members)
    # Each syndicated cluster adds at most one source, and only if some outlet in it has no
    # reporting of its own in this story (otherwise every outlet in it is already counted).
    total = len(counted)
    for members in sorted(syndicated, key=min):
        member_sources = {sources[i] for i in members}
        if member_sources - counted:
            total += 1
            counted |= member_sources
    return total
