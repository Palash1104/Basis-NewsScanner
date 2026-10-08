"""Scoring the watchlist's calls, and the lead-time report (step 6).

Which calls: the first positive or negative call on each stock in each story - the call as it
was made when the news broke, not a later re-call. Neutral calls and passing mentions make no
claim to judge.

From when: the first hourly bar at or after BASIS *first saw* the story, priced at the close
of the bar before it (Phase 3's reference rule). So "it had already moved" means already
moved when BASIS knew, not when the publisher stamped it.

Against what: the Nifty for every stock, and also the stock's group index for a defence name
(NIFTY_IND_DEFENCE.NS), so a sector rally can't make every positive call look right (user,
2026-10-08). Each benchmark is its own row and its own track record.

On what: daily closes built from hourly bars - the last hourly close of each session, dated
in the exchange's time. Yahoo has no daily history for NIFTY_IND_DEFENCE.NS (only today's
bar; hourly bars go back to November 2024), and building every series the same way keeps the
stock and its benchmarks on exactly the same sessions. Holidays have no hourly bars, so they
are never sessions - no filler-bar problem.

Then Phase 4's rule: at horizon N (the Nth session on or after the reference session), the
excess return over the benchmark against half the stock's daily volatility (measured before
the reference) times the square root of N - hit, miss or no move.
"""

import logging
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, WatchlistFile
from app.models import WatchArticle, WatchCall, WatchRun, WatchScore, WatchStory
from app.pipeline.prices import (
    INTRADAY,
    Bar,
    PriceProvider,
    PriceUnavailable,
    cached_bars,
    reference_point,
    refresh_symbol,
)
from app.pipeline.scoring import (
    HIT,
    MISS,
    NO_MOVE,
    UNSCORABLE,
    TrackRow,
    TradingSession,
    due_at,
    session_on_or_after,
    volatility_before,
)

log = logging.getLogger(__name__)

NSE = ZoneInfo("Asia/Kolkata")
HISTORY = timedelta(days=45)  # hourly bars before the earliest call: 20+ sessions of volatility
GROUPS = ("event_type", "materiality", "first_source", "lead")


def daily_sessions(bars: Sequence[Bar], zone: ZoneInfo = NSE) -> list[TradingSession]:
    """One session per exchange-local date that has hourly bars, closed at its last bar."""
    closes: dict = {}
    for bar in sorted(bars, key=lambda b: b.ts):
        closes[bar.ts.astimezone(zone).date()] = bar.close
    return [TradingSession(day, close) for day, close in sorted(closes.items())]


def scorable_calls(session: Session) -> list[WatchCall]:
    """The first directional call on each stock in each story."""
    first: dict[tuple[int, str], WatchCall] = {}
    for call in session.scalars(
        select(WatchCall)
        .where(WatchCall.sentiment != "neutral", WatchCall.relevance != "passing")
        .order_by(WatchCall.created_at, WatchCall.id)
    ):
        first.setdefault((call.story_id, call.symbol), call)
    return list(first.values())


@dataclass
class WatchScoreReport:
    scored: dict[str, int] = field(default_factory=dict)
    not_due: int = 0
    calls: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.scored.values())


def benchmarks_for(symbol: str, watchlist: WatchlistFile, settings: Settings) -> list[str]:
    """The Nifty, then the stock's group index when it has one."""
    found = [settings.watch.benchmark]
    stock = next((s for s in watchlist.stocks if s.symbol == symbol), None)
    group = watchlist.groups.get(stock.group or "") if stock else None
    if group and group.index and group.index not in found:
        found.append(group.index)
    return found


