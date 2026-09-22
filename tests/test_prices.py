from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.config import AssetConfig, Settings, load_assets
from app.models import Event, Impact, Story
from app.pipeline.prices import (
    DAILY,
    INTRADAY,
    Bar,
    cached_bars,
    format_move,
    move_label,
    move_pct,
    price_impacts,
    reference_point,
    refresh_symbol,
    refresh_universe,
    typical_move,
    usable_daily,
)
from tests.fakes import FakePrices

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")
ASSETS = {asset.symbol: asset for asset in load_assets()}
ONGC, BRENT, TNX, WHEAT = (ASSETS[s] for s in ("ONGC.NS", "BZ=F", "^TNX", "ZW=F"))

# A story filed at 23:00 IST on Thursday: the NSE is shut, crude is still trading.
STORY_AT = datetime(2026, 9, 17, 23, 0, tzinfo=IST)


def _bars(times: list[datetime], closes: list[float], volume: float = 1000.0) -> list[Bar]:
    return [
        Bar(ts.astimezone(UTC), close, close, close, close, volume)
        for ts, close in zip(times, closes, strict=True)
    ]


def _nse_session(day: datetime) -> list[datetime]:
    """The seven hourly bars Yahoo publishes for an NSE session (09:15 to 15:15 IST)."""
    return [day.replace(hour=9, minute=15) + timedelta(hours=n) for n in range(7)]


NSE_BARS = _bars(
    _nse_session(datetime(2026, 9, 17, tzinfo=IST))
    + _nse_session(datetime(2026, 9, 18, tzinfo=IST)),
    [230, 231, 232, 233, 234, 235, 236] + [240, 241, 242, 243, 244, 245, 246],
)
# Crude trades almost around the clock, so bars exist through the Indian night.
CRUDE_BARS = _bars(
    [datetime(2026, 9, 17, 12, 0, tzinfo=NY) + timedelta(hours=n) for n in range(8)],
    [100.0, 100.5, 101.0, 101.5, 102.0, 102.5, 103.0, 103.5],
)


# ---------------------------------------------------------------- reference selection


def test_story_outside_nse_hours_references_the_next_session() -> None:
    reference = reference_point(NSE_BARS, STORY_AT)
    assert reference is not None
    when, price = reference
    assert when.astimezone(IST) == datetime(2026, 9, 18, 9, 15, tzinfo=IST)  # next morning
    assert price == 236  # Thursday's last close, the price before the news


def test_the_same_story_references_the_next_hourly_bar_for_crude() -> None:
    reference = reference_point(CRUDE_BARS, STORY_AT)  # 23:00 IST is 13:30 in New York
    assert reference is not None
    when, price = reference
    assert when.astimezone(NY) == datetime(2026, 9, 17, 14, 0, tzinfo=NY)
    assert price == 100.5  # the 13:00 close


def test_no_reference_without_a_price_before_the_story() -> None:
    assert reference_point(NSE_BARS, datetime(2026, 9, 1, tzinfo=IST)) is None  # nothing before
    assert reference_point(NSE_BARS, datetime(2026, 9, 30, tzinfo=IST)) is None  # nothing after
    assert reference_point([], STORY_AT) is None


# ---------------------------------------------------------------- volatility baseline


def _daily(closes: list[float], volumes: list[float] | None = None) -> list[Bar]:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    volumes = volumes or [1000.0] * len(closes)
    return [
        Bar(start + timedelta(days=n), close, close, close, close, volume)
        for n, (close, volume) in enumerate(zip(closes, volumes, strict=True))
    ]


def test_holiday_filler_bars_are_left_out_for_assets_that_report_volume() -> None:
    """An NSE holiday leaves a zero-volume daily bar for .NS stocks; counting it as a flat day
    would understate the asset's typical move."""
    closes = [100, 100, 102, 102, 104]
    volumes = [1000, 0, 1000, 0, 1000]  # two holiday fillers
    assert [bar.close for bar in usable_daily(_daily(closes, volumes), ONGC)] == [100, 102, 104]
    # Indices, FX and rates always report zero volume, so nothing is dropped for them.
    assert len(usable_daily(_daily(closes, volumes), TNX)) == 5


