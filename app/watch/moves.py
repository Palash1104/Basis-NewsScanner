"""'Moved, no story yet' (user, 2026-10-07/08): thresholds from each stock's typical day,
and the defence peer group checked first.

A stock's typical move is the median, over its last 60 sessions, of the day's largest
excursion from the previous close (from hourly highs and lows). Backtested on a year of
hourly bars, 2026-10-08, at THRESHOLD x typical:

  - alone, the eight stocks would have alerted 1.4-3.1 times a month each
  - a defence name's move net of NIFTY_IND_DEFENCE.NS, against its typical net move, 2.1-3.3
  - Prime Focus on the tax-raid day moved 9.1% against a typical 3.7% (2.5x): at 2x it
    would have fired around 10:00, before the first web report (10:18); 2.5x only at the low

So, at each price poll:
  - sector move: the group's index beyond THRESHOLD x its typical move, or SECTOR_SHARE of
    the group beyond PEER_THRESHOLD x their own in the same direction - one alert for the
    group, not one per stock
  - a group member alerts alone only when its move net of the index is beyond THRESHOLD x
    its typical net move (and it moved at least its typical move): well beyond its peers
  - a stock outside any group alerts at THRESHOLD x its typical move
  - none for a stock with a story since the previous close: that story explains it
  - at most one alert per stock per day, and one sector alert per group, day and direction
"""

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

THRESHOLD = 2.0
PEER_THRESHOLD = 1.5
SECTOR_SHARE = 0.6
LOOKBACK_SESSIONS = 60
MIN_SESSIONS = 20  # fewer and there is no threshold yet: no alert

HourlyBar = tuple[datetime, float, float, float]  # start, high, low, close


@dataclass(frozen=True)
class Typical:
    move: float  # the median session excursion from the previous close, as a fraction
    excess: float | None  # the same, net of the group's index (group members only)


@dataclass(frozen=True)
class Snapshot:
    """The latest price of one symbol against its previous close."""

    symbol: str
    at: datetime
    move: float  # price / previous close - 1


@dataclass(frozen=True)
class MoveEvent:
    kind: str  # price | sector
    key: str  # what makes it once a day: "price:HAL.NS:2026-10-08"
    at: datetime
    symbol: str | None  # the stock, or the group's index for a sector move
    move: float
    multiple: float  # the move against the threshold's typical move
    group: str | None = None
    peers: tuple[tuple[str, float], ...] = ()  # (symbol, move) for a sector move
    excess: float | None = None  # net of the index, for a group member


def _sessions(bars: Sequence[HourlyBar], day_of: callable) -> dict[date, list[HourlyBar]]:  # type: ignore[valid-type]
    days: dict[date, list[HourlyBar]] = {}
    for bar in sorted(bars):
        days.setdefault(day_of(bar[0]), []).append(bar)
    return days


def session_excursions(
    bars: Sequence[HourlyBar], day_of, versus: Sequence[HourlyBar] | None = None
) -> list[tuple[date, float]]:
    """Each session's largest excursion from the previous close; with `versus`, of the
    stock net of that index, hour by hour."""
    days = _sessions(bars, day_of)
    index_days = _sessions(versus, day_of) if versus is not None else None
    ordered = sorted(days)
    found: list[tuple[date, float]] = []
    for prev, day in zip(ordered, ordered[1:], strict=False):
        close = days[prev][-1][3]
        if index_days is None:
            worst = max(
                max(abs(high / close - 1), abs(low / close - 1)) for _, high, low, _ in days[day]
            )
            found.append((day, worst))
            continue
        if day not in index_days or prev not in index_days:
            continue
        index_close = index_days[prev][-1][3]
        index_bars = {bar[0]: bar for bar in index_days[day]}
        worst = 0.0
        for start, high, low, _ in days[day]:
            other = index_bars.get(start)
            if other is None:
                continue
            for mine, theirs in ((high, other[1]), (low, other[2])):
                worst = max(worst, abs((mine / close - 1) - (theirs / index_close - 1)))
        found.append((day, worst))
    return found


