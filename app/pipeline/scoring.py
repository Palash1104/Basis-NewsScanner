"""Scoring and the track record (SPEC 7.9).

Once N trading days of data exist after an impact's reference time, the call is judged:

    asset_return    = close N trading days after the reference / reference_price - 1
    benchmark       = the asset's index (NSE stocks -> ^NSEI, US stocks -> ^GSPC), measured
                      from the same instant over the same sessions; nothing else has one
    excess_return   = asset_return - benchmark_return
    threshold       = scoring.hit_threshold_vol_multiple * daily volatility * sqrt(N)
    outcome         = hit / miss / no_move / unscorable

Trading days are counted from each asset's own sessions, so the NSE and US calendars are
respected without a calendar table: a session is a daily bar that isn't an exchange-holiday
filler (zero volume where the type reports volume, or a zero-range bar with no volume).

Volatility is measured over the days *before* the reference, so the move being judged can't
inflate the threshold it is judged against. That multiple (0.5) is deliberately not the one
Phase 3 uses for "already moved" (1.0): this asks whether the call was right, that asks
whether the news was already in the price.

Scoring is idempotent. A row is written only when every input is present, so a late bar
simply means the next run writes it; existing rows are never rewritten (except by --rescore).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from statistics import pstdev
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.assets import benchmark_for
from app.config import AssetConfig, Settings
from app.models import Event, Impact, ImpactScore, Story
from app.pipeline.prices import (
    DAILY,
    DAILY_LEAD,
    INTRADAY,
    INTRADAY_LEAD,
    VOLUME_BEARING,
    Bar,
    PriceProvider,
    PriceUnavailable,
    cached_bars,
    reference_point,
    refresh_symbol,
)

log = logging.getLogger(__name__)

HIT, MISS, NO_MOVE, UNSCORABLE = "hit", "miss", "no_move", "unscorable"


@dataclass(frozen=True)
class TradingSession:
    date: date  # the session's date in the exchange's own time zone
    close: float


def is_filler(bar: Bar, asset: AssetConfig) -> bool:
    """True for a bar Yahoo writes when the exchange was shut. For `.NS` stocks that is a
    zero-volume bar repeating the previous close; for assets that never report volume, a bar
    with no range at all."""
    if asset.type in VOLUME_BEARING:
        return bar.volume <= 0
    return bar.volume <= 0 and bar.high == bar.low


def sessions(bars: Sequence[Bar], asset: AssetConfig) -> list[TradingSession]:
    """The asset's real trading sessions, dated in its exchange's time zone."""
    zone = ZoneInfo(asset.timezone) if asset.timezone else UTC
    return [
        TradingSession(bar.ts.astimezone(zone).date(), bar.close)
        for bar in bars
        if not is_filler(bar, asset)
    ]


def session_on_or_after(
    trading: Sequence[TradingSession], reference: date, horizon: int
) -> TradingSession | None:
    """The `horizon`-th session on or after `reference`: a story that lands mid-session is
    judged from that session's close, and an overnight story from the next one's."""
    after = [item for item in trading if item.date >= reference]
    return after[horizon - 1] if len(after) >= horizon else None


def volatility_before(
    trading: Sequence[TradingSession], reference: date, lookback: int, min_returns: int
) -> float | None:
    """Standard deviation of daily returns over the `lookback` sessions before `reference`."""
    closes = [item.close for item in trading if item.date < reference][-(lookback + 1) :]
    returns = [(b - a) / a for a, b in zip(closes, closes[1:], strict=False) if a]
    return pstdev(returns) if len(returns) >= min_returns else None


@dataclass(frozen=True)
class Scored:
    horizon_days: int
    outcome: str
    asset_return: float | None = None
    benchmark_symbol: str | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    threshold: float | None = None


@dataclass(frozen=True)
class SymbolData:
    """Everything needed to judge one symbol's calls."""

    asset: AssetConfig
    trading: list[TradingSession]
    benchmark_symbol: str | None = None
    benchmark_trading: list[TradingSession] = field(default_factory=list)
    benchmark_reference: float | None = None  # the benchmark's price just before the news


def due_at(reference_time: datetime, horizon: int, grace_days: int) -> datetime:
    """When to stop waiting for a horizon's data and call it unscorable."""
    return reference_time + timedelta(days=horizon * 3 + grace_days)


