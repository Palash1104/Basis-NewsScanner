"""Price check (SPEC 7.8): has the market already moved on a call?

For each impact:
- `reference_time` is the first 60-minute bar at or after the story's `first_seen_at`, in the
  asset's own series. Because Yahoo stamps bars in the exchange's time zone and only writes
  bars for sessions that happened, this needs no market-hours table and no holiday calendar:
  a story at 23:00 IST resolves to the next NSE morning for Indian stocks, and to the next
  hourly bar for crude, which trades through the night.
- `reference_price` is the close of the bar *before* that one: the last price before the news.
- `move_at_detection_pct` is the latest cached price against that reference.

A move is called "already moved" (or "moving against this call") when it is at least
`impacts.moved_vol_multiple` times the asset's typical daily move. That multiple is separate
from the scoring threshold in SPEC 7.9 on purpose: this one asks "is the news already in the
price", scoring asks "was the call right".

Rates are handled in points, not percent: a yield going 4.00 -> 4.10 is +0.10 points, and
calling that "+2.5%" invites misreading. Their typical move is the standard deviation of daily
*differences*, so threshold and display use the same unit.
"""

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import pstdev
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import AssetConfig, Settings
from app.models import Impact, PriceBar, Story
from app.net import backoff_seconds

log = logging.getLogger(__name__)

INTRADAY = "60m"
DAILY = "1d"
# Asset types that normally report volume; a zero-volume bar for these is an exchange holiday
# filler (seen on .NS stocks), not a session. Indices, FX and rates always report zero.
VOLUME_BEARING = frozenset({"stock", "etf", "commodity"})
# How far back to look for the bar before the reference bar, and for the volatility baseline.
INTRADAY_LEAD = timedelta(days=7)
DAILY_LEAD = timedelta(days=60)
# The newest bar of an open session keeps changing, so always re-fetch this much of the tail.
INTRADAY_OVERLAP = timedelta(days=2)
DAILY_OVERLAP = timedelta(days=5)


class PriceUnavailable(Exception):
    """No usable prices for this symbol (unknown, delisted, or the provider failed)."""


@dataclass(frozen=True)
class Bar:
    ts: datetime  # UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


class PriceProvider(Protocol):
    """Daily or intraday bars for one symbol. A broker API can replace yfinance here."""

    def bars(self, symbol: str, interval: str, start: datetime, end: datetime) -> list[Bar]: ...


class YahooPrices:
    """yfinance implementation. Retries transient failures; raises PriceUnavailable instead."""

    def __init__(
        self,
        timeout: float = 15.0,
        attempts: int = 3,
        backoff_base: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.attempts = attempts
        self.backoff_base = backoff_base
        self.sleep = sleep
        self.requests = 0

    def bars(self, symbol: str, interval: str, start: datetime, end: datetime) -> list[Bar]:
        import yfinance as yf

        yf.config.debug.hide_exceptions = False
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        for attempt in range(1, self.attempts + 1):
            self.requests += 1
            try:
                frame = yf.Ticker(symbol).history(
                    start=start.astimezone(UTC),
                    end=end.astimezone(UTC),
                    interval=interval,
                    auto_adjust=False,
                    timeout=self.timeout,
                )
                break
            except Exception as exc:
                if attempt == self.attempts:
                    raise PriceUnavailable(f"{type(exc).__name__}: {exc}") from exc
                delay = backoff_seconds(attempt, self.backoff_base)
                log.warning(
                    "prices %s %s: %s (attempt %d/%d), retrying in %.1fs",
                    symbol,
                    interval,
                    type(exc).__name__,
                    attempt,
                    self.attempts,
                    delay,
                )
                self.sleep(delay)
        return [
            Bar(
                ts=stamp.to_pydatetime().astimezone(UTC),
                open=float(row.Open),
                high=float(row.High),
                low=float(row.Low),
                close=float(row.Close),
                volume=float(row.Volume or 0),
            )
            for stamp, row in frame.iterrows()
            if row.Close == row.Close  # skip NaN closes
        ]


# ---------------------------------------------------------------- cache


def cached_bars(
    session: Session, symbol: str, interval: str, since: datetime | None = None
) -> list[Bar]:
    query = select(PriceBar).where(PriceBar.symbol == symbol, PriceBar.interval == interval)
    if since is not None:
        query = query.where(PriceBar.ts >= since)
    rows = session.scalars(query.order_by(PriceBar.ts))
    return [Bar(r.ts, r.open, r.high, r.low, r.close, r.volume) for r in rows]


def newest_cached(session: Session, symbol: str, interval: str) -> datetime | None:
    return session.scalar(
        select(PriceBar.ts)
        .where(PriceBar.symbol == symbol, PriceBar.interval == interval)
        .order_by(PriceBar.ts.desc())
        .limit(1)
    )


def store_bars(session: Session, symbol: str, interval: str, bars: Iterable[Bar]) -> int:
    """Upsert bars; the newest bar of an open session changes, so existing rows are replaced."""
    rows = [
        {
            "symbol": symbol,
            "interval": interval,
            "ts": bar.ts,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in bars
    ]
    if not rows:
        return 0
    statement = sqlite_insert(PriceBar).values(rows)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["symbol", "interval", "ts"],
            set_={
                "open": statement.excluded.open,
                "high": statement.excluded.high,
                "low": statement.excluded.low,
                "close": statement.excluded.close,
                "volume": statement.excluded.volume,
            },
        )
    )
    return len(rows)