def score_watch_calls(
    session: Session,
    provider: PriceProvider,
    watchlist: WatchlistFile,
    settings: Settings,
    now: datetime,
    rescore: bool = False,
) -> WatchScoreReport:
    """Judge every watchlist call whose horizon is complete. Idempotent: a (call, horizon,
    benchmark) already scored is skipped unless `rescore`."""
    report = WatchScoreReport()
    calls = scorable_calls(session)
    report.calls = len(calls)
    if not calls:
        return report
    done = (
        set()
        if rescore
        else set(
            session.execute(
                select(WatchScore.call_id, WatchScore.horizon_days, WatchScore.benchmark_symbol)
            ).all()
        )
    )
    seen_at = dict(
        session.execute(
            select(WatchStory.id, WatchStory.first_seen_at).where(
                WatchStory.id.in_({c.story_id for c in calls})
            )
        ).all()
    )
    horizons = settings.scoring.horizons_trading_days
    wanted = [
        call
        for call in calls
        if any(
            (call.id, h, b) not in done
            for h in horizons
            for b in benchmarks_for(call.symbol, watchlist, settings)
        )
    ]
    if not wanted:
        return report
    earliest = min(seen_at[call.story_id] for call in wanted) - HISTORY
    symbols = sorted(
        {c.symbol for c in wanted}
        | {b for c in wanted for b in benchmarks_for(c.symbol, watchlist, settings)}
    )
    bars: dict[str, list[Bar]] = {}
    for symbol in symbols:
        try:
            refresh_symbol(session, provider, symbol, INTRADAY, earliest, now)
        except PriceUnavailable as exc:
            report.problems.append((symbol, f"provider error: {exc}"))
        bars[symbol] = cached_bars(session, symbol, INTRADAY, earliest)
    session.commit()
    trading = {symbol: daily_sessions(series) for symbol, series in bars.items()}

    for call in wanted:
        seen = seen_at[call.story_id]
        point = reference_point(bars.get(call.symbol, []), seen)
        for benchmark in benchmarks_for(call.symbol, watchlist, settings):
            for horizon in horizons:
                if (call.id, horizon, benchmark) in done:
                    continue
                scored = _judge(call, seen, point, benchmark, horizon, bars, trading, settings, now)
                if scored is None:
                    report.not_due += 1
                    continue
                _write(session, scored, now, rescore)
                report.scored[scored.outcome] = report.scored.get(scored.outcome, 0) + 1
    session.commit()
    return report


def _judge(
    call: WatchCall,
    seen: datetime,
    point: tuple[datetime, float] | None,
    benchmark: str,
    horizon: int,
    bars: dict[str, list[Bar]],
    trading: dict[str, list[TradingSession]],
    settings: Settings,
    now: datetime,
) -> WatchScore | None:
    def row(outcome: str, **values: float | datetime | None) -> WatchScore:
        return WatchScore(
            call_id=call.id,
            horizon_days=horizon,
            benchmark_symbol=benchmark,
            outcome=outcome,
            **values,
        )

    if point is None:
        # No hourly bar after the story yet (or none before it): wait, then give up.
        if now - seen > timedelta(days=settings.scoring.reference_grace_days):
            return row(UNSCORABLE)
        return None
    reference_time, reference_price = point
    reference_date = reference_time.astimezone(NSE).date()
    overdue = now > due_at(reference_time, horizon, settings.scoring.score_grace_days)
    close = session_on_or_after(trading.get(call.symbol, []), reference_date, horizon)
    bench_point = reference_point(bars.get(benchmark, []), seen)
    bench_close = session_on_or_after(trading.get(benchmark, []), reference_date, horizon)
    if close is None or bench_point is None or bench_close is None:
        return (
            row(UNSCORABLE, reference_time=reference_time, reference_price=reference_price)
            if overdue
            else None
        )
    asset_return = close.close / reference_price - 1
    benchmark_return = bench_close.close / bench_point[1] - 1
    excess = asset_return - benchmark_return
    volatility = volatility_before(
        trading[call.symbol],
        reference_date,
        settings.scoring.vol_lookback_days,
        settings.impacts.vol_min_returns,
    )
    values = {
        "reference_time": reference_time,
        "reference_price": reference_price,
        "asset_return": asset_return,
        "benchmark_return": benchmark_return,
        "excess_return": excess,
    }
    if volatility is None:
        return row(UNSCORABLE, **values)
    threshold = settings.scoring.hit_threshold_vol_multiple * volatility * horizon**0.5
    if abs(excess) < threshold:
        outcome = NO_MOVE
    elif (excess > 0) == (call.sentiment == "positive"):
        outcome = HIT
    else:
        outcome = MISS
    return row(outcome, threshold=threshold, **values)


