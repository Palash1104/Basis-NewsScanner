from datetime import timedelta

import pytest

from app.config import DedupeSettings
from app.pipeline.dedupe import (
    count_independent_sources,
    dedupe_articles,
    normalize_source,
    normalize_title,
    normalize_url,
)
from app.pipeline.fetch import FetchedArticle
from tests.conftest import NOW


def make_article(
    title: str,
    source: str = "Outlet A",
    url: str | None = None,
    hours_ago: float = 0,
) -> FetchedArticle:
    return FetchedArticle(
        url=url or f"https://example.com/{normalize_title(title).replace(' ', '-')}/{source}",
        source_name=source,
        source_region="GLOBAL",
        source_weight=2,
        title=title,
        snippet="",
        published_at=NOW - timedelta(hours=hours_ago),
        fetched_at=NOW,
        feed_url="https://example.com/rss",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://WWW.Example.COM/a/b/", "https://www.example.com/a/b"),
        ("https://example.com/a?utm_source=x&utm_campaign=y", "https://example.com/a"),
        ("https://example.com/a?b=2&a=1&fbclid=z", "https://example.com/a?a=1&b=2"),
        ("https://example.com/a?at_medium=RSS&at_campaign=rss", "https://example.com/a"),
        ("https://example.com/a?traffic_source=rss#section", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:8080/a/", "http://example.com:8080/a"),
        ("https://example.com/", "https://example.com"),
        ("https://example.com/CaseSensitive/Path", "https://example.com/CaseSensitive/Path"),
        ("not a url", "not a url"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_source() -> None:
    assert normalize_source("The Hindu") == normalize_source("hindu")
    assert normalize_source("POLITICO") == normalize_source("Politico")
    assert normalize_source("cnbc.com") == normalize_source("CNBC")
    assert normalize_source("Moneycontrol.com") == normalize_source("Moneycontrol")
    assert normalize_source("The New York Times") != normalize_source("The Times of India")


def test_duplicate_url_dropped(dedupe_settings: DedupeSettings) -> None:
    existing = [make_article("Old headline", url="https://example.com/story")]
    candidate = make_article("Totally different headline", url="https://example.com/story")
    kept, dropped = dedupe_articles([candidate], existing, dedupe_settings)
    assert kept == []
    assert dropped[0].reason == "duplicate_url"


def test_same_source_similar_title_dropped_within_window(dedupe_settings: DedupeSettings) -> None:
    first = make_article("Union Cabinet approves raising EPFO wage ceiling", hours_ago=3)
    repeat = make_article(
        "Union Cabinet approves raising EPFO wage ceiling", url="https://example.com/v2"
    )
    kept, dropped = dedupe_articles([repeat, first], [], dedupe_settings)
    assert kept == [first]  # earliest copy wins
    assert dropped[0].article == repeat
    assert dropped[0].reason == "same_source_similar_title"


def test_same_title_outside_window_kept(dedupe_settings: DedupeSettings) -> None:
    old = make_article("Markets open higher", hours_ago=30)
    new = make_article("Markets open higher", url="https://example.com/today")
    kept, _ = dedupe_articles([new], [old], dedupe_settings)
    assert kept == [new]


def test_different_sources_with_same_title_both_kept(dedupe_settings: DedupeSettings) -> None:
    a = make_article("Cabinet approves new wage ceiling", source="Outlet A")
    b = make_article("Cabinet approves new wage ceiling", source="Outlet B")
    kept, dropped = dedupe_articles([a, b], [], dedupe_settings)
    assert len(kept) == 2 and dropped == []


def test_google_news_copy_of_known_outlet_counts_as_same_source(
    dedupe_settings: DedupeSettings,
) -> None:
    direct = make_article("PM inaugurates new airport terminal", source="The Hindu", hours_ago=1)
    via_google = make_article(
        "PM inaugurates new airport terminal",
        source="Hindu",
        url="https://news.google.com/rss/articles/X",
    )
    kept, dropped = dedupe_articles([via_google], [direct], dedupe_settings)
    assert kept == [] and dropped[0].reason == "same_source_similar_title"


def test_independent_sources_collapse_syndicated_copy() -> None:
    articles = [
        make_article("Storm makes landfall on east coast", source="Wire A", hours_ago=3),
        make_article("Storm makes landfall on east coast", source="Paper B", hours_ago=2),
        make_article("Storm makes landfall on east coast", source="Paper C", hours_ago=1),
        make_article("Thousands evacuated as cyclone batters coastal towns", source="Paper D"),
    ]
    assert count_independent_sources(articles, 90) == 2


def test_independent_sources_keep_outlet_with_its_own_reporting() -> None:
    articles = [
        make_article("Storm makes landfall on east coast", source="Wire A", hours_ago=3),
        make_article("Storm makes landfall on east coast", source="Paper B", hours_ago=2),
        make_article(
            "Inside the shelters: families describe the night the storm hit", source="Paper B"
        ),
    ]
    assert count_independent_sources(articles, 90) == 2


def test_independent_sources_short_headline_inside_longer_one_is_not_syndication() -> None:
    articles = [
        make_article("Fed raises rates", source="Wire A"),
        make_article(
            "Fed raises rates for first time in three years as inflation stays sticky",
            source="Paper B",
        ),
    ]
    assert count_independent_sources(articles, 90) == 2
