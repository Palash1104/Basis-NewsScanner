"""The asset universe in use (SPEC §8): ticker validation against Yahoo Finance, the startup
warning for symbols that aren't validated, and each asset's scoring benchmark (SPEC 7.9).

Validation downloads 5 days of daily prices per symbol. Yahoo's metadata from the same request
(name, currency, exchange, instrument type) is compared with assets.yaml, and anything that
looks off is flagged for a person to review; flags are not failures. Failing symbols are only
reported: nothing here ever substitutes a different symbol.
"""

import logging
import re
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import AssetConfig
from app.models import TickerCheck
from app.net import backoff_seconds

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(days=7)  # a latest daily bar older than this means stale data
MAX_CHECK_AGE = timedelta(days=30)  # warn when a symbol's last check is older than this
NAME_MATCH_MIN = 70.0  # rapidfuzz token_set_ratio between our name and Yahoo's

# Yahoo instrument types expected per asset type; anything else is flagged for review.
EXPECTED_INSTRUMENT_TYPES: dict[str, frozenset[str]] = {
    "commodity": frozenset({"FUTURE"}),
    "fx": frozenset({"CURRENCY", "INDEX", "FUTURE"}),
    "rate": frozenset({"INDEX"}),
    "index": frozenset({"INDEX"}),
    "stock": frozenset({"EQUITY"}),
    "etf": frozenset({"ETF"}),
}

# Scoring benchmarks (SPEC 7.9) for stocks, chosen by the exchange they trade on. Keys are
# Yahoo's exchange codes as reported by validate-tickers on 2026-09-19 for the stocks in the
# universe: NSI = NSE, NYQ = NYSE, NMS = Nasdaq Global Select. A stock on any other exchange
# gets no benchmark until its code is seen in a report and added here. Indices, commodities,
# FX, rates and ETFs have no benchmark.
EXCHANGE_BENCHMARKS: dict[str, str] = {"NSI": "^NSEI", "NYQ": "^GSPC", "NMS": "^GSPC"}

_NAME_NOISE = re.compile(
    r"\b(the|ltd|limited|inc|incorporated|corp|corporation|co|company|plc|holdings|class [a-z])\b"
)


class NoPriceData(Exception):
    """Yahoo has no prices for the symbol (unknown or delisted)."""


@dataclass(frozen=True)
class PriceSample:
    # (bar time, close), oldest first, no NaNs. Bar times stay in the exchange's time zone:
    # Yahoo stamps daily bars at local midnight, so Friday's NSE bar is 00:00 IST on Friday,
    # which is Thursday 18:30 in UTC. Dates must be taken in local time, never after UTC
    # conversion.
    closes: list[tuple[datetime, float]]
    metadata: dict[str, Any]


Fetch = Callable[[str], PriceSample]


def yahoo_fetch(timeout: float) -> Fetch:
    """5 days of daily bars plus Yahoo's metadata for a symbol, via yfinance (one request)."""
    import yfinance as yf
    from yfinance.exceptions import YFPricesMissingError, YFTickerMissingError

    yf.config.debug.hide_exceptions = False  # raise instead of printing and returning nothing
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # errors are reported by us

    def fetch(symbol: str) -> PriceSample:
        ticker = yf.Ticker(symbol)
        try:
            frame = ticker.history(period="5d", interval="1d", auto_adjust=False, timeout=timeout)
        except (YFPricesMissingError, YFTickerMissingError) as exc:
            raise NoPriceData(str(exc)) from exc
        closes = (
            [
                (stamp.to_pydatetime(), float(close))
                for stamp, close in frame["Close"].dropna().items()
            ]
            if "Close" in frame
            else []
        )
        return PriceSample(closes, dict(ticker.get_history_metadata() or {}))

    return fetch


@dataclass
class TickerResult:
    symbol: str
    status: str  # ok | empty | stale | error
    rows: int = 0
    last_bar_at: datetime | None = None  # in the exchange's time zone (see PriceSample)
    last_close: float | None = None
    yahoo_name: str | None = None
    currency: str | None = None
    exchange: str | None = None
    timezone: str | None = None
    instrument_type: str | None = None
    error: str | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _clean_name(name: str) -> str:
    text = re.sub(r"[^a-z0-9& ]+", " ", name.lower())
    return " ".join(_NAME_NOISE.sub(" ", text).split())


def name_similarity(asset: AssetConfig, yahoo_names: Sequence[str]) -> float:
    ours = [_clean_name(asset.name), _clean_name(asset.display_name)]
    theirs = [_clean_name(name) for name in yahoo_names if name]
    return max((fuzz.token_set_ratio(a, b) for a in ours for b in theirs if a and b), default=0.0)