def test_typical_move_is_a_fraction_for_prices_and_points_for_yields() -> None:
    swinging = _daily([100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100])
    assert typical_move(swinging, ONGC, min_returns=10) == pytest.approx(0.00995, abs=1e-4)
    yields = _daily([4.0, 4.1, 4.0, 4.1, 4.0, 4.1, 4.0, 4.1, 4.0, 4.1, 4.0])
    assert typical_move(yields, TNX, min_returns=10) == pytest.approx(0.1, abs=1e-6)


def test_too_little_history_gives_no_baseline() -> None:
    assert typical_move(_daily([100, 101, 100]), ONGC, min_returns=10) is None


# ---------------------------------------------------------------- labels and formatting


def _label(asset: AssetConfig, direction: str, reference: float, pct: float, typical: float | None):
    return move_label(asset, direction, reference, pct, typical, multiple=1.0)


def test_labels_need_a_move_of_one_typical_day_in_the_called_direction() -> None:
    typical = 0.01  # 1% a day
    assert _label(ONGC, "up", 100, 2.0, typical) == "already moved"
    assert _label(ONGC, "up", 100, -2.0, typical) == "moving against this call"
    assert _label(ONGC, "down", 100, -2.0, typical) == "already moved"
    assert _label(ONGC, "up", 100, 0.5, typical) is None  # inside the day's normal range
    assert _label(ONGC, "up", 100, 5.0, None) is None  # no baseline: no label


def test_yield_labels_are_measured_in_points() -> None:
    # 4.00 -> 4.10 is +2.5% but only +0.10 points, which is one typical day for this yield.
    assert _label(TNX, "up", 4.0, 2.5, 0.1) == "already moved"
    assert _label(TNX, "up", 4.0, 1.0, 0.1) is None  # +0.04 points: inside the range


def test_moves_are_shown_in_percent_except_yields_which_use_points() -> None:
    assert format_move(ONGC, 100, 2.35) == "+2.4%"
    assert format_move(BRENT, 100, -1.04) == "-1.0%"
    assert format_move(TNX, 4.0, 2.5) == "+0.10 pts"
    assert format_move(TNX, 4.0, -5.0) == "-0.20 pts"


def test_cents_quoted_grains_behave_like_any_other_percentage() -> None:
    """Wheat quotes in US cents (714.25 = $7.1425); a ratio of two cent prices is the same
    number as a ratio of two dollar prices, so nothing needs converting."""
    assert WHEAT.currency == "USX"
    in_cents = move_pct(714.25, 728.50)
    in_dollars = move_pct(7.1425, 7.2850)
    assert in_cents == pytest.approx(in_dollars)
    assert format_move(WHEAT, 714.25, in_cents) == "+2.0%"
    cent_bars = _daily([700, 707, 700, 707, 700, 707, 700, 707, 700, 707, 700])
    dollar_bars = _daily([7.00, 7.07, 7.00, 7.07, 7.00, 7.07, 7.00, 7.07, 7.00, 7.07, 7.00])
    assert typical_move(cent_bars, WHEAT, 10) == pytest.approx(typical_move(dollar_bars, WHEAT, 10))


# ---------------------------------------------------------------- cache


def test_cache_fetches_only_the_tail_and_replaces_the_latest_bar(session: Session) -> None:
    now = CRUDE_BARS[-1].ts + timedelta(minutes=30)  # inside the newest bar's hour
    series = {"BZ=F": {INTRADAY: CRUDE_BARS}}
    provider = FakePrices(series)
    stored = refresh_symbol(session, provider, "BZ=F", INTRADAY, now - timedelta(days=2), now)
    assert stored == len(CRUDE_BARS) and provider.calls == [("BZ=F", INTRADAY)]

    # Same hour again: the current bar is already cached, so no request is made.
    assert refresh_symbol(session, provider, "BZ=F", INTRADAY, now - timedelta(days=2), now) == 0
    assert provider.calls == [("BZ=F", INTRADAY)]

    # An hour later the last bar has moved on; it is re-fetched and overwritten, not duplicated.
    last = CRUDE_BARS[-1]
    provider.series["BZ=F"][INTRADAY] = [
        *CRUDE_BARS[:-1],
        Bar(last.ts, 103.5, 105, 103.5, 104.9, 1),
    ]
    later = last.ts + timedelta(hours=2)
    refresh_symbol(session, provider, "BZ=F", INTRADAY, later - timedelta(days=2), later)
    bars = cached_bars(session, "BZ=F", INTRADAY)
    assert len(bars) == len(CRUDE_BARS) and bars[-1].close == 104.9