def _write(session: Session, scored: WatchScore, now: datetime, rescore: bool) -> None:
    existing = session.scalar(
        select(WatchScore).where(
            WatchScore.call_id == scored.call_id,
            WatchScore.horizon_days == scored.horizon_days,
            WatchScore.benchmark_symbol == scored.benchmark_symbol,
        )
    )
    if existing is not None and not rescore:
        return
    if existing is not None:
        session.delete(existing)
        session.flush()
    scored.scored_at = now
    session.add(scored)


# ---------------------------------------------------------------- where a story came from


@dataclass(frozen=True)
class Arrival:
    """How one story reached BASIS: who had it first, and the media's lead over the filing."""

    first_source: str
    first_at: datetime
    lead: str  # the bucket: "no filing", "filing first", "media first by under an hour"...
    lead_seconds: float | None  # media first -> the first filing; None without both


def lead_bucket(seconds: float | None, has_media: bool, has_filing: bool) -> str:
    if not has_filing:
        return "no filing (media only)"
    if not has_media:
        return "filing only"
    if seconds is None or seconds <= 0:
        return "filing first"
    if seconds < 3600:
        return "media first by under an hour"
    if seconds < 6 * 3600:
        return "media first by 1-6 hours"
    return "media first by over 6 hours"


def arrival(story: WatchStory) -> Arrival:
    media = sorted(story.articles, key=lambda a: a.first_seen_at)
    filings = sorted(story.filings, key=lambda f: f.filed_at)
    candidates = [(a.first_seen_at, a.source_name) for a in media] + [
        (f.filed_at, f"{f.exchange} filing") for f in filings
    ]
    first_at, first_source = min(candidates) if candidates else (story.first_seen_at, "?")
    seconds = (
        (filings[0].filed_at - media[0].first_seen_at).total_seconds()
        if media and filings
        else None
    )
    return Arrival(
        first_source, first_at, lead_bucket(seconds, bool(media), bool(filings)), seconds
    )


def _stories(session: Session, ids: Sequence[int] | None = None, since: datetime | None = None):
    query = select(WatchStory).options(
        selectinload(WatchStory.articles).selectinload(WatchArticle.sightings),
        selectinload(WatchStory.articles).selectinload(WatchArticle.matches),
        selectinload(WatchStory.filings),
    )
    if ids is not None:
        query = query.where(WatchStory.id.in_(ids))
    if since is not None:
        query = query.where(WatchStory.first_seen_at >= since)
    return list(session.scalars(query))


@dataclass
class WatchTrackRow(TrackRow):
    benchmark: str = ""


def watch_track_record(
    session: Session, group: str, horizon: int | None = None
) -> list[WatchTrackRow]:
    """Counts per group and benchmark, with the stories behind them (one story's calls rise
    and fall together, so n is not independent events)."""
    if group not in GROUPS:
        raise ValueError(f"unknown watchlist track-record group {group!r}")
    query = select(WatchScore, WatchCall).join(WatchCall, WatchScore.call_id == WatchCall.id)
    if horizon is not None:
        query = query.where(WatchScore.horizon_days == horizon)
    pairs = session.execute(query).all()
    arrivals = {s.id: arrival(s) for s in _stories(session, ids={c.story_id for _, c in pairs})}
    rows: dict[tuple[str, int, str], WatchTrackRow] = {}
    for score, call in pairs:
        how = arrivals.get(call.story_id)
        key = {
            "event_type": call.event_type.replace("_", " "),
            "materiality": call.materiality,
            "first_source": how.first_source if how else "?",
            "lead": how.lead if how else "?",
        }[group]
        row = rows.setdefault(
            (key, score.horizon_days, score.benchmark_symbol),
            WatchTrackRow(key, score.horizon_days, benchmark=score.benchmark_symbol),
        )
        if score.outcome == HIT:
            row.hits += 1
        elif score.outcome == MISS:
            row.misses += 1
        elif score.outcome == NO_MOVE:
            row.no_move += 1
        else:
            row.unscorable += 1
        row.stories.add(call.story_id)
    return sorted(
        rows.values(), key=lambda r: (r.benchmark != "^NSEI", -r.judged, r.key, r.horizon_days)
    )