def review_flags(asset: AssetConfig, result: TickerResult, metadata: dict[str, Any]) -> list[str]:
    """Things a person should look at; they don't fail the check."""
    flags = []
    yahoo_names = [metadata.get("longName") or "", metadata.get("shortName") or ""]
    approved = {name.casefold() for name in asset.approved_yahoo_names}
    if (
        any(yahoo_names)
        and not approved & {name.casefold() for name in yahoo_names if name}
        and name_similarity(asset, yahoo_names) < NAME_MATCH_MIN
    ):
        flags.append(f"name: Yahoo calls it {result.yahoo_name!r}")
    if not any(yahoo_names):
        flags.append("name: Yahoo returned no name")
    expected = EXPECTED_INSTRUMENT_TYPES[asset.type]
    if result.instrument_type and result.instrument_type not in expected:
        flags.append(f"type: Yahoo says {result.instrument_type}, expected {'/'.join(expected)}")
    if asset.currency and result.currency and asset.currency != result.currency:
        flags.append(f"currency: assets.yaml {asset.currency}, Yahoo {result.currency}")
    if asset.exchange and result.exchange and asset.exchange != result.exchange:
        flags.append(f"exchange: assets.yaml {asset.exchange}, Yahoo {result.exchange}")
    if asset.timezone and result.timezone and asset.timezone != result.timezone:
        flags.append(f"timezone: assets.yaml {asset.timezone}, Yahoo {result.timezone}")
    if not result.currency or not result.exchange:
        flags.append("metadata: Yahoo returned no currency or exchange")
    return flags


