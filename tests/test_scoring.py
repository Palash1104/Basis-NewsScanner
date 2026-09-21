from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, load_assets
from app.models import Event, Impact, ImpactScore, Story
from app.pipeline.prices import DAILY, Bar
from app.pipeline.scoring import (
    HIT,
    MISS,
    NO_MOVE,
    UNSCORABLE,
    SymbolData,
    TradingSession,
    mark_unreferenced,
    score_impact,
    score_impacts,
    session_on_or_after,
    sessions,
    story_track_line,
    track_record,
    volatility_before,
)
from tests.fakes import FakePrices

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")
ASSETS = {asset.symbol: asset for asset in load_assets()}
XOM, ONGC, BRENT, NIFTY, SP500 = (ASSETS[s] for s in ("XOM", "ONGC.NS", "BZ=F", "^NSEI", "^GSPC"))

# A story on Thursday 17 September 2026, while New York is trading.
STORY_AT = datetime(2026, 9, 17, 14, 0, tzinfo=NY)
REFERENCE_AT = datetime(2026, 9, 17, 15, 0, tzinfo=NY)


def _daily_bars(closes: list[float], zone: ZoneInfo = NY, volume: float = 1000.0) -> list[Bar]:
    """One bar per weekday from 10 August 2026, stamped at local midnight as Yahoo does."""
    bars, day = [], date(2026, 8, 10)
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars.append(
            Bar(
                datetime(day.year, day.month, day.day, tzinfo=zone),
                close,
                close * 1.01,
                close * 0.99,
                close,
                volume,
            )
        )
        day += timedelta(days=1)
    return bars


def _trading(closes: list[float], **kwargs) -> list[TradingSession]:
    return sessions(_daily_bars(closes, **kwargs), XOM)


# A flat-ish history: 1% swings, so the daily volatility is about 1%.
STEADY = [100.0 + (1 if n % 2 else 0) for n in range(26)]


# ---------------------------------------------------------------- sessions and trading days


def test_holiday_filler_bars_are_not_sessions() -> None:
    bars = _daily_bars([100, 100, 102], zone=IST)
    filler = Bar(bars[1].ts, 100, 100, 100, 100, 0.0)  # NSE holiday: zero volume, no range
    assert len(sessions([bars[0], filler, bars[2]], ONGC)) == 2
    # An index never reports volume, so only a zero-range bar counts as a filler.
    index_bar = Bar(bars[1].ts, 100, 101, 99, 100, 0.0)
    assert len(sessions([bars[0], index_bar, bars[2]], NIFTY)) == 3


def test_sessions_are_dated_in_the_exchanges_own_time_zone() -> None:
    """An NSE bar is stamped 00:00 IST, which is 18:30 the previous day in UTC."""
    bar = Bar(datetime(2026, 9, 18, tzinfo=IST), 230, 231, 229, 230, 5.0)
    assert bar.ts.astimezone(UTC).date() == date(2026, 9, 17)  # what UTC would say
    assert sessions([bar], ONGC)[0].date == date(2026, 9, 18)  # what the exchange says


def test_trading_days_are_counted_as_sessions_not_calendar_days() -> None:
    trading = _trading([100, 101, 102, 103, 104, 105, 106])  # weekdays from Mon 10 Aug
    first = trading[0].date
    assert session_on_or_after(trading, first, 1) == trading[0]  # mid-session: same close
    assert session_on_or_after(trading, first, 5) == trading[4]  # five sessions, not five days
    assert session_on_or_after(trading, first, 5).date == date(2026, 8, 14)  # Friday, no weekend
    assert session_on_or_after(trading, date(2026, 12, 1), 1) is None  # nothing yet


def test_volatility_uses_only_days_before_the_reference() -> None:
    trading = _trading(STEADY)
    reference = trading[-1].date
    assert volatility_before(trading, reference, 20, 10) == pytest.approx(0.00995, abs=1e-4)
    assert volatility_before(trading[:3], reference, 20, 10) is None  # too little history


