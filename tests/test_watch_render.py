"""How a watchlist item reads in Telegram (app/watch/render.py, user 2026-10-16)."""

from datetime import UTC, datetime

from app.config import Settings
from app.models import WatchArticle, WatchCall, WatchFiling, WatchMatch, WatchStory
from app.watch.moves import MoveEvent
from app.watch.render import (
    Sections,
    adds_information,
    move_lines,
    short_headline,
    status_line,
    story_item,
)

# Thursday 2026-10-15, 19:07 IST: the end of the catch-up the user sent a screenshot of.
REF = datetime(2026, 10, 15, 13, 37, tzinfo=UTC)
NAMES = {"BDL.NS": "Bharat Dynamics", "ZENTEC.NS": "Zen Technologies", "HAL.NS": "HAL"}


def ist(hour: int, minute: int, day: int = 15) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC) - (
        datetime(2026, 1, 1, 5, 30) - datetime(2026, 1, 1, 0, 0)
    )


def story(
    headline: str, published: datetime, filings: list[WatchFiling] | None = None
) -> WatchStory:
    found = WatchStory(first_seen_at=REF, headline=headline)
    article = WatchArticle(
        url="https://news.example.com/a?x=1&y=2",
        source_name="PSU Watch",
        title=headline,
        snippet="",
        published_at=published,
        first_seen_at=REF,
    )
    article.matches = [WatchMatch(symbol="BDL.NS", verdict="keep", reason="named")]
    found.articles = [article]
    found.filings = filings or []
    return found


def call(reason: str, materiality: str = "high") -> WatchCall:
    return WatchCall(
        story_id=1,
        symbol="BDL.NS",
        relevance="primary",
        sentiment="positive",
        materiality=materiality,
        event_type="order_win",
        reason=reason,
        summary="",
        created_at=REF,
    )


def order_filing(at: datetime) -> WatchFiling:
    return WatchFiling(
        key="k",
        exchange="NSE",
        symbol="BDL.NS",
        company="Bharat Dynamics Limited",
        subject="Bagging/Receiving of orders/contracts",
        description="",
        link="https://nsearchives.nseindia.com/x.pdf",
        filed_at=at,
        first_seen_at=REF,
        kind="filing",
    )


def test_an_item_is_the_company_the_linked_headline_and_one_status_line(
    settings: Settings,
) -> None:
    torpedo = story(
        "Bharat Dynamics Bags ₹1,307 Crore Indian Navy Contract for Supply of Heavyweight "
        "Torpedoes",
        ist(14, 59),
        [order_filing(ist(14, 56))],
    )
    text = story_item(
        torpedo,
        [call("Bharat Dynamics secured a heavyweight torpedo contract worth Rs 1,307 crore.")],
        NAMES,
        settings,
        REF,
    )
    assert text == (
        '▲ <b>Bharat Dynamics</b>: <a href="https://news.example.com/a?x=1&amp;y=2">Bags ₹1,307 '
        "Crore Indian Navy Contract for Supply of Heavyweight Torpedoes</a>\n"
        "<i>NSE filing 14:56 · first media report 14:59</i>"
    )  # the reason only restates the headline, so it is left out


def test_the_reason_shows_when_it_says_something_new(settings: Settings) -> None:
    hal = story("From Tejas to AMCA: 5 Key Projects That Will Shape New Air Chief", ist(15, 10))
    reason = "It names the Tejas as a key project without announcing new orders."
    text = story_item(hal, [call(reason, "low")], NAMES, settings, REF)
    assert text.endswith(f"\n{reason}")
    assert "<i>15:10 · media only, not filed</i>" in text


def test_reasons_that_restate_the_headline_are_recognised() -> None:
    headline = "HAL to Raise AL-31FP Engine Output to 50 a Year"
    assert not adds_information(
        "The company plans to raise AL-31FP engine output to 50 a year.", headline
    )
    assert adds_information("The plan follows a Russian licence renewal signed in March.", headline)


def test_the_company_name_is_not_said_twice() -> None:
    assert (
        short_headline("Aequs Ltd: Insider Trading / SAST", ["Aequs"]) == "Insider Trading / SAST"
    )
    assert (
        short_headline("HAL to Raise AL-31FP Engine Output", ["HAL"])
        == "To Raise AL-31FP Engine Output"
    )
    # Left whole: a sentence about the company, another name, or a longer word.
    whole = "Data Patterns (India) Ltd is Rated Hold"
    assert short_headline(whole, ["Data Patterns"]) == whole
    assert short_headline("BDL’s ₹56.8 Crore Tender Raises Questions", ["Bharat Dynamics"]) == (
        "BDL’s ₹56.8 Crore Tender Raises Questions"
    )
    assert short_headline("HALO Group wins big order", ["HAL"]) == "HALO Group wins big order"
    long = short_headline("Word " * 40, [])
    assert len(long) <= 110 and long.endswith("…")


def test_a_status_line_follows_the_story_through_the_exchange(settings: Settings) -> None:
    asked = order_filing(ist(11, 5))
    asked.kind, asked.subject = "clarification_sought", "News Verification"
    replied = order_filing(ist(15, 33))
    replied.kind = "company_reply"
    raid = story("Prime Focus shares tank 8% after Income Tax raids", ist(10, 23), [asked, replied])
    assert status_line(raid, settings, REF) == (
        "first media report 10:23 · NSE asked for clarification 11:05 · company replied 15:33"
    )


def test_a_price_move_reads_today_and_against_its_index(settings: Settings) -> None:
    event = MoveEvent(
        kind="price",
        key="price:ZENTEC.NS:2026-10-15",
        at=ist(15, 11),
        symbol="ZENTEC.NS",
        move=0.027,
        multiple=2.3,
        group="defence",
        excess=0.038,
    )
    assert move_lines(event, NAMES, settings, REF) == [
        "▲ <b>Zen Technologies</b> +2.7% today, +3.8% vs defence index",
        "<i>15:11 · no news found · 2.3× its typical day</i>",
    ]
    next_day = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)
    assert "+2.7% on Thu 15 Oct" in move_lines(event, NAMES, settings, next_day)[0]


def test_sections_go_high_medium_moves_low_and_low_is_folded() -> None:
    sections = Sections.empty()
    sections.add("low", ist(10, 0), "low one")
    sections.add("moves", ist(15, 11), "a move")
    sections.add("medium", ist(7, 26), "medium old")
    sections.add("medium", ist(9, 0), "medium new")
    sections.add("high", ist(14, 59), "high one")
    text = "\n".join(sections.blocks())
    assert text == (
        "\n<b>HIGH</b>\nhigh one\n"
        "\n<b>MEDIUM</b>\nmedium new\n\nmedium old\n"
        "\n<b>PRICE MOVES</b>\na move\n"
        "\n<b>LOW</b> · 1\n<blockquote expandable>low one</blockquote>"
    )
    assert len(sections) == 5
