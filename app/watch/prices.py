"""Intraday price polls for the watchlist, in NSE market hours.

Step 2 only records them: how far behind Yahoo runs, and how far each stock and its peers
move in a day, which the "moved, no story yet" alert will be tuned on. Measured 2026-10-08,
11:26-11:33 IST: the current minute's 1-minute bar was always there, and Yahoo's last trade
was 1-5 s old for RELIANCE and HAL, 3-16 s for ASTRAMICRO (thinner trading).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol

from app.config import Settings

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