def score_impact(
    impact: Impact, horizon: int, data: SymbolData, settings: Settings, now: datetime
) -> Scored | None:
    """Judge one call at one horizon. None means "not due yet": try again tomorrow."""
    assert impact.reference_time is not None and impact.reference_price is not None
    zone = ZoneInfo(data.asset.timezone) if data.asset.timezone else UTC
    reference_date = impact.reference_time.astimezone(zone).date()
    overdue = now > due_at(impact.reference_time, horizon, settings.scoring.score_grace_days)

    close = session_on_or_after(data.trading, reference_date, horizon)
    if close is None:
        return Scored(horizon, UNSCORABLE) if overdue else None

    asset_return = close.close / impact.reference_price - 1
    benchmark_return = None
    if data.benchmark_symbol is not None:
        benchmark_close = session_on_or_after(data.benchmark_trading, reference_date, horizon)
        if benchmark_close is None or not data.benchmark_reference:
            return Scored(horizon, UNSCORABLE) if overdue else None
        benchmark_return = benchmark_close.close / data.benchmark_reference - 1
    excess = asset_return - (benchmark_return or 0.0)

    volatility = volatility_before(
        data.trading,
        reference_date,
        settings.scoring.vol_lookback_days,
        settings.impacts.vol_min_returns,
    )
    if volatility is None:  # history before the reference can't grow: this never becomes scorable
        return Scored(
            horizon, UNSCORABLE, asset_return, data.benchmark_symbol, benchmark_return, excess
        )

    threshold = settings.scoring.hit_threshold_vol_multiple * volatility * horizon**0.5
    if abs(excess) < threshold:
        outcome = NO_MOVE
    elif (excess > 0) == (impact.direction == "up"):
        outcome = HIT
    else:
        outcome = MISS
    return Scored(
        horizon, outcome, asset_return, data.benchmark_symbol, benchmark_return, excess, threshold
    )


# ---------------------------------------------------------------- the score run


@dataclass
class ScoreReport:
    backfilled: int = 0  # impacts that finally got a reference price
    waiting_for_reference: int = 0
    scored: dict[str, int] = field(default_factory=dict)  # outcome -> count
    not_due: int = 0
    symbols: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)

    def count(self, outcome: str) -> None:
        self.scored[outcome] = self.scored.get(outcome, 0) + 1

    @property
    def total_scored(self) -> int:
        return sum(self.scored.values())


def unpriced_impacts(session: DbSession) -> list[Impact]:
    return list(session.scalars(select(Impact).where(Impact.reference_time.is_(None))))


def scorable_impacts(session: DbSession) -> list[Impact]:
    return list(session.scalars(select(Impact).where(Impact.reference_time.is_not(None))))


def existing_scores(session: DbSession) -> set[tuple[int, int]]:
    return set(session.execute(select(ImpactScore.impact_id, ImpactScore.horizon_days)).all())


def load_symbol_data(
    session: DbSession,
    symbol: str,
    assets: dict[str, AssetConfig],
    impacts: Sequence[Impact],
    provider: PriceProvider,
    now: datetime,
) -> SymbolData | None:
    """Cache-backed sessions for a symbol and, for stocks, its benchmark index."""
    asset = assets.get(symbol)
    if asset is None:
        return None
    earliest = min(impact.story.first_seen_at for impact in impacts)
    refresh_symbol(session, provider, symbol, DAILY, earliest - DAILY_LEAD, now)
    trading = sessions(cached_bars(session, symbol, DAILY, earliest - DAILY_LEAD), asset)

    benchmark_symbol = benchmark_for(asset)
    if benchmark_symbol is None or benchmark_symbol not in assets:
        return SymbolData(asset, trading)
    benchmark = assets[benchmark_symbol]
    refresh_symbol(session, provider, benchmark_symbol, DAILY, earliest - DAILY_LEAD, now)
    refresh_symbol(session, provider, benchmark_symbol, INTRADAY, earliest - INTRADAY_LEAD, now)
    benchmark_bars = cached_bars(session, benchmark_symbol, INTRADAY, earliest - INTRADAY_LEAD)
    point = reference_point(benchmark_bars, earliest)
    return SymbolData(
        asset,
        trading,
        benchmark_symbol,
        sessions(cached_bars(session, benchmark_symbol, DAILY, earliest - DAILY_LEAD), benchmark),
        point[1] if point else None,
    )


def score_impacts(
    session: DbSession,
    assets: dict[str, AssetConfig],
    provider: PriceProvider,
    settings: Settings,
    now: datetime,
    rescore: bool = False,
) -> ScoreReport:
    """Judge every call whose horizon is complete. Idempotent: an impact already scored at a
    horizon is skipped unless `rescore` is set."""
    report = ScoreReport()
    impacts = scorable_impacts(session)
    done = set() if rescore else existing_scores(session)
    by_symbol: dict[str, list[Impact]] = {}
    for impact in impacts:
        if any(
            (impact.id, horizon) not in done for horizon in settings.scoring.horizons_trading_days
        ):
            by_symbol.setdefault(impact.symbol, []).append(impact)
    report.symbols = len(by_symbol)

    for symbol, symbol_impacts in by_symbol.items():
        try:
            data = load_symbol_data(session, symbol, assets, symbol_impacts, provider, now)
        except PriceUnavailable as exc:
            report.problems.append((symbol, f"provider error: {exc}"))
            continue
        if data is None:
            report.problems.append((symbol, "not in the asset universe"))
            continue
        for impact in symbol_impacts:
            for horizon in settings.scoring.horizons_trading_days:
                if (impact.id, horizon) in done:
                    continue
                scored = score_impact(impact, horizon, data, settings, now)
                if scored is None:
                    report.not_due += 1
                    continue
                _write(session, impact, scored, now, rescore)
                report.count(scored.outcome)
        session.commit()
    return report


