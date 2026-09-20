import asyncio
import json
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import Settings, load_assets
from app.delivery.format import (
    FOOTER,
    DigestItem,
    SourceLink,
    format_digest,
    format_story,
    impact_lines,
    pick_sources,
    telegram_length,
)
from app.delivery.telegram import TelegramError, get_me, send_messages
from app.models import Article, Impact
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


# ---------------------------------------------------------------- impacts in the digest

ASSETS = {asset.symbol: asset for asset in load_assets()}


def _impact(symbol: str, direction: str, **fields: object) -> Impact:
    values: dict[str, object] = {
        "order": "first",
        "confidence": "medium",
        "mechanism": "Supply fears add a risk premium to crude",
        "origin": "playbook",
        "rule_id": "oil_supply_shock",
        "conflict": False,
        "created_at": NOW,
    }
    return Impact(symbol=symbol, direction=direction, **(values | fields))


def test_impacts_sharing_a_mechanism_share_a_line() -> None:
    impacts = [_impact("BZ=F", "up"), _impact("CL=F", "up")]
    (line,) = impact_lines(impacts, ASSETS, limit=6)
    assert (
        line == "▲ Brent crude, WTI crude · 1st · medium — Supply fears add a risk premium to crude"
    )


def test_impact_order_first_then_confidence_and_currency_direction_is_spelled_out() -> None:
    impacts = [
        _impact(
            "ASIANPAINT.NS", "down", order="second", confidence="low", mechanism="Costlier inputs"
        ),
        _impact("INR=X", "up", order="second", mechanism="A higher oil import bill hits the rupee"),
        _impact("BZ=F", "up", confidence="high"),
    ]
    lines = impact_lines(impacts, ASSETS, limit=6)
    assert lines[0].startswith("▲ Brent crude · 1st · high")
    assert (
        lines[1]
        == "▲ USD/INR (rupee weaker) · 2nd · medium — A higher oil import bill hits the rupee"
    )
    assert lines[2].startswith("▼ Asian Paints · 2nd · low")


def test_rules_that_agree_are_counted_once() -> None:
    impacts = [
        _impact("^NSEI", "down", order="second", confidence="low", mechanism="Risk-off selling"),
        _impact(
            "^NSEI",
            "down",
            order="second",
            confidence="low",
            mechanism="Risk-off selling",
            rule_id="us_tariffs_on_india",
        ),
    ]
    (line,) = impact_lines(impacts, ASSETS, limit=6)
    assert line == "▼ Nifty 50 · 2nd · low · 2 rules — Risk-off selling"


def test_opposite_calls_become_one_mixed_signals_line() -> None:
    impacts = [
        _impact("GC=F", "up", mechanism="Safe-haven demand", conflict=True),
        _impact("GC=F", "down", mechanism="Higher yields", conflict=True, rule_id="fed_hawkish"),
        _impact("BZ=F", "up"),
    ]
    lines = impact_lines(impacts, ASSETS, limit=6)
    assert lines[0] == "↕ Gold · mixed signals: Safe-haven demand vs Higher yields"
    assert len(lines) == 2 and lines[1].startswith("▲ Brent crude")


def test_extra_impacts_are_summarized_as_n_more() -> None:
    impacts = [
        _impact(symbol, "up", mechanism=f"m{index}")
        for index, symbol in enumerate(["BZ=F", "CL=F", "GC=F", "SI=F", "HG=F", "ZW=F", "ZC=F"])
    ]
    lines = impact_lines(impacts, ASSETS, limit=6)
    assert lines[-1] == "+1 more: ▲ Corn"
    assert len(lines) == 7


def test_unknown_symbol_falls_back_to_the_ticker() -> None:
    (line,) = impact_lines([_impact("XYZ", "up")], {}, limit=6)
    assert line.startswith("▲ XYZ")


def test_story_shows_impacts_under_the_summary() -> None:
    item = DigestItem(
        headline="Strikes hit a Saudi oil terminal",
        summary="Drones struck a terminal. Exports may slow.",
        category="Geopolitics",
        regions=["Global"],
        disagreement_note=None,
        sources=[SourceLink("BBC", "https://example.com/1")],
        impacts=["▲ Brent crude · 1st · high — Supply fears add a risk premium to crude"],
    )
    text = format_story(item)
    lines = text.split("\n")
    assert lines[2] == "Drones struck a terminal. Exports may slow."
    assert lines[3].startswith("▲ Brent crude")
    assert lines[4].startswith("Sources:")


def test_impact_line_shows_the_move_and_the_label() -> None:
    impacts = [
        _impact("BZ=F", "up", id=1, reference_price=100.0, move_at_detection_pct=2.4),
        _impact("CL=F", "up", id=2, reference_price=100.0, move_at_detection_pct=1.9),
    ]
    (line,) = impact_lines(impacts, ASSETS, limit=6, labels={1: "already moved"})
    assert line.startswith(
        "▲ Brent crude +2.4% (already moved), WTI crude +1.9% · since news · 1st · medium"
    )


def test_yield_moves_are_shown_in_points_with_the_unit() -> None:
    impact = _impact(
        "^TNX",
        "up",
        id=3,
        reference_price=4.0,
        move_at_detection_pct=2.5,
        mechanism="Tighter policy",
    )
    (line,) = impact_lines([impact], ASSETS, limit=6, labels={3: "already moved"})
    assert "US 10-year yield +0.10 pts (already moved)" in line
    assert "%" not in line.split("—")[0]


def test_impacts_without_a_price_show_the_name_alone() -> None:
    (line,) = impact_lines([_impact("BZ=F", "up")], ASSETS, limit=6)
    assert line.startswith("▲ Brent crude · 1st · medium")
