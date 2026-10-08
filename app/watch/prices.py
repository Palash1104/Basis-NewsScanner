"""Intraday price polls for the watchlist, in NSE market hours.

Step 2 only records them: how far behind Yahoo runs, and how far each stock and its peers
move in a day, which the "moved, no story yet" alert will be tuned on. Measured 2026-10-08,
11:26-11:33 IST: the current minute's 1-minute bar was always there, and Yahoo's last trade
was 1-5 s old for RELIANCE and HAL, 3-16 s for ASTRAMICRO (thinner trading).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from app.config import Settings
from app.models import WatchPrice

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    price: float | None
    previous_close: float | None
    last_trade_at: datetime | None
    newest_bar_at: datetime | None
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None


class SnapshotProvider(Protocol):
    def snapshot(self, symbol: str) -> Snapshot:
        """The latest price and today's bars so far; raises on failure."""
        ...


def _utc(value: Any) -> datetime | None:
    """yfinance gives times as pandas Timestamps or epoch seconds, depending on the field."""
    if value is None:
        return None
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().astimezone(UTC)
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromtimestamp(int(value), UTC)


class YahooSnapshots:
    """One chart request per symbol: today's 1-minute bars, and in the same answer Yahoo's
    metadata, which carries the last trade's time and the previous close."""

    def snapshot(self, symbol: str) -> Snapshot:
        import yfinance as yf  # heavy import: only when prices are polled

        ticker = yf.Ticker(symbol)
        frame = ticker.history(period="1d", interval="1m", auto_adjust=False)
        meta = ticker.history_metadata or {}
        price = meta.get("regularMarketPrice")
        previous = meta.get("chartPreviousClose") or meta.get("previousClose")
        if frame.empty:
            return Snapshot(symbol, price, previous, _utc(meta.get("regularMarketTime")), None)
        return Snapshot(
            symbol,
            float(price) if price is not None else float(frame["Close"].iloc[-1]),
            float(previous) if previous is not None else None,
            _utc(meta.get("regularMarketTime")),
            _utc(frame.index[-1]),
            float(frame["Open"].iloc[0]),
            float(frame["High"].max()),
            float(frame["Low"].min()),
        )


class HistoryProvider(Protocol):
    """Bars for the past, for catching up and for thresholds. Both raise on failure."""

    def minute_closes(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        """(bar start in UTC, close) for each 1-minute bar in [start, end)."""
        ...

    def session_closes(self, symbol: str, days: int) -> dict[date, float]:
        """Each session's close over the last `days` days, by session date (exchange time)."""
        ...

    def hourly_bars(self, symbol: str, days: int) -> list[tuple[datetime, float, float, float]]:
        """(bar start in UTC, high, low, close) for each hourly bar over the last `days` days:
        what each stock's typical day is measured from (app/watch/moves.py)."""
        ...


# Yahoo keeps 1-minute bars for the last 30 days and serves at most 8 days of them per
# request (both verified 2026-10-08: "The requested range must be within the last 30 days").
MINUTE_HISTORY = timedelta(days=29)
MINUTE_CHUNK = timedelta(days=7)


class YahooHistory:
    def minute_closes(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        import yfinance as yf

        found: list[tuple[datetime, float]] = []
        cursor = start
        while cursor < end:
            stop = min(cursor + MINUTE_CHUNK, end)
            frame = yf.Ticker(symbol).history(
                start=cursor, end=stop, interval="1m", auto_adjust=False
            )
            for stamp, close in zip(frame.index, frame["Close"], strict=True):
                found.append((_utc(stamp), float(close)))  # type: ignore[arg-type]
            cursor = stop
        return found

    def session_closes(self, symbol: str, days: int) -> dict[date, float]:
        """From hourly bars, not daily ones: Yahoo has no daily history for
        NIFTY_IND_DEFENCE.NS (only today's bar), but hourly bars back to Nov 2024. A
        session's close is its last hourly bar's close, so holidays - no hourly bars - are
        never sessions, the filler-bar problem of NSE daily data included."""
        import yfinance as yf

        frame = yf.Ticker(symbol).history(period=f"{days}d", interval="60m", auto_adjust=False)
        closes: dict[date, float] = {}
        for stamp, close in zip(frame.index, frame["Close"], strict=True):
            closes[stamp.date()] = float(close)  # the last bar of each day wins
        return closes

    def hourly_bars(self, symbol: str, days: int) -> list[tuple[datetime, float, float, float]]:
        import yfinance as yf

        frame = yf.Ticker(symbol).history(period=f"{days}d", interval="60m", auto_adjust=False)
        return [
            (_utc(stamp), float(high), float(low), float(close))  # type: ignore[misc]
            for stamp, high, low, close in zip(
                frame.index, frame["High"], frame["Low"], frame["Close"], strict=True
            )
        ]


def previous_close(closes: dict[date, float], day: date) -> float | None:
    """The close of the last session before `day`."""
    earlier = [d for d in closes if d < day]
    return closes[max(earlier)] if earlier else None


def backfill_rows(
    symbol: str,
    bars: Sequence[tuple[datetime, float]],
    closes: dict[date, float],
    every_minutes: int,
    settings: Settings,
) -> list[WatchPrice]:
    """Price rows for missed sessions, shaped like the live polls: one every `every_minutes`
    (and the session's last bar), each against the previous session's close, with the day's
    high and low so far. A row's time is the end of its bar, when that price was known."""
    tz = settings.tz
    by_day: dict[date, list[tuple[datetime, float]]] = {}
    for start, close in sorted(bars):
        by_day.setdefault(start.astimezone(tz).date(), []).append((start, close))
    rows: list[WatchPrice] = []
    for day, day_bars in by_day.items():
        previous = previous_close(closes, day)
        high = low = day_bars[0][1]
        for index, (start, close) in enumerate(day_bars):
            high, low = max(high, close), min(low, close)
            last = index == len(day_bars) - 1
            if start.astimezone(tz).minute % every_minutes and not last:
                continue
            rows.append(
                WatchPrice(
                    symbol=symbol,
                    polled_at=start + timedelta(minutes=1),
                    price=close,
                    previous_close=previous,
                    day_open=day_bars[0][1],
                    day_high=high,
                    day_low=low,
                    backfill=True,
                )
            )
    return rows


def _clock(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def in_market_hours(now: datetime, settings: Settings) -> bool:
    """Monday to Friday, between the open and a few minutes after the close (so the closing
    prints are caught), in the exchange's time. Exchange holidays are not known here: a poll
    on one just records a price that didn't move."""
    local = now.astimezone(settings.tz)
    if local.weekday() >= 5:
        return False
    opens = _clock(settings.watch.market_open)
    closes = (
        datetime.combine(local.date(), _clock(settings.watch.market_close)) + timedelta(minutes=5)
    ).time()
    return opens <= local.time() <= closes


def price_symbols(
    stocks: Sequence[str], groups_indices: Sequence[str], benchmark: str
) -> list[str]:
    """The stocks, their groups' indices and the benchmark, each once, in that order."""
    return list(dict.fromkeys([*stocks, *groups_indices, benchmark]))