def refresh_symbol(
    session: Session,
    provider: PriceProvider,
    symbol: str,
    interval: str,
    needed_from: datetime,
    now: datetime,
) -> int:
    """Fetch only what the cache is missing, plus an overlap so the latest bar is current.
    Returns the number of bars stored. Nothing is fetched when the cache is already current."""
    overlap = INTRADAY_OVERLAP if interval == INTRADAY else DAILY_OVERLAP
    step = timedelta(hours=1) if interval == INTRADAY else timedelta(days=1)
    newest = newest_cached(session, symbol, interval)
    if newest is not None and now - newest < step:
        return 0  # the current bar is already cached; nothing new can exist yet
    start = needed_from if newest is None else min(needed_from, newest - overlap)
    bars = provider.bars(symbol, interval, start, now + timedelta(days=1))
    return store_bars(session, symbol, interval, bars)


# ---------------------------------------------------------------- reference and moves


def reference_point(bars: Sequence[Bar], when: datetime) -> tuple[datetime, float] | None:
    """(reference_time, reference_price): the first bar at or after `when`, priced at the close
    of the bar before it. None if the series has no such bar, or none before it."""
    for index, bar in enumerate(bars):
        if bar.ts >= when:
            if index == 0:
                return None  # no "before the news" price in the series
            return bar.ts, bars[index - 1].close
    return None


def usable_daily(bars: Sequence[Bar], asset: AssetConfig) -> list[Bar]:
    """Daily bars for volatility, without exchange-holiday filler bars (volume 0 where the
    asset type normally reports volume)."""
    if asset.type not in VOLUME_BEARING:
        return list(bars)
    return [bar for bar in bars if bar.volume > 0]


def typical_move(bars: Sequence[Bar], asset: AssetConfig, min_returns: int) -> float | None:
    """The asset's typical daily move: the standard deviation of daily returns (a fraction),
    or of daily point differences for rates. None when there isn't enough history."""
    closes = [bar.close for bar in usable_daily(bars, asset)]
    if asset.type == "rate":
        changes = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    else:
        changes = [(b - a) / a for a, b in zip(closes, closes[1:], strict=False) if a]
    if len(changes) < min_returns:
        return None
    return pstdev(changes)


def move_pct(reference_price: float, latest: float) -> float:
    return (latest - reference_price) / reference_price * 100


def move_size(asset: AssetConfig, reference_price: float, pct: float) -> float:
    """The move in the unit the asset is judged and shown in: points for rates, else a
    fraction of the price."""
    return reference_price * pct / 100 if asset.type == "rate" else pct / 100


def move_label(
    asset: AssetConfig,
    direction: str,
    reference_price: float,
    pct: float,
    typical: float | None,
    multiple: float,
) -> str | None:
    """SPEC 7.8: "already moved" / "moving against this call", or None to show just the move."""
    if typical is None or typical <= 0:
        return None
    size = move_size(asset, reference_price, pct)
    if abs(size) < multiple * typical:
        return None
    moved_up = size > 0
    return "already moved" if moved_up == (direction == "up") else "moving against this call"


def move_labels(
    session: Session,
    impacts: Sequence[Impact],
    assets: dict[str, AssetConfig],
    settings: Settings,
    now: datetime,
) -> dict[int, str]:
    """ "already moved" / "moving against this call" per impact, from cached daily bars only,
    so neither the digest nor a web page ever reaches the network to label a move."""
    labels: dict[int, str] = {}
    daily: dict[str, list[Bar]] = {}
    for impact in impacts:
        if impact.symbol not in daily:
            daily[impact.symbol] = cached_bars(session, impact.symbol, DAILY, now - DAILY_LEAD)
        label = label_for(impact, assets.get(impact.symbol), daily[impact.symbol], settings)
        if label:
            labels[impact.id] = label
    return labels


