"""Catching up after the laptop was off or asleep (user, 2026-10-08: "BASIS should work
whenever the laptop is on").

When the scanner starts or wakes, it resumes from the start of its last completed feed scan
and reads everything published since, from every source that can reach that far back
(measured 2026-10-08):

  RSS feeds     each keeps 7 h (Livemint markets, ET stocks) to several days (BusinessLine
                80 h, CNBC-TV18 markets 68 h); a feed covers the gap from its oldest entry
  Google News   searched over exactly the missed period (`when:Nh`, or `after:` dates for
                more than three days); up to 100 results per search
  NSE filings   the announcements API, per company and date range (30 days and more back);
                the RSS only ever holds today
  prices        Yahoo's 1-minute bars, kept for the last 30 days

What none of them reaches is a possible gap, and the "while you were away" summary says so.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

SourceKind = Literal["news", "filings", "prices"]


@dataclass(frozen=True)
class Gap:
    start: datetime  # when the last completed scan started: everything after it is new
    end: datetime
    wanted_start: datetime  # before any cap on how far back a catch-up reads

    @property
    def capped(self) -> bool:
        return self.wanted_start < self.start


@dataclass(frozen=True)
class SourceCoverage:
    """How far back one source reached in a catch-up. `reached` None: not at all."""

    kind: SourceKind
    source: str
    reached: datetime | None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "reached": self.reached.isoformat() if self.reached else None,
            "note": self.note,
        }


def find_gap(
    last_completed: datetime | None, now: datetime, every: timedelta, max_days: int
) -> Gap | None:
    """The period to catch up on, or None when the scanner has kept up (its last completed
    scan is no more than two intervals old) or has never completed one."""
    if last_completed is None or now - last_completed <= 2 * every + timedelta(minutes=1):
        return None
    floor = now - timedelta(days=max_days)
    return Gap(max(last_completed, floor), now, last_completed)


def possible_gaps(gap: Gap, sources: Sequence[SourceCoverage]) -> list[dict[str, Any]]:
    """Parts of the gap that no source of a kind could cover. Coverage is a union: news
    from any outlet's feed or from Google News counts, since either could have carried a
    story; filings and prices each have one source."""
    found: list[dict[str, Any]] = []
    labels = {
        "news": "news",
        "filings": "NSE filings",
        "prices": "price moves",
    }
    for kind in ("news", "filings", "prices"):
        of_kind = [s for s in sources if s.kind == kind]
        if not of_kind:
            continue
        reached = [s.reached for s in of_kind if s.reached is not None]
        earliest = min(reached) if reached else None
        if earliest is None or earliest > gap.start:
            found.append(
                {
                    "what": labels[kind],
                    "from": gap.start.isoformat(),
                    "to": (earliest or gap.end).isoformat(),
                    "why": "; ".join(s.note for s in of_kind if s.note) or "no source reached it",
                }
            )
    if gap.capped:
        found.append(
            {
                "what": "everything",
                "from": gap.wanted_start.isoformat(),
                "to": gap.start.isoformat(),
                "why": "older than a catch-up reads back",
            }
        )
    return found