def check_ticker(
    asset: AssetConfig,
    fetch: Fetch,
    now: datetime,
    attempts: int = 3,
    backoff_base: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> TickerResult:
    """Check one symbol, retrying failed requests (including "no data", which Yahoo sometimes
    returns transiently). Never raises."""
    for attempt in range(1, attempts + 1):
        try:
            sample = fetch(asset.symbol)
            break
        except Exception as exc:  # every failure is reported; none may crash the check
            if attempt == attempts:
                status = "empty" if isinstance(exc, NoPriceData) else "error"
                return TickerResult(asset.symbol, status, error=f"{type(exc).__name__}: {exc}")
            delay = backoff_seconds(attempt, backoff_base)
            log.warning(
                "ticker %s: %s (attempt %d/%d), retrying in %.1fs",
                asset.symbol,
                type(exc).__name__,
                attempt,
                attempts,
                delay,
            )
            sleep(delay)

    meta = sample.metadata
    result = TickerResult(
        asset.symbol,
        "ok",
        rows=len(sample.closes),
        yahoo_name=meta.get("longName") or meta.get("shortName"),
        currency=meta.get("currency"),
        exchange=meta.get("exchangeName"),
        timezone=meta.get("exchangeTimezoneName"),
        instrument_type=meta.get("instrumentType"),
    )
    if not sample.closes:
        result.status = "empty"
        result.error = "no daily closes in the last 5 days"
    else:
        result.last_bar_at, result.last_close = sample.closes[-1]
        if now - result.last_bar_at > STALE_AFTER:
            result.status = "stale"
            result.error = f"latest bar is {(now - result.last_bar_at).days} days old"
    result.flags = review_flags(asset, result, meta)
    return result


def validate_assets(
    assets: Sequence[AssetConfig],
    fetch: Fetch,
    now: datetime,
    workers: int = 4,
    sleep: Callable[[float], None] = time.sleep,
) -> list[TickerResult]:
    """Check every asset (a few at a time); results are in the order of `assets`."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda asset: check_ticker(asset, fetch, now, sleep=sleep), assets))


def record_checks(session: Session, results: Sequence[TickerResult], now: datetime) -> None:
    session.add_all(
        TickerCheck(
            symbol=result.symbol,
            checked_at=now,
            status=result.status,
            rows=result.rows,
            last_bar_at=result.last_bar_at,
            last_close=result.last_close,
            yahoo_name=result.yahoo_name,
            currency=result.currency,
            exchange=result.exchange,
            timezone=result.timezone,
            instrument_type=result.instrument_type,
            error=result.error,
            flags=result.flags,
        )
        for result in results
    )


def latest_checks(session: Session) -> dict[str, TickerCheck]:
    """The most recent check of each symbol."""
    newest = (
        select(TickerCheck.symbol, func.max(TickerCheck.checked_at).label("checked_at"))
        .group_by(TickerCheck.symbol)
        .subquery()
    )
    rows = session.scalars(
        select(TickerCheck).join(
            newest,
            (TickerCheck.symbol == newest.c.symbol)
            & (TickerCheck.checked_at == newest.c.checked_at),
        )
    )
    return {row.symbol: row for row in rows}


@dataclass(frozen=True)
class ValidationGaps:
    never: list[str]
    failed: list[str]
    outdated: list[str]  # last check older than MAX_CHECK_AGE

    @property
    def symbols(self) -> list[str]:
        return [*self.never, *self.failed, *self.outdated]


def validation_gaps(
    session: Session, assets: Sequence[AssetConfig], now: datetime
) -> ValidationGaps:
    latest = latest_checks(session)
    never, failed, outdated = [], [], []
    for asset in assets:
        check = latest.get(asset.symbol)
        if check is None:
            never.append(asset.symbol)
        elif check.status != "ok":
            failed.append(asset.symbol)
        elif now - check.checked_at > MAX_CHECK_AGE:
            outdated.append(asset.symbol)
    return ValidationGaps(never, failed, outdated)


def validation_warning(gaps: ValidationGaps, total: int) -> str | None:
    """One line for run output and logs, or None when every symbol is validated."""
    if not gaps.symbols:
        return None
    parts = []
    if gaps.never:
        parts.append(f"never validated: {', '.join(gaps.never)}")
    if gaps.failed:
        parts.append(f"failed the last check: {', '.join(gaps.failed)}")
    if gaps.outdated:
        parts.append(
            f"last validated over {MAX_CHECK_AGE.days} days ago: {', '.join(gaps.outdated)}"
        )
    return (
        f"asset universe: {len(gaps.symbols)} of {total} symbols not currently validated "
        f"({'; '.join(parts)}). Run `uv run newsdesk validate-tickers`."
    )


def benchmark_for(asset: AssetConfig) -> str | None:
    """SPEC 7.9: NSE stocks against the Nifty 50, US-listed stocks against the S&P 500, chosen
    by exchange (TSM is a Taiwanese company listed in New York, so it gets the S&P 500).
    Everything else has no benchmark."""
    if asset.type != "stock" or asset.exchange is None:
        return None
    return EXCHANGE_BENCHMARKS.get(asset.exchange)


# ---------------------------------------------------------------- report

_STATUS_ORDER = {"error": 0, "empty": 1, "stale": 2, "ok": 3}


def report_rows(
    results: Sequence[TickerResult], rules_by_symbol: dict[str, list[str]] | None = None
) -> list[list[str]]:
    """Failures first, then flagged, then clean, each in universe order."""
    ordered = sorted(
        enumerate(results),
        key=lambda item: (_STATUS_ORDER[item[1].status], not item[1].flags, item[0]),
    )
    rows = []
    for _, result in ordered:
        row = [
            result.symbol,
            result.status,
            result.last_bar_at.date().isoformat() if result.last_bar_at else "",
            f"{result.last_close:,.2f}" if result.last_close is not None else "",
            result.yahoo_name or "",
            result.currency or "",
            result.exchange or "",
            result.timezone or "",
            result.instrument_type or "",
            "; ".join(filter(None, [result.error, *result.flags])),
        ]
        if rules_by_symbol is not None:
            row.append(", ".join(rules_by_symbol.get(result.symbol, [])))
        rows.append(row)
    return rows


REPORT_HEADER = [
    "symbol",
    "status",
    "last bar",
    "close",
    "Yahoo name",
    "ccy",
    "exchange",
    "time zone",
    "Yahoo type",
    "notes",
]


def markdown_report(
    results: Sequence[TickerResult],
    checked_at: datetime,
    rules_by_symbol: dict[str, list[str]] | None = None,
) -> str:
    header = [*REPORT_HEADER, *(["used by rules"] if rules_by_symbol is not None else [])]
    failed = [r for r in results if not r.ok]
    flagged = [r for r in results if r.ok and r.flags]
    lines = [
        "# Ticker validation",
        "",
        f"Checked {checked_at.strftime('%Y-%m-%d %H:%M')} UTC: {len(results)} symbols, "
        f"{len(results) - len(failed)} ok, {len(failed)} failed, {len(flagged)} ok but flagged "
        "for review.",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
    ]
    for row in report_rows(results, rules_by_symbol):
        lines.append("| " + " | ".join(cell.replace("|", "/") for cell in row) + " |")
    return "\n".join(lines) + "\n"