def format_move(asset: AssetConfig, reference_price: float, pct: float) -> str:
    """Yields in points, everything else in percent; the unit is always written out."""
    if asset.type == "rate":
        return f"{move_size(asset, reference_price, pct):+.2f} pts"
    return f"{pct:+.1f}%"


# ---------------------------------------------------------------- the run step


@dataclass
class PriceReport:
    priced: int = 0  # impacts that now have a reference price
    refreshed: int = 0  # impacts whose move was updated
    # Impacts whose market simply hasn't opened since the story (overnight, weekend, holiday).
    # Normal, not a failure: the next run after the open prices them.
    waiting: int = 0
    unusable: list[tuple[str, str]] = field(default_factory=list)  # (symbol, reason), once each
    symbols: int = 0
    bars_stored: int = 0

    @property
    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, reason in self.unusable:
            counts[reason] = counts.get(reason, 0) + 1
        return counts


def _note(report: PriceReport, symbol: str, reason: str) -> None:
    """Record a symbol-level problem once, however many impacts it affects."""
    if (symbol, reason) not in report.unusable:
        report.unusable.append((symbol, reason))


def impacts_to_price(session: Session, since: datetime) -> list[Impact]:
    """Impacts of stories touched since `since`: new ones need a reference, and the ones headed
    for the next digest need their move kept current."""
    return list(
        session.scalars(
            select(Impact).join(Story).where(Story.updated_at > since).order_by(Impact.id)
        )
    )


def price_impacts(
    session: Session,
    impacts: Sequence[Impact],
    assets: dict[str, AssetConfig],
    provider: PriceProvider | None,
    settings: Settings,
    now: datetime,
) -> PriceReport:
    """Fill in reference prices and refresh moves. Never raises: a symbol that can't be priced
    is reported and retried on the next run. Without a provider nothing is fetched."""
    report = PriceReport()
    if not impacts or provider is None:
        return report
    stale_after = timedelta(days=settings.impacts.price_stale_days)
    by_symbol: dict[str, list[Impact]] = {}
    for impact in impacts:
        by_symbol.setdefault(impact.symbol, []).append(impact)
    report.symbols = len(by_symbol)

    for symbol, symbol_impacts in by_symbol.items():
        asset = assets.get(symbol)
        if asset is None:
            _note(report, symbol, "not in the asset universe")
            continue
        earliest = min(impact.story.first_seen_at for impact in symbol_impacts)
        try:
            report.bars_stored += refresh_symbol(
                session, provider, symbol, INTRADAY, earliest - INTRADAY_LEAD, now
            )
            report.bars_stored += refresh_symbol(
                session, provider, symbol, DAILY, now - DAILY_LEAD, now
            )
        except PriceUnavailable as exc:
            _note(report, symbol, f"provider error: {exc}")
            continue
        session.commit()

        # Daily bars are cached by the refresh above; the digest reads them for labels.
        intraday = cached_bars(session, symbol, INTRADAY, earliest - INTRADAY_LEAD)
        if not intraday:
            _note(report, symbol, "no intraday bars")
            continue
        if now - intraday[-1].ts > stale_after:
            _note(report, symbol, "latest bar is stale")
            continue
        latest = intraday[-1].close

        newest = intraday[-1].ts
        for impact in symbol_impacts:
            if impact.reference_time is None:
                point = reference_point(intraday, impact.story.first_seen_at)
                if point is None:
                    if impact.story.first_seen_at > newest:
                        report.waiting += 1  # the market hasn't opened since the story
                    else:
                        _note(report, symbol, "no price before the story")
                    continue
                impact.reference_time, impact.reference_price = point
                report.priced += 1
            elif impact.reference_price is None:
                continue
            impact.move_at_detection_pct = move_pct(impact.reference_price, latest)
            report.refreshed += 1
        session.commit()
    return report


def label_for(
    impact: Impact, asset: AssetConfig | None, daily: Sequence[Bar], settings: Settings
) -> str | None:
    if asset is None or impact.reference_price is None or impact.move_at_detection_pct is None:
        return None
    typical = typical_move(daily, asset, settings.impacts.vol_min_returns)
    return move_label(
        asset,
        impact.direction,
        impact.reference_price,
        impact.move_at_detection_pct,
        typical,
        settings.impacts.moved_vol_multiple,
    )
