import asyncio
import json
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import Settings
from app.delivery.format import (
    FOOTER,
    DigestItem,
    SourceLink,
    format_digest,
    format_story,
    pick_sources,
    telegram_length,
)
from app.delivery.telegram import TelegramError, get_me, send_messages
from app.models import Article
from tests.conftest import NOW

KOLKATA = ZoneInfo("Asia/Kolkata")
TOKEN = "123456:SECRET-TOKEN"


def _item(n: int, summary: str = "Something happened. It matters.") -> DigestItem:
    return DigestItem(
        headline=f"Headline number {n}",
        summary=summary,
        category="Economy & Markets",
        regions=["India", "Global"],
        disagreement_note=None,
        sources=[SourceLink("BBC", f"https://example.com/{n}")],
    )


# ---------------------------------------------------------------- format


def test_story_is_escaped_for_telegram_html() -> None:
    item = DigestItem(
        headline="Tariffs <b>up</b> & away",
        summary="Costs rose as a < b > c.",
        category="Business",
        regions=["US"],
        disagreement_note="One says 5 & another says <6>.",
        sources=[SourceLink('The "Paper"', 'https://example.com/a?x=1&y="2"')],
    )
    text = format_story(item)
    assert text.startswith("<b>Tariffs &lt;b&gt;up&lt;/b&gt; &amp; away</b>")
    assert "<i>Business · US</i>" in text
    assert "Costs rose as a &lt; b &gt; c." in text
    assert "One says 5 &amp; another says &lt;6&gt;." in text
    assert '<a href="https://example.com/a?x=1&amp;y=&quot;2&quot;">The "Paper"</a>' in text


def test_pick_sources_one_link_per_outlet_up_to_three() -> None:
    def article(source: str, weight: int, hours_ago: float) -> Article:
        return Article(
            url=f"https://example.com/{source}/{hours_ago}",
            source_name=source,
            source_region="US",
            source_weight=weight,
            title="t",
            snippet="",
            published_at=NOW - timedelta(hours=hours_ago),
            fetched_at=NOW,
        )

    links = pick_sources(
        [
            article("CNBC", 2, 1),
            article("BBC", 3, 1),
            article("BBC", 3, 5),
            article("The Hindu", 3, 2),
            article("Hindu", 3, 9),
            article("Politico", 1, 1),
        ]
    )
    assert [link.name for link in links] == ["Hindu", "BBC", "CNBC"]
    assert links[1].url == "https://example.com/BBC/5"  # earliest article from that outlet


def test_telegram_length_counts_utf16_units() -> None:
    assert telegram_length("abc") == 3
    assert telegram_length("₹") == 1
    assert telegram_length("😀") == 2


def test_digest_splits_between_stories_under_limit() -> None:
    items = [_item(n, summary="Word " * 40 + "end. Second sentence.") for n in range(1, 9)]
    messages = format_digest(items, NOW, KOLKATA, limit=700)

    assert len(messages) > 1
    assert all(telegram_length(m) <= 700 for m in messages)
    assert messages[0].startswith("<b>Newsdesk digest</b> · Wed 16 Sep 2026, 17:30 IST · 8 stories")
    for n in range(1, 9):  # every story whole, in exactly one message
        holders = [m for m in messages if f"Headline number {n}</b>" in m]
        assert len(holders) == 1 and f'href="https://example.com/{n}"' in holders[0]
    assert messages[-1].endswith(FOOTER)
    assert sum(FOOTER in m for m in messages) == 1


def test_single_oversized_story_is_shortened_to_fit() -> None:
    messages = format_digest([_item(1, summary="long " * 2000)], NOW, KOLKATA, limit=1000)
    assert all(telegram_length(m) <= 1000 for m in messages)
    assert "Headline number 1" in messages[0] and "…" in messages[0]


def test_empty_digest_still_has_footer() -> None:
    messages = format_digest([], NOW, KOLKATA)
    assert len(messages) == 1
    assert "No new stories" in messages[0] and messages[0].endswith(FOOTER)


# ---------------------------------------------------------------- telegram


async def _no_sleep(_: float) -> None:
    return None


def test_send_messages_posts_html_in_order(settings: Settings) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(requests)}})

    sent = asyncio.run(
        send_messages(["first", "second"], TOKEN, "42", settings.http, httpx.MockTransport(handler))
    )
    assert sent == 2
    assert [r.url.path for r in requests] == [f"/bot{TOKEN}/sendMessage"] * 2
    payloads = [json.loads(r.content) for r in requests]
    assert [p["text"] for p in payloads] == ["first", "second"]
    assert payloads[0]["parse_mode"] == "HTML"
    assert payloads[0]["chat_id"] == "42"
    assert payloads[0]["link_preview_options"] == {"is_disabled": True}


def test_telegram_error_description_raised_without_token(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    with pytest.raises(TelegramError, match="chat not found") as info:
        asyncio.run(send_messages(["hi"], TOKEN, "42", settings.http, httpx.MockTransport(handler)))
    assert TOKEN not in str(info.value)


def test_token_never_logged_on_retries(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502)
        if calls["n"] == 2:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"username": "newsdesk_bot"}})

    caplog.set_level(logging.DEBUG, logger="app")
    bot = asyncio.run(get_me(TOKEN, settings.http, httpx.MockTransport(handler), sleep=_no_sleep))
    assert bot["username"] == "newsdesk_bot"
    assert "telegram getMe" in caplog.text
    assert TOKEN not in caplog.text


def test_transport_failure_message_hides_token(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(TelegramError) as info:
        asyncio.run(get_me(TOKEN, settings.http, httpx.MockTransport(handler), sleep=_no_sleep))
    assert TOKEN not in str(info.value) and "***" in str(info.value)


def test_pick_sources_skips_non_news() -> None:
    def article(source: str, non_news: bool) -> Article:
        return Article(
            url=f"https://example.com/{source}",
            source_name=source,
            source_region="US",
            source_weight=3,
            title="t",
            snippet="",
            published_at=NOW,
            fetched_at=NOW,
            non_news=non_news,
        )

    links = pick_sources([article("Explainer Weekly", True), article("BBC", False)])
    assert [link.name for link in links] == ["BBC"]