# ---------------------------------------------------------------- the universe refresh


def test_the_whole_universe_is_fetched_in_one_request(session: Session) -> None:
    """The web UI ranks 24-hour movers across every asset, not only the ones a story called,
    so each run tops up the lot - in one batched request, and with no LLM call anywhere."""
    now = CRUDE_BARS[-1].ts + timedelta(minutes=30)
    gold = _bars([bar.ts for bar in CRUDE_BARS], [2400.0] * len(CRUDE_BARS))
    provider = FakePrices({"BZ=F": {INTRADAY: CRUDE_BARS}, "GC=F": {INTRADAY: gold}})

    report = refresh_universe(session, provider, ["BZ=F", "GC=F", "CL=F"], now)

    assert provider.batches == [(("BZ=F", "GC=F", "CL=F"), INTRADAY)]
    assert provider.calls == []  # one request for all three, not one each
    assert report.symbols == 2  # CL=F returned nothing; it is simply absent
    assert report.bars_stored == len(CRUDE_BARS) + len(gold)
    assert len(cached_bars(session, "GC=F", INTRADAY)) == len(gold)


def test_a_failed_universe_refresh_is_reported_not_raised(session: Session) -> None:
    """Yahoo being down must not cost the run its impacts, its events or its digest."""
    provider = FakePrices({"BZ=F": {INTRADAY: CRUDE_BARS}})
    provider.fail_batch = True

    report = refresh_universe(session, provider, ["BZ=F"], CRUDE_BARS[-1].ts)

    assert report.error and "503" in report.error
    assert report.symbols == 0 and cached_bars(session, "BZ=F", INTRADAY) == []


def test_without_a_provider_nothing_is_fetched(session: Session) -> None:
    assert refresh_universe(session, None, ["BZ=F"], STORY_AT).symbols == 0


# ---------------------------------------------------------------- the pipeline step


def _story_with_impacts(session: Session, symbols: list[str], direction: str = "up") -> Story:
    story = Story(
        first_seen_at=STORY_AT,
        updated_at=STORY_AT,
        headline="Strikes hit an oil terminal",
        summary="One. Two.",
        status="analyzed",
    )
    event = Event(
        story=story,
        event_type="geopolitical_conflict",
        countries=[],
        regions=[],
        entities=[],
        companies=[],
        channels=["oil_supply"],
        severity="escalation",
        policy_stance="not_applicable",
        is_new_development=True,
        model="m",
        prompt_version="event-v2",
        created_at=STORY_AT,
    )
    session.add_all([story, event])
    for symbol in symbols:
        session.add(
            Impact(
                story=story,
                event=event,
                symbol=symbol,
                direction=direction,
                mechanism="m",
                order="first",
                confidence="high",
                origin="playbook",
                rule_id="oil_supply_shock",
                created_at=STORY_AT,
            )
        )
    session.flush()
    return story


def test_pricing_fills_the_reference_then_only_refreshes_the_move(
    session: Session, settings: Settings
) -> None:
    now = datetime(2026, 9, 18, 16, 0, tzinfo=IST)
    series = {
        "ONGC.NS": {INTRADAY: NSE_BARS, DAILY: _daily([230, 232, 230, 232, 236])},
        "BZ=F": {INTRADAY: CRUDE_BARS, DAILY: _daily([100, 101, 100, 101, 103.5])},
    }
    provider = FakePrices(series)
    story = _story_with_impacts(session, ["ONGC.NS", "BZ=F"])

    report = price_impacts(session, story.impacts, ASSETS, provider, settings, now)

    assert report.priced == 2 and report.symbols == 2 and report.unusable == []
    ongc, brent = story.impacts
    assert ongc.reference_time is not None
    assert ongc.reference_time.astimezone(IST).hour == 9  # next session's first bar
    assert ongc.reference_price == 236 and ongc.move_at_detection_pct == pytest.approx(
        4.24, abs=0.01
    )
    assert brent.reference_price == 100.5
    assert brent.move_at_detection_pct == pytest.approx(2.99, abs=0.01)

    # A later run keeps the reference and only updates the move.
    provider.series["ONGC.NS"][INTRADAY] = [
        *NSE_BARS,
        Bar(NSE_BARS[-1].ts + timedelta(hours=1), 250, 250, 250, 250, 10),
    ]
    later = now + timedelta(hours=3)
    again = price_impacts(session, story.impacts, ASSETS, provider, settings, later)
    assert again.priced == 0 and again.refreshed == 2
    assert ongc.reference_price == 236 and ongc.move_at_detection_pct == pytest.approx(
        5.93, abs=0.01
    )


