"""Listed companies to add to the watchlist from the BASIS page (user, 2026-10-16).

The list is NSE's own equity master file, EQUITY_L.csv: symbol, company name, series, listing
date and ISIN for every company listed on NSE's main board. Verified 2026-10-16 at the URL
below (header "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT,
ISIN NUMBER, FACE VALUE"; the column names after the first carry a leading space).

BSE-only companies are not offered: BSE's scrip list comes from its API, which refused this
machine's requests in October 2026 (see CLAUDE.md, step 7), and BSE filings reach BASIS only
through the PEAD tool. A company listed on both exchanges is in NSE's file.

The scanner downloads the file when it is missing or a week old (`refresh_if_due`), and
`newsdesk companies --refresh` does it by hand. The web page only ever reads it: the page
makes no request for the type-ahead.
"""

import csv
import io
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.config import WatchAliases, WatchItem

log = logging.getLogger(__name__)

EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
LIST_FILE = "data/nse_equity_list.csv"
REFRESH_EVERY = timedelta(days=7)
RETRY_AFTER = timedelta(hours=6)
SUGGESTIONS = 8
# A symbol this short is likely shared ("HAL", "BDL", "ITC"): it needs a market word.
STRONG_SYMBOL_LENGTH = 6
_SUFFIX = re.compile(r"\s+(limited|ltd\.?)\s*$", re.I)


@dataclass(frozen=True)
class Company:
    symbol: str  # NSE symbol, e.g. BDL
    name: str  # as NSE writes it, e.g. Bharat Dynamics Limited
    isin: str
    series: str

    @property
    def yahoo_symbol(self) -> str:
        return f"{self.symbol}.NS"

    @property
    def short_name(self) -> str:
        """The name without "Limited", in the case NSE uses unless that is all capitals."""
        name = _SUFFIX.sub("", self.name).strip()
        return name.title() if name.isupper() and len(name) > 4 else name


def parse_equity_list(content: bytes) -> list[Company]:
    """NSE's EQUITY_L.csv. Raises ValueError when it isn't that file."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("empty file")
    fields = {name.strip().upper(): name for name in reader.fieldnames}
    needed = ("SYMBOL", "NAME OF COMPANY", "ISIN NUMBER")
    if any(name not in fields for name in needed):
        raise ValueError(f"not NSE's equity list: columns {sorted(fields)}")
    found = []
    for row in reader:
        symbol = (row.get(fields["SYMBOL"]) or "").strip()
        name = (row.get(fields["NAME OF COMPANY"]) or "").strip()
        isin = (row.get(fields["ISIN NUMBER"]) or "").strip()
        series = (row.get(fields.get("SERIES", ""), "") or "").strip()
        if symbol and name:
            found.append(Company(symbol, name, isin, series))
    if not found:
        raise ValueError("no companies in the file")
    return found


def load_companies(path: Path) -> list[Company]:
    """The cached list, or [] when it hasn't been downloaded yet or can't be read."""
    try:
        return _cached(path, path.stat().st_mtime)
    except (OSError, ValueError) as exc:
        log.debug("company list unavailable: %s", exc)
        return []


_cache: dict[Path, tuple[float, list[Company]]] = {}


def _cached(path: Path, mtime: float) -> list[Company]:
    held = _cache.get(path)
    if held is None or held[0] != mtime:
        held = (mtime, parse_equity_list(path.read_bytes()))
        _cache[path] = held
    return held[1]


def list_age(path: Path, now: datetime) -> timedelta | None:
    try:
        return now - datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


def refresh(
    path: Path, user_agent: str, timeout: float, transport: httpx.BaseTransport | None = None
) -> str:
    """Download the list, check it is the list, and replace the cached copy in one step.
    Returns a line saying what happened; never raises."""
    try:
        with httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            transport=transport,
        ) as client:
            response = client.get(EQUITY_LIST_URL)
        response.raise_for_status()
        companies = parse_equity_list(response.content)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(response.content)
        os.replace(temporary, path)
        return f"NSE company list: {len(companies)} companies"
    except Exception as exc:
        return f"NSE company list not refreshed: {type(exc).__name__}: {exc}"


def search(companies: Sequence[Company], query: str, limit: int = SUGGESTIONS) -> list[Company]:
    """Companies whose symbol or name matches what is typed: an exact symbol first, then a
    symbol or a word of the name that starts with it, then the name containing it."""
    wanted = query.strip().lower()
    if len(wanted) < 2:
        return []
    ranked: list[tuple[int, int, str, Company]] = []
    for company in companies:
        symbol, name = company.symbol.lower(), company.name.lower()
        if symbol == wanted:
            rank = 0
        elif symbol.startswith(wanted) or name.startswith(wanted):
            rank = 1
        elif any(word.startswith(wanted) for word in re.split(r"[\s&().,-]+", name)):
            rank = 2
        elif wanted in name:
            rank = 3
        else:
            continue
        # EQ (the main series) before the rest, then shorter names.
        ranked.append((rank, 0 if company.series == "EQ" else 1, f"{len(name):04d}{name}", company))
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked[:limit]]


def find(companies: Sequence[Company], symbol: str) -> Company | None:
    wanted = symbol.strip().upper().removesuffix(".NS")
    return next((c for c in companies if c.symbol.upper() == wanted), None)


def draft(company: Company, group: str | None = None) -> WatchItem:
    """A first watchlist entry for a company, to be checked against real headlines before it
    is added: its name without "Limited" as a strong alias, and its NSE symbol - strong when
    it is distinctive (PFOCUS, DATAPATTNS), weak when short enough to be shared (HAL, BDL),
    where a weak alias counts only with a market word nearby."""
    name = company.short_name
    strong, weak = [name], []
    symbol = company.symbol
    if symbol.upper() != name.upper():
        (strong if len(symbol) >= STRONG_SYMBOL_LENGTH else weak).append(symbol)
    return WatchItem(
        type="stock",
        symbol=company.yahoo_symbol,
        name=name,
        nse_symbol=symbol,
        nse_name=company.name,
        isin=company.isin or None,
        aliases=WatchAliases(strong=strong, weak=weak),
        group=group,
    )


def parse_aliases(text: str) -> list[str]:
    """Aliases as typed in the page: comma-separated, blanks and repeats dropped."""
    found: dict[str, None] = {}
    for part in text.split(","):
        alias = " ".join(part.split())
        if alias:
            found.setdefault(alias, None)
    return list(found)