def typical_move(excursions: Sequence[tuple[date, float]], before: date) -> float | None:
    earlier = [value for day, value in excursions if day < before][-LOOKBACK_SESSIONS:]
    return statistics.median(earlier) if len(earlier) >= MIN_SESSIONS else None


def typical_moves(
    bars: Mapping[str, Sequence[HourlyBar]],
    group_index: Mapping[str, str | None],
    day: date,
    day_of,
) -> dict[str, Typical]:
    """Typical moves for every symbol with enough history, as of the sessions before `day`.
    `group_index` maps a stock to its group's index (or None)."""
    found: dict[str, Typical] = {}
    for symbol, series in bars.items():
        move = typical_move(session_excursions(series, day_of), day)
        if move is None:
            continue
        index = group_index.get(symbol)
        excess = None
        if index and index in bars and index != symbol:
            excess = typical_move(session_excursions(series, day_of, bars[index]), day)
        found[symbol] = Typical(move, excess)
    return found


def detect(
    snapshots: Mapping[str, Snapshot],
    typical: Mapping[str, Typical],
    groups: Mapping[str, tuple[str | None, Sequence[str]]],
    ungrouped: Iterable[str],
    has_story: Mapping[str, bool],
    day: date,
) -> list[MoveEvent]:
    """The moves worth an alert at one moment. `groups` maps a group name to (its index,
    its members). Dedupe across moments is the caller's: every event carries its key."""
    events: list[MoveEvent] = []
    for name, (index, members) in groups.items():
        present = [s for s in members if s in snapshots and s in typical]
        index_snap = snapshots.get(index) if index else None
        index_typical = typical.get(index) if index else None
        sector = None
        if index_snap and index_typical and abs(index_snap.move) >= THRESHOLD * index_typical.move:
            sector = 1 if index_snap.move > 0 else -1
        elif present:
            for direction in (1, -1):
                beyond = [
                    s
                    for s in present
                    if snapshots[s].move * direction >= PEER_THRESHOLD * typical[s].move
                ]
                if len(beyond) >= SECTOR_SHARE * len(present):
                    sector = direction
                    break
        if sector is not None:
            reference = index_snap or snapshots[present[0]]
            move = (
                index_snap.move
                if index_snap
                else statistics.mean(snapshots[s].move for s in present)
            )
            events.append(
                MoveEvent(
                    "sector",
                    f"sector:{name}:{day.isoformat()}:{'up' if sector > 0 else 'down'}",
                    reference.at,
                    index,
                    move,
                    abs(move) / index_typical.move if index_typical else 0.0,
                    group=name,
                    peers=tuple((s, snapshots[s].move) for s in present),
                )
            )
        for symbol in present:
            snap, usual = snapshots[symbol], typical[symbol]
            if has_story.get(symbol) or abs(snap.move) < usual.move:
                continue
            if index_snap and usual.excess:
                excess = snap.move - index_snap.move
                if abs(excess) >= THRESHOLD * usual.excess:
                    events.append(
                        MoveEvent(
                            "price",
                            f"price:{symbol}:{day.isoformat()}",
                            snap.at,
                            symbol,
                            snap.move,
                            abs(excess) / usual.excess,
                            group=name,
                            excess=excess,
                        )
                    )
            elif sector is None and abs(snap.move) >= THRESHOLD * usual.move:
                # No index to net out: alone only if the sector as a whole didn't move.
                events.append(
                    MoveEvent(
                        "price",
                        f"price:{symbol}:{day.isoformat()}",
                        snap.at,
                        symbol,
                        snap.move,
                        abs(snap.move) / usual.move,
                        group=name,
                    )
                )
    for symbol in ungrouped:
        snap, usual = snapshots.get(symbol), typical.get(symbol)
        if snap is None or usual is None or has_story.get(symbol):
            continue
        if abs(snap.move) >= THRESHOLD * usual.move:
            events.append(
                MoveEvent(
                    "price",
                    f"price:{symbol}:{day.isoformat()}",
                    snap.at,
                    symbol,
                    snap.move,
                    abs(snap.move) / usual.move,
                )
            )
    return events