@pytest.mark.parametrize(
    ("series", "failing", "reason"),
    [
        ({}, set(), "no intraday bars"),
        ({"ONGC.NS": {INTRADAY: NSE_BARS}}, {"ONGC.NS"}, "provider error"),
    ],
)
def test_symbols_that_cannot_be_priced_are_reported_not_raised(
    session: Session, settings: Settings, series: dict, failing: set[str], reason: str
) -> None:
    provider = FakePrices(series)
    provider.fail = failing
    story = _story_with_impacts(session, ["ONGC.NS"])
    report = price_impacts(
        session, story.impacts, ASSETS, provider, settings, datetime(2026, 9, 18, 16, 0, tzinfo=IST)
    )
    assert report.priced == 0
    assert len(report.unusable) == 1 and report.unusable[0][1].startswith(reason)
    assert story.impacts[0].reference_time is None  # retried on the next run


def test_stale_prices_are_treated_as_unavailable(session: Session, settings: Settings) -> None:
    provider = FakePrices({"ONGC.NS": {INTRADAY: NSE_BARS, DAILY: _daily([230, 232])}})
    story = _story_with_impacts(session, ["ONGC.NS"])
    long_after = STORY_AT + timedelta(days=10)
    report = price_impacts(session, story.impacts, ASSETS, provider, settings, long_after)
    assert [reason for _, reason in report.unusable] == ["latest bar is stale"]


def test_an_asset_outside_the_universe_is_reported(session: Session, settings: Settings) -> None:
    story = _story_with_impacts(session, ["NOTREAL"])
    report = price_impacts(
        session, story.impacts, ASSETS, FakePrices(), settings, datetime(2026, 9, 18, tzinfo=UTC)
    )
    assert report.unusable == [("NOTREAL", "not in the asset universe")]


def test_a_story_after_the_last_bar_waits_for_the_market_to_open(
    session: Session, settings: Settings
) -> None:
    """News breaking after Friday's close (or overnight) has no reference bar yet. That is the
    normal case, not a failure: the first run after the open prices it."""
    provider = FakePrices({"ONGC.NS": {INTRADAY: NSE_BARS, DAILY: _daily([230, 232, 236])}})
    story = _story_with_impacts(session, ["ONGC.NS"])
    story.first_seen_at = NSE_BARS[-1].ts + timedelta(hours=2)  # after the last session
    saturday = story.first_seen_at + timedelta(hours=6)

    report = price_impacts(session, story.impacts, ASSETS, provider, settings, saturday)

    assert report.waiting == 1 and report.unusable == [] and report.priced == 0
    assert story.impacts[0].reference_time is None

    # Monday's session arrives: the same impact is priced from the open.
    monday_open = NSE_BARS[-1].ts + timedelta(days=3)
    provider.series["ONGC.NS"][INTRADAY] = [*NSE_BARS, Bar(monday_open, 250, 250, 250, 250, 10)]
    after = price_impacts(session, story.impacts, ASSETS, provider, settings, monday_open)
    assert after.priced == 1 and after.waiting == 0
    assert story.impacts[0].reference_time == monday_open
    assert story.impacts[0].reference_price == 246  # Friday's close


def test_one_note_per_symbol_however_many_impacts_it_has(
    session: Session, settings: Settings
) -> None:
    story = _story_with_impacts(session, ["NOTREAL", "NOTREAL"])
    report = price_impacts(
        session, story.impacts, ASSETS, FakePrices(), settings, datetime(2026, 9, 18, tzinfo=UTC)
    )
    assert report.unusable == [("NOTREAL", "not in the asset universe")]