def _write(
    session: DbSession, impact: Impact, scored: Scored, now: datetime, rescore: bool
) -> None:
    row = session.scalar(
        select(ImpactScore).where(
            ImpactScore.impact_id == impact.id, ImpactScore.horizon_days == scored.horizon_days
        )
    )
    if row is not None and not rescore:
        return
    if row is None:
        row = ImpactScore(impact_id=impact.id, horizon_days=scored.horizon_days)
        session.add(row)
    row.asset_return = scored.asset_return
    row.benchmark_symbol = scored.benchmark_symbol
    row.benchmark_return = scored.benchmark_return
    row.excess_return = scored.excess_return
    row.threshold = scored.threshold
    row.outcome = scored.outcome
    row.scored_at = now


def mark_unreferenced(session: DbSession, settings: Settings, now: datetime) -> int:
    """Impacts that never got a reference price are unscorable once the grace period is up, so
    the daily job stops retrying them and the track record counts them."""
    marked = 0
    for impact in unpriced_impacts(session):
        if now - impact.story.first_seen_at <= timedelta(
            days=settings.scoring.reference_grace_days
        ):
            continue
        for horizon in settings.scoring.horizons_trading_days:
            _write(session, impact, Scored(horizon, UNSCORABLE), now, rescore=False)
        marked += 1
    session.commit()
    return marked


# ---------------------------------------------------------------- track record


GROUPS = ("rule_id", "event_type", "origin", "confidence", "horizon_days", "prompt_version")


@dataclass
class TrackRow:
    key: str
    horizon_days: int
    hits: int = 0
    misses: int = 0
    no_move: int = 0
    unscorable: int = 0
    stories: set[int] = field(default_factory=set)

    @property
    def judged(self) -> int:
        return self.hits + self.misses

    @property
    def rate(self) -> float | None:
        return self.hits / self.judged if self.judged else None

    @property
    def no_move_share(self) -> float | None:
        total = self.judged + self.no_move
        return self.no_move / total if total else None

    def shows_rate(self, minimum: int) -> bool:
        return self.judged >= minimum


def track_record(session: DbSession, group: str, horizon: int | None = None) -> list[TrackRow]:
    """Counts per group (SPEC 7.9). `stories` is kept because one story produces many
    correlated calls, so n is not a count of independent events."""
    if group not in GROUPS:
        raise ValueError(f"unknown track-record group {group!r}")
    query = (
        select(ImpactScore, Impact, Event.event_type, Event.prompt_version, Impact.story_id)
        .join(Impact, ImpactScore.impact_id == Impact.id)
        .join(Event, Impact.event_id == Event.id, isouter=True)
    )
    if horizon is not None:
        query = query.where(ImpactScore.horizon_days == horizon)
    rows: dict[tuple[str, int], TrackRow] = {}
    for score, impact, event_type, prompt_version, story_id in session.execute(query):
        key = {
            "rule_id": impact.rule_id or "(none)",
            "event_type": event_type or "(none)",
            "origin": impact.origin,
            "confidence": impact.confidence,
            "horizon_days": str(score.horizon_days),
            "prompt_version": prompt_version or "(none)",
        }[group]
        row = rows.setdefault((key, score.horizon_days), TrackRow(key, score.horizon_days))
        if score.outcome == HIT:
            row.hits += 1
        elif score.outcome == MISS:
            row.misses += 1
        elif score.outcome == NO_MOVE:
            row.no_move += 1
        else:
            row.unscorable += 1
        row.stories.add(story_id)
    return sorted(rows.values(), key=lambda r: (-r.judged, r.key, r.horizon_days))


def story_track_line(
    story: Story, rows: Sequence[TrackRow], minimum: int, display: dict[str, str] | None = None
) -> str | None:
    """SPEC 10: one line per story, only for a rule with enough judged calls to mean anything."""
    rules = {impact.rule_id for impact in story.impacts if impact.rule_id}
    qualifying = [row for row in rows if row.key in rules and row.shows_rate(minimum)]
    if not qualifying:
        return None
    best = max(qualifying, key=lambda row: row.judged)
    name = (display or {}).get(best.key, best.key)
    # "close" marks the window: this is the close N trading days on, not the move since news.
    return (
        f"Track record: {name} right {best.hits} of {best.judged} "
        f"({best.horizon_days}d close, {len(best.stories)} stories)"
    )
