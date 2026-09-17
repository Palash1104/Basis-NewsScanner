import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from app.config import Settings
from app.pipeline.fetch import (
    FetchedArticle,
    SourceResolver,
    fetch_all,
    filter_recent,
    html_to_text,
    parse_feed,
    truncate,
)
from tests.conftest import NOW, make_feed, read_fixture


def _parse_basic() -> dict[str, FetchedArticle]:
    feed = make_feed()
    articles = parse_feed(read_fixture("rss_basic.xml"), feed, NOW, SourceResolver([feed]), 500)
    return {article.title: article for article in articles}


def test_html_to_text_strips_tags_scripts_and_double_escaping() -> None:
    assert html_to_text("<p>One</p><p>Two &amp; three</p><script>x()</script>") == "One Two & three"
    assert html_to_text("S&amp;amp;P 500") == "S&P 500"
    assert html_to_text("  plain\n\ttext  ") == "plain text"


def test_truncate_prefers_word_boundary() -> None:
    text = "alpha beta gamma delta epsilon"
    result = truncate(text, 20)
    assert len(result) <= 20
    assert result == "alpha beta gamma…"
    assert truncate("short", 20) == "short"


def test_parse_basic_feed_fields() -> None:
    articles = _parse_basic()
    plan = articles["Central bank & treasury announce plan"]
    assert plan.url == "https://www.example.com/news/plan?id=7"
    assert plan.snippet == "The central bank said on Tuesday it would act. Markets rose."
    assert plan.published_at == datetime(2026, 9, 16, 10, 0, tzinfo=UTC)  # +0530 -> UTC
    assert plan.source_name == "Example Outlet"
    assert plan.source_region == "GLOBAL"
    assert plan.source_weight == 2


def test_parse_skips_untitled_and_handles_bad_dates() -> None:
    articles = _parse_basic()
    assert "" not in articles
    assert articles["Entry without a date"].published_at == NOW
    assert articles["Entry dated in the future"].published_at == NOW  # 17:00 > now + tolerance
    assert articles["Index gains as S&P rallies"].snippet == "The S&P 500 rose 0.20%"


def test_parse_truncates_long_snippets() -> None:
    snippet = _parse_basic()["Long description entry"].snippet
    assert len(snippet) <= 500
    assert snippet.endswith("…")


def test_google_news_entries_use_outlet_and_strip_suffix() -> None:
    feed = make_feed(
        name="Google News US", url="https://news.google.com/rss?hl=en-US", region="US", weight=1
    )
    known = make_feed(
        name="Example Times", url="https://example-times.com/rss", region="IN", weight=3
    )
    wire = make_feed(
        name="Example Wire",
        url="https://examplewire.com/rss",
        region="GLOBAL",
        weight=3,
        enabled=False,
    )
    resolver = SourceResolver([feed, known, wire])
    articles = parse_feed(read_fixture("google_news.xml"), feed, NOW, resolver, 500)
    by_title = {article.title: article for article in articles}

    assert set(by_title) == {
        "Parliament passes trade bill",
        "Phone maker launches new model",
        "Storm hits coast",
    }
    trade = by_title["Parliament passes trade bill"]
    assert (trade.source_name, trade.source_region, trade.source_weight) == (
        "Example Times",
        "IN",
        3,
    )
    assert trade.snippet == ""
    assert trade.url == "https://news.google.com/rss/articles/AAA111?oc=5"

    phone = by_title["Phone maker launches new model"]  # unknown outlet: edition region, weight 1
    assert (phone.source_name, phone.source_region, phone.source_weight) == ("Gadgets.com", "US", 1)

    storm = by_title["Storm hits coast"]  # domain-style name matches a (disabled) configured outlet
    assert (storm.source_name, storm.source_weight) == ("Example Wire", 3)


def test_filter_recent() -> None:
    def article(hours_ago: float) -> FetchedArticle:
        return FetchedArticle(
            url=f"https://example.com/{hours_ago}",
            source_name="A",
            source_region="US",
            source_weight=1,
            title="t",
            snippet="",
            published_at=NOW - timedelta(hours=hours_ago),
            fetched_at=NOW,
            feed_url="https://example.com/rss",
        )

    kept = filter_recent([article(1), article(11.9), article(12.1)], 12, NOW)
    assert [a.url for a in kept] == ["https://example.com/1", "https://example.com/11.9"]


def test_fetch_all_survives_broken_feeds(settings: Settings) -> None:
    good = make_feed(url="https://good.example.com/rss")
    server_error = make_feed(name="Down", url="https://down.example.com/rss")
    garbage = make_feed(name="Garbage", url="https://garbage.example.com/rss")
    unreachable = make_feed(name="Unreachable", url="https://unreachable.example.com/rss")

    def handler(request: httpx.Request) -> httpx.Response:
        match request.url.host:
            case "good.example.com":
                return httpx.Response(200, content=read_fixture("rss_basic.xml"))
            case "down.example.com":
                return httpx.Response(500)
            case "garbage.example.com":
                return httpx.Response(200, content=b"\x00\x01 this is not a feed <<<")
            case _:
                raise httpx.ConnectError("no route", request=request)

    async def no_sleep(_: float) -> None:
        return None

    async def go() -> list:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_all(
                [good, server_error, garbage, unreachable], settings, client=client, sleep=no_sleep
            )

    results = {result.feed.name: result for result in asyncio.run(go())}
    assert results["Example Outlet"].ok and len(results["Example Outlet"].articles) == 5
    assert results["Down"].error == "HTTP 500"
    assert results["Garbage"].error is not None and not results["Garbage"].articles
    assert (
        results["Unreachable"].error is not None and "ConnectError" in results["Unreachable"].error
    )
