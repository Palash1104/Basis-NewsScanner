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
    digest_item,
    format_digest,
    format_story,
    pick_sources,
    telegram_length,
)
from app.delivery.telegram import TelegramError, get_me, send_messages
from app.models import Article, Impact, Story
from app.presentation import signal_block, story_calls
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
    assert messages[0].startswith("<b>BASIS</b> · Wed 16 Sep 2026, 17:30 IST · 8 stories")
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


def _block(
    impacts: list[Impact],
    labels: dict[int, str] | None = None,
    limit: int = 6,
    track_record: str | None = None,
    assets: dict[str, object] | None = None,
) -> list[str]:
    """The market block's lines, exactly as Telegram receives them."""
    known = ASSETS if assets is None else assets
    item = DigestItem(
        headline="Strikes hit a Saudi oil terminal",
        summary="Drones struck a terminal.",
        category="Geopolitics",
        regions=["Global"],
        disagreement_note=None,
        sources=[],
        signals=signal_block(story_calls(impacts, known, limit, labels), track_record),
    )
    text = format_story(item)
    quote = text.split("<blockquote expandable>")[1].split("</blockquote>")[0]
    return quote.split("\n")


def test_what_every_call_shares_is_said_once() -> None:
    """The old digest repeated "1st · medium · playbook" on every line, which made the reader
    compare near-identical strings to find the one that differed."""
    impacts = [_impact("BZ=F", "up"), _impact("CL=F", "up")]
    lines = _block(impacts)

    assert lines[0] == "<b>MARKET SIGNALS</b> · playbook · first order · medium confidence"
    assert lines[1] == "▲ Brent crude"
    assert lines[2] == "▲ WTI crude"
    # The mechanism they share is stated once, at the bottom, not on either line.
    assert lines[-1] == "<i>Why:</i> Supply fears add a risk premium to crude"


def test_only_what_differs_is_repeated_on_a_line() -> None:
    impacts = [
        _impact("BZ=F", "up", confidence="high"),
        _impact("INR=X", "up", order="second", mechanism="A higher oil import bill hits the rupee"),
        _impact(
            "ASIANPAINT.NS", "down", order="second", confidence="low", mechanism="Costlier inputs"
        ),
    ]
    lines = _block(impacts)

    # Only the origin is common to all three, so only it is in the header.
    assert lines[0] == "<b>MARKET SIGNALS</b> · playbook"
    assert lines[1] == "▲ Brent crude · first order · high confidence"
    assert lines[2] == "▲ USD/INR (rupee weaker) · second order · medium confidence"
    assert lines[3] == "▼ Asian Paints · second order · low confidence"


def test_first_order_and_confidence_still_set_the_order() -> None:
    impacts = [
        _impact("ASIANPAINT.NS", "down", order="second", confidence="low"),
        _impact("INR=X", "up", order="second"),
        _impact("BZ=F", "up", confidence="high"),
    ]
    lines = _block(impacts)
    assert [line.split(" · ")[0] for line in lines[1:4]] == [
        "▲ Brent crude",
        "▲ USD/INR (rupee weaker)",
        "▼ Asian Paints",
    ]


def test_rules_that_agree_are_counted_on_the_line() -> None:
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
    lines = _block(impacts)
    assert lines[1] == "▼ Nifty 50 · 2 rules"
    assert lines[-1].startswith("<i>Why:</i>")
    assert "Risk-off selling" in lines[-1]


def test_opposite_calls_stay_one_mixed_signals_line() -> None:
    impacts = [
        _impact("GC=F", "up", mechanism="Safe-haven demand", conflict=True),
        _impact("GC=F", "down", mechanism="Higher yields", conflict=True, rule_id="fed_hawkish"),
        _impact("BZ=F", "up"),
    ]
    lines = _block(impacts)

    assert lines[1] == "↕ Gold · mixed signals"
    assert lines[2] == "▲ Brent crude"
    # Both sides of the conflict are still visible, in the reasons.
    assert any("Safe-haven demand vs Higher yields" in line for line in lines)


def test_extra_impacts_are_summarized_as_n_more() -> None:
    impacts = [
        _impact(symbol, "up", mechanism=f"m{index}")
        for index, symbol in enumerate(["BZ=F", "CL=F", "GC=F", "SI=F", "HG=F", "ZW=F", "ZC=F"])
    ]
    lines = _block(impacts)
    assert "+1 more: Corn" in lines


def test_unknown_symbol_falls_back_to_the_ticker() -> None:
    lines = _block([_impact("XYZ", "up")], assets={})
    assert lines[1] == "▲ XYZ"


def test_a_move_and_its_label_are_two_words() -> None:
    impacts = [
        _impact("BZ=F", "up", id=1, reference_price=100.0, move_at_detection_pct=2.4),
        _impact("CL=F", "up", id=2, reference_price=100.0, move_at_detection_pct=1.9),
    ]
    lines = _block(impacts, labels={1: "already moved"})

    assert lines[1] == "▲ Brent crude +2.4% ✓ already moved"
    assert lines[2] == "▲ WTI crude +1.9%"
    # The window every move covers is named once, not on each line.
    assert lines.count("<i>Moves since news</i>") == 1
    assert "since news" not in lines[1]


def test_a_move_against_the_call_is_marked() -> None:
    impact = _impact("BZ=F", "up", id=1, reference_price=100.0, move_at_detection_pct=-3.1)
    lines = _block([impact], labels={1: "moving against this call"})
    assert lines[1] == "▲ Brent crude -3.1% ✕ against call"