# ---------------------------------------------------------------- the lead-time report


@dataclass
class LeadRow:
    source: str
    stories: int = 0  # stories this source carried
    first: int = 0  # ...that it had before anyone else
    leads: list[float] = field(default_factory=list)  # seconds ahead of the filing, when first
    lags: list[float] = field(default_factory=list)  # seconds behind the first source

    @property
    def median_lead(self) -> float | None:
        return statistics.median(self.leads) if self.leads else None

    @property
    def median_lag(self) -> float | None:
        return statistics.median(self.lags) if self.lags else None


def backlog_times(session: Session, settings: Settings, since: datetime) -> set[datetime]:
    """Pass times that read a backlog (the first after a start or a gap, and catch-ups):
    what they "first saw" had been out for a while, so it says nothing about who was first."""
    runs = session.scalars(
        select(WatchRun)
        .where(WatchRun.started_at >= since - timedelta(days=1))
        .order_by(WatchRun.started_at)
    ).all()
    every = {
        "feeds": timedelta(minutes=settings.watch.feeds_every_minutes),
        "google_news": timedelta(minutes=settings.watch.google_news_quiet_every_minutes),
    }
    found: set[datetime] = set()
    last: dict[str, datetime] = {}
    for run in runs:
        if run.job == "catchup":
            found.add(run.started_at)
        if run.job not in every:
            continue
        previous = last.get(run.job)
        if previous is None or run.started_at - previous > 2 * every[run.job]:
            found.add(run.started_at)
        last[run.job] = run.started_at
    return found


def lead_times(
    session: Session, settings: Settings, now: datetime, days: int = 30
) -> tuple[list[LeadRow], int, int]:
    """Per source: how often it had the story first, its median lead over the exchange filing
    when it did, and its median lag behind whoever was first. Returns (rows, stories counted,
    stories left out as backlog)."""
    since = now - timedelta(days=days)
    backlog = backlog_times(session, settings, since)
    rows: dict[str, LeadRow] = {}
    counted = skipped = 0
    for story in _stories(session, since=since):
        kept = [a for a in story.articles if any(m.verdict == "keep" for m in a.matches)]
        if not kept and not story.filings:
            continue
        times = sorted(
            [(a.first_seen_at, a.source_name) for a in kept]
            + [(f.filed_at, f"{f.exchange} filing") for f in story.filings]
        )
        if story.first_seen_at in backlog:
            skipped += 1
            continue
        counted += 1
        first_at, first_source = times[0]
        filing_at = min((f.filed_at for f in story.filings), default=None)
        seen_sources: set[str] = set()
        for at, source in times:
            if source in seen_sources:
                continue
            seen_sources.add(source)
            row = rows.setdefault(source, LeadRow(source))
            row.stories += 1
            if source == first_source and at == first_at:
                row.first += 1
                if filing_at is not None and not source.endswith("filing") and filing_at > at:
                    row.leads.append((filing_at - at).total_seconds())
            else:
                row.lags.append((at - first_at).total_seconds())
    ordered = sorted(rows.values(), key=lambda r: (-r.first, -r.stories, r.source))
    return ordered, counted, skipped