# ---------------------------------------------------------------- one call, one horizon


def _impact(
    session: Session, symbol: str, direction: str, rule: str = "oil_supply_shock"
) -> Impact:
    story = Story(
        first_seen_at=STORY_AT,
        updated_at=STORY_AT,
        headline="h",
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
    impact = Impact(
        story=story,
        event=event,
        symbol=symbol,
        direction=direction,
        mechanism="m",
        order="first",
        confidence="high",
        origin="playbook",
        rule_id=rule,
        reference_time=REFERENCE_AT,
        reference_price=100.0,
        created_at=STORY_AT,
    )
    session.add_all([story, event, impact])
    session.flush()
    return impact


def _data(closes: list[float], benchmark: list[float] | None = None) -> SymbolData:
    """Sessions ending at the reference date, then the days after it."""
    trading = _trading(closes)
    if benchmark is None:
        return SymbolData(XOM, trading)
    return SymbolData(XOM, trading, "^GSPC", _trading(benchmark), 100.0)


def _closes_after(*moves: float) -> list[float]:
    """A steady history whose last day is the reference session, then the given closes."""
    return [*STEADY, *moves]


def _score(session: Session, settings: Settings, closes: list[float], **kwargs):
    impact = _impact(session, "XOM", kwargs.pop("direction", "up"))
    data = _data(closes, kwargs.pop("benchmark", None))
    # The reference lands on the first session after the steady run, so horizon 1 is judged
    # on that session's close (a story that breaks mid-session is judged from its own close).
    reference_date = _trading(closes)[len(STEADY)].date
    impact.reference_time = datetime(
        reference_date.year, reference_date.month, reference_date.day, 15, 0, tzinfo=NY
    )
    now = impact.reference_time + timedelta(days=30)
    return score_impact(impact, kwargs.pop("horizon", 1), data, settings, now)


def test_a_move_past_the_threshold_in_the_called_direction_is_a_hit(
    session: Session, settings: Settings
) -> None:
    scored = _score(session, settings, _closes_after(103.0))  # +3% against a 1% day
    assert scored is not None and scored.outcome == HIT
    assert scored.asset_return == pytest.approx(0.03)
    assert scored.threshold == pytest.approx(0.5 * 0.00995, abs=1e-4)


def test_a_move_the_other_way_is_a_miss(session: Session, settings: Settings) -> None:
    assert _score(session, settings, _closes_after(97.0)).outcome == MISS


def test_a_move_inside_the_noise_is_no_move(session: Session, settings: Settings) -> None:
    scored = _score(session, settings, _closes_after(100.2))  # +0.2%, under half a daily move
    assert scored.outcome == NO_MOVE


def test_too_little_history_to_set_a_threshold_is_unscorable(
    session: Session, settings: Settings
) -> None:
    impact = _impact(session, "XOM", "up")
    short = _trading([100, 101, 103])
    impact.reference_time = datetime(
        short[1].date.year, short[1].date.month, short[1].date.day, 15, 0, tzinfo=NY
    )
    scored = score_impact(
        impact, 1, SymbolData(XOM, short), settings, impact.reference_time + timedelta(days=30)
    )
    assert scored is not None and scored.outcome == UNSCORABLE and scored.threshold is None


def test_a_horizon_whose_data_has_not_arrived_is_not_due_yet(
    session: Session, settings: Settings
) -> None:
    impact = _impact(session, "XOM", "up")
    trading = _trading(STEADY)
    impact.reference_time = datetime(
        trading[-1].date.year, trading[-1].date.month, trading[-1].date.day, 15, 0, tzinfo=NY
    )
    soon = impact.reference_time + timedelta(days=1)
    assert score_impact(impact, 5, SymbolData(XOM, trading), settings, soon) is None  # wait
    late = impact.reference_time + timedelta(days=60)
    assert score_impact(impact, 5, SymbolData(XOM, trading), settings, late).outcome == UNSCORABLE


def test_the_benchmark_is_subtracted_over_the_same_window(
    session: Session, settings: Settings
) -> None:
    """A stock that rises with its index has no excess return; one that beats it does."""
    with_market = _score(session, settings, _closes_after(103.0), benchmark=_closes_after(103.0))
    assert with_market.outcome == NO_MOVE
    assert with_market.benchmark_symbol == "^GSPC"
    assert with_market.asset_return == pytest.approx(0.03)
    assert with_market.benchmark_return == pytest.approx(0.03)
    assert with_market.excess_return == pytest.approx(0.0, abs=1e-9)

    beating = _score(session, settings, _closes_after(103.0), benchmark=_closes_after(99.0))
    assert beating.outcome == HIT and beating.excess_return == pytest.approx(0.04)


def test_assets_without_a_benchmark_use_the_raw_return(
    session: Session, settings: Settings
) -> None:
    impact = _impact(session, "BZ=F", "up")
    trading = _trading(_closes_after(103.0))
    impact.reference_time = datetime(
        _trading(STEADY)[-1].date.year,
        _trading(STEADY)[-1].date.month,
        _trading(STEADY)[-1].date.day,
        15,
        0,
        tzinfo=NY,
    )
    scored = score_impact(
        impact, 1, SymbolData(BRENT, trading), settings, impact.reference_time + timedelta(days=9)
    )
    assert scored.benchmark_symbol is None and scored.benchmark_return is None
    assert scored.excess_return == pytest.approx(scored.asset_return)


# ---------------------------------------------------------------- the score run


def _series(closes: list[float]) -> dict:
    return {DAILY: _daily_bars(closes)}


def test_scoring_is_idempotent(session: Session, settings: Settings) -> None:
    impact = _impact(session, "XOM", "up")
    reference = _trading(STEADY)[-1].date
    impact.reference_time = datetime(
        reference.year, reference.month, reference.day, 15, 0, tzinfo=NY
    )
    session.commit()
    provider = FakePrices(
        {
            "XOM": _series(_closes_after(103, 104, 105, 106, 107)),
            "^GSPC": _series(_closes_after(100, 100, 100, 100, 100)),
        }
    )
    now = impact.reference_time + timedelta(days=30)

    first = score_impacts(session, ASSETS, provider, settings, now)
    assert first.total_scored == 2  # one row per horizon
    rows = session.scalars(select(ImpactScore)).all()
    assert {row.horizon_days for row in rows} == {1, 5}
    stamps = {row.id: row.scored_at for row in rows}

    again = score_impacts(session, ASSETS, provider, settings, now + timedelta(days=1))
    assert again.total_scored == 0  # nothing rewritten
    rows = session.scalars(select(ImpactScore)).all()
    assert len(rows) == 2 and {row.id: row.scored_at for row in rows} == stamps

    forced = score_impacts(
        session, ASSETS, provider, settings, now + timedelta(days=2), rescore=True
    )
    assert forced.total_scored == 2
    assert len(session.scalars(select(ImpactScore)).all()) == 2  # replaced, not duplicated


def test_impacts_that_never_get_a_reference_price_become_unscorable(
    session: Session, settings: Settings
) -> None:
    impact = _impact(session, "XOM", "up")
    impact.reference_time = None
    impact.reference_price = None
    session.commit()

    soon = STORY_AT + timedelta(days=1)
    assert mark_unreferenced(session, settings, soon) == 0  # still inside the grace period

    late = STORY_AT + timedelta(days=settings.scoring.reference_grace_days + 1)
    assert mark_unreferenced(session, settings, late) == 1
    rows = session.scalars(select(ImpactScore)).all()
    assert [row.outcome for row in rows] == [UNSCORABLE, UNSCORABLE]
    assert mark_unreferenced(session, settings, late + timedelta(days=1)) == 1  # no new rows
    assert len(session.scalars(select(ImpactScore)).all()) == 2


# ---------------------------------------------------------------- track record


def _scored(
    session: Session,
    rule: str,
    outcome: str,
    horizon: int = 1,
    temperature: float | None = None,
    seed: int | None = None,
) -> Impact:
    impact = _impact(session, "XOM", "up", rule=rule)
    impact.event.temperature = temperature
    impact.event.seed = seed
    session.add(
        ImpactScore(impact_id=impact.id, horizon_days=horizon, outcome=outcome, scored_at=STORY_AT)
    )
    session.flush()
    return impact


def test_track_record_counts_by_group_and_keeps_the_story_count(session: Session) -> None:
    for outcome in [HIT, HIT, HIT, MISS, NO_MOVE, UNSCORABLE]:
        _scored(session, "oil_supply_shock", outcome)
    _scored(session, "fed_hawkish", MISS)

    rows = {row.key: row for row in track_record(session, "rule_id")}
    oil = rows["oil_supply_shock"]
    assert (oil.hits, oil.misses, oil.no_move, oil.unscorable) == (3, 1, 1, 1)
    assert oil.judged == 4 and oil.rate == 0.75
    assert oil.no_move_share == pytest.approx(0.2)
    assert len(oil.stories) == 6  # six separate stories in this fixture
    assert {row.key for row in track_record(session, "event_type")} == {"geopolitical_conflict"}
    assert {row.key for row in track_record(session, "origin")} == {"playbook"}
    assert {row.key for row in track_record(session, "prompt_version")} == {"event-v2"}
    # Sampling settings come from the extraction, and rows stored before they were recorded
    # group together rather than being dropped.
    assert {row.key for row in track_record(session, "temperature")} == {"(none)"}
    assert {row.key for row in track_record(session, "seed")} == {"(none)"}


def test_a_sampling_change_splits_the_track_record(session: Session) -> None:
    """The reason temperature and seed are stored: calls made under different settings are
    not one record (SPEC 14)."""
    _scored(session, "oil_supply_shock", HIT, temperature=0.0, seed=20260921)
    _scored(session, "oil_supply_shock", MISS, temperature=0.0, seed=20260921)
    _scored(session, "oil_supply_shock", MISS, temperature=1.0, seed=20260921)

    by_temperature = {row.key: row for row in track_record(session, "temperature")}
    assert (by_temperature["0.0"].hits, by_temperature["0.0"].misses) == (1, 1)
    assert (by_temperature["1.0"].hits, by_temperature["1.0"].misses) == (0, 1)
    assert {row.key for row in track_record(session, "seed")} == {"20260921"}
    # The rule's own record still covers every call it made.
    assert track_record(session, "rule_id")[0].judged == 3


def test_a_rate_is_only_shown_once_there_are_enough_judged_calls(session: Session) -> None:
    for _ in range(4):
        _scored(session, "oil_supply_shock", HIT)
    (row,) = track_record(session, "rule_id")
    assert row.judged == 4 and not row.shows_rate(5)
    _scored(session, "oil_supply_shock", HIT)
    (row,) = track_record(session, "rule_id")
    assert row.judged == 5 and row.shows_rate(5)


def test_the_digest_line_appears_only_for_a_rule_with_enough_calls(session: Session) -> None:
    for _ in range(4):
        _scored(session, "oil_supply_shock", HIT)
    story = _impact(session, "BZ=F", "up").story
    rows = track_record(session, "rule_id")
    assert story_track_line(story, rows, minimum=5) is None  # n=4: nothing shown

    _scored(session, "oil_supply_shock", MISS)
    rows = track_record(session, "rule_id")
    line = story_track_line(story, rows, minimum=5)
    assert line == "Track record: oil_supply_shock right 4 of 5 (1d close, 5 stories)"