def test_yield_moves_are_shown_in_points_with_the_unit() -> None:
    impact = _impact(
        "^TNX",
        "up",
        id=3,
        reference_price=4.0,
        move_at_detection_pct=2.5,
        mechanism="Tighter policy",
    )
    lines = _block([impact], labels={3: "already moved"})
    assert lines[1] == "▲ US 10-year yield +0.10 pts ✓ already moved"


def test_impacts_without_a_price_show_the_name_alone() -> None:
    lines = _block([_impact("BZ=F", "up")])
    assert lines[1] == "▲ Brent crude"
    assert "Moves since news" not in lines


def test_one_why_line_per_rule_not_per_asset() -> None:
    impacts = [
        _impact("BZ=F", "up"),
        _impact("CL=F", "up"),
        _impact("GC=F", "up", rule_id="geopolitical_risk_off", mechanism="Investors buy gold"),
    ]
    lines = _block(impacts)
    whys = [line for line in lines if line.startswith("<i>Why:</i>")]
    assert len(whys) == 2
    assert whys[0] == "<i>Why:</i> oil_supply_shock — Supply fears add a risk premium to crude"
    assert whys[1] == "<i>Why:</i> geopolitical_risk_off — Investors buy gold"


def test_a_single_rule_needs_no_name_in_front_of_its_reason() -> None:
    lines = _block([_impact("BZ=F", "up")])
    assert lines[-1] == "<i>Why:</i> Supply fears add a risk premium to crude"


def test_the_track_record_closes_the_block() -> None:
    line = "Track record: oil_supply_shock right 10 of 24 (1d close, 5 stories) · early"
    lines = _block([_impact("BZ=F", "up")], track_record=line)
    assert lines[-1] == f"<i>{line}</i>"


def test_the_market_block_is_one_expandable_quote(settings: Settings) -> None:
    """Telegram hides all but the first lines behind "show more", so the digest stays
    scrollable however many assets a story calls."""
    item = DigestItem(
        headline="Strikes hit a Saudi oil terminal",
        summary="Drones struck a terminal. Exports may slow.",
        category="Geopolitics",
        regions=["Global"],
        disagreement_note=None,
        sources=[SourceLink("BBC", "https://example.com/1")],
        signals=signal_block(story_calls([_impact("BZ=F", "up")], ASSETS, 6)),
    )
    text = format_story(item, number=3)
    lines = text.split("\n")

    assert lines[0] == "<b>03 · Strikes hit a Saudi oil terminal</b>"
    assert lines[1] == "<i>Geopolitics · Global</i>"
    assert lines[2] == ""  # the summary is its own paragraph
    assert lines[3] == "Drones struck a terminal. Exports may slow."
    assert text.count("<blockquote expandable>") == 1
    assert text.count("</blockquote>") == 1
    assert text.endswith('Sources: <a href="https://example.com/1">BBC</a>')


def test_a_story_with_no_calls_has_no_quote_at_all() -> None:
    item = DigestItem(
        headline="Parliament debates the water treaty",
        summary="Nothing for markets here.",
        category="Politics",
        regions=["India"],
        disagreement_note=None,
        sources=[SourceLink("The Hindu", "https://example.com/2")],
        signals=signal_block(story_calls([], ASSETS, 6)),
    )
    text = format_story(item, number=1)
    assert "blockquote" not in text
    assert text.startswith("<b>01 · Parliament debates the water treaty</b>")


def test_nothing_links_to_the_local_dashboard() -> None:
    """127.0.0.1 is not reachable from a phone, so the digest never points at it."""
    item = DigestItem(
        headline="Strikes hit a Saudi oil terminal",
        summary="Drones struck a terminal.",
        category="Geopolitics",
        regions=["Global"],
        disagreement_note=None,
        sources=[SourceLink("BBC", "https://example.com/1")],
        signals=signal_block(story_calls([_impact("BZ=F", "up")], ASSETS, 6)),
    )
    text = format_story(item, number=1)
    assert "127.0.0.1" not in text and "localhost" not in text


# ---------------------------------------------------------------- story age


def _aged(first_seen_hours: int, summarized_hours: int) -> Story:
    """A story that broke `first_seen_hours` ago and was summarized `summarized_hours` ago."""
    return Story(
        first_seen_at=NOW - timedelta(hours=first_seen_hours),
        updated_at=NOW - timedelta(hours=summarized_hours),
        headline="Kerala floods",
        summary="Rain.",
        category="Science & Health",
        regions=["India"],
        status="summarized",
    )


def test_a_story_summarized_long_after_it_broke_is_dated(settings: Settings) -> None:
    """The reserved slots and carried-over summaries both surface older stories; a reader
    should not have to guess whether this happened this morning."""
    item = digest_item(_aged(first_seen_hours=30, summarized_hours=1), now=NOW)
    assert item.age == "first reported 30h ago"
    assert "Science &amp; Health · India · first reported 30h ago" in format_story(item)


def test_a_story_older_than_two_days_is_counted_in_days(settings: Settings) -> None:
    item = digest_item(_aged(first_seen_hours=80, summarized_hours=2), now=NOW)
    assert item.age == "first reported 3d ago"


def test_a_story_summarized_as_it_broke_is_not_dated(settings: Settings) -> None:
    """Most stories are summarized within a run or two, and saying "2h ago" adds nothing."""
    item = digest_item(_aged(first_seen_hours=3, summarized_hours=1), now=NOW)
    assert item.age is None
    assert "first reported" not in format_story(item)


def test_the_age_is_left_out_when_no_time_is_given(settings: Settings) -> None:
    assert digest_item(_aged(first_seen_hours=40, summarized_hours=1)).age is None
