"""Listed companies to add to the watchlist from the BASIS page (user, 2026-10-16; BSE and
new listings, 2026-10-17).

Three lists, each downloaded again once a day, so a company that lists today is offered by the
next day at the latest:

  NSE        nsearchives.nseindia.com/content/equities/EQUITY_L.csv          main board
  NSE SME    nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv   Emerge
  BSE        api.bseindia.com/BseIndiaAPI/api/ListofScripData/w              active equity

Verified: EQUITY_L.csv's header on 2026-10-16 ("SYMBOL,NAME OF COMPANY, SERIES, ..."; the
names after the first carry a leading space) and SME_EQUITY_L.csv's on 2026-10-17
("SYMBOL,NAME_OF_COMPANY,SERIES,DATE_OF_LISTING,PAID_UP_VALUE,ISIN_NUMBER,FACE_VALUE,":
underscores, a trailing comma, newest listing first, two-digit years). BSE's list is the one
the PEAD tool has downloaded every day since 2026-09-24 from this laptop with BSE_HEADERS
(BSE answers 403 without Origin and Accept), 5,110 active codes on 2026-10-16; the row shape
is the PEAD tool's fixture of a real row. Every request is the user's own data: none of these
can be reached from anywhere else BASIS has been tested. BSE's list is fetched with `requests`,
the PEAD tool's own call: on 2026-10-17 BSE answered httpx 403 for the same URL and headers,
the morning after the PEAD tool's `requests` call had downloaded it (`_bse_get`).

The lists are merged by ISIN: a company on both exchanges is one entry and is added as its NSE
ticker (.NS); a company only on BSE is added as a BSE ticker (.BO), in whichever form Yahoo
answers to - BSE's scrip id or its numeric code - which the page's preview finds out.

An ISIN changes with a split or a bonus, and the two exchanges don't change it on the same
day: on 2026-10-16 NSE's and BSE's lists (as the PEAD tool downloaded them) gave 36 companies
different ISINs, all the same issuer with a new serial (360 ONE: INE466L01020 at NSE,
INE466L01038 at BSE; NSE had caught up by the next day). So a BSE code whose ISIN isn't on
NSE's lists joins the one NSE company with the same issuer and security type (the ISIN's first
nine characters), when exactly one company on each side has them; it keeps its own ISIN as
`bse_isin`. The same split had left four of the eight watchlist stocks with the ISIN from
before it, so BSE filings - which the PEAD tool keys by BSE's ISIN and code - are matched on
what today's lists say as well as the watchlist's own (`filing_keys`).

New listings: NSE's lists carry a listing date. BSE's doesn't, so a BSE code is dated the day
BASIS first saw it in BSE's list; codes already there at the very first download get no date,
since they aren't new - BASIS just hadn't looked. Anything listed or first seen in the last
NEW_DAYS days is offered under the search box before anything is typed.

The scanner and the web server both refresh the file when it is due, whichever runs first,
under one lock (`job_lock`, "companies"); `newsdesk companies --refresh` does it by hand. A
list that fails keeps its last good copy and is tried again after RETRY_AFTER; the page says
which list failed and why.
"""

import contextlib
import csv
import io
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import WatchAliases, WatchItem
from app.locks import job_lock

log = logging.getLogger(__name__)

LIST_FILE = "data/companies.json"
# The NSE-only list of 2026-10-16. Read when companies.json doesn't exist yet, so the page
# works before the first download of all three; never written again.
LEGACY_NSE_FILE = "data/nse_equity_list.csv"

NSE_MAIN_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_SME_URL = "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"
BSE_LIST_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)
# The PEAD tool's headers for BSE, verbatim: BSE refuses the API without Origin and Accept.
BSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

SOURCES = ("nse", "nse_sme", "bse")
LABELS = {"nse": "NSE", "nse_sme": "NSE SME", "bse": "BSE"}
REFRESH_EVERY = timedelta(hours=20)  # once a day, a little early so it never drifts later
RETRY_AFTER = timedelta(hours=2)
NEW_DAYS = 30
SUGGESTIONS = 8
TIMEOUT = 60.0  # BSE's list is a few MB of JSON
# A symbol this short is likely shared ("HAL", "BDL", "ITC"): it needs a market word.
STRONG_SYMBOL_LENGTH = 6
# NSE Emerge's series, as its file writes them (verified 2026-10-17).
SME_SERIES = frozenset({"SM", "ST"})
INDIA = ZoneInfo("Asia/Kolkata")
ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}\d")
_SUFFIX = re.compile(r"[\s,]+(limited|ltd\.?)\s*$", re.I)


# ---------------------------------------------------------------- one company


@dataclass(frozen=True)
class Company:
    """One listed company, on NSE, BSE or both."""

    key: str  # the ISIN, or "NSE:SYMBOL" / "BSE:CODE" when there is no valid one
    name: str  # as the exchange writes it, NSE's when it is on both
    isin: str | None = None
    nse_symbol: str | None = None
    nse_series: str | None = None
    bse_code: str | None = None
    bse_id: str | None = None  # BSE's scrip id, e.g. ABB
    bse_isin: str | None = None  # BSE's ISIN, when it isn't yet the same as NSE's
    listed: date | None = None  # NSE's listing date
    first_seen: date | None = None  # when BASIS first saw it in BSE's list

    @property
    def exchanges(self) -> list[str]:
        return [name for name, code in (("NSE", self.nse_symbol), ("BSE", self.bse_code)) if code]

    @property
    def sme(self) -> bool:
        return self.nse_series in SME_SERIES

    @property
    def added_on(self) -> date | None:
        """When it listed, or when BASIS first saw it listed."""
        return self.listed or self.first_seen

    @property
    def yahoo_candidates(self) -> list[str]:
        """Yahoo tickers worth asking for, best first: NSE's, then BSE's scrip id, then BSE's
        numeric code (Yahoo uses either for BSE, depending on the stock)."""
        found: list[str] = []
        if self.nse_symbol:
            found.append(f"{self.nse_symbol}.NS")
        if self.bse_id and not self.bse_id.isdigit():
            found.append(f"{self.bse_id}.BO")
        if self.bse_code:
            found.append(f"{self.bse_code}.BO")
        return list(dict.fromkeys(found))

    @property
    def yahoo_symbol(self) -> str:
        return self.yahoo_candidates[0]

    @property
    def short_name(self) -> str:
        """The name without "Limited", in the case the exchange uses unless that is all
        capitals."""
        name = _SUFFIX.sub("", self.name).strip()
        return name.title() if name.isupper() and len(name) > 4 else name

    @property
    def symbol(self) -> str:
        """The exchange code a person would type: NSE's symbol, else BSE's scrip id or code."""
        return self.nse_symbol or self.bse_id or self.bse_code or self.key

    def is_new(self, today: date, days: int = NEW_DAYS) -> bool:
        added = self.added_on
        return added is not None and added >= today - timedelta(days=days)


# ---------------------------------------------------------------- parsing the lists


def _date(value: str) -> date | None:
    for pattern in ("%d-%b-%Y", "%d-%b-%y"):
        with contextlib.suppress(ValueError):
            return datetime.strptime(value.strip(), pattern).date()
    return None


def parse_nse_csv(content: bytes) -> list[dict[str, str]]:
    """EQUITY_L.csv or SME_EQUITY_L.csv: their headers differ only in spaces and underscores.
    Raises ValueError when it isn't one of them, so a blocked page never replaces a list."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("empty file")
    fields = {name.strip().upper().replace("_", " "): name for name in reader.fieldnames if name}
    needed = ("SYMBOL", "NAME OF COMPANY", "ISIN NUMBER")
    if any(name not in fields for name in needed):
        raise ValueError(f"not an NSE equity list: columns {sorted(fields)}")

    def cell(row: Mapping[str, str | None], name: str) -> str:
        column = fields.get(name)
        return (row.get(column) or "").strip() if column else ""

    rows = []
    for row in reader:
        symbol, name = cell(row, "SYMBOL"), cell(row, "NAME OF COMPANY")
        if symbol and name:
            rows.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "series": cell(row, "SERIES"),
                    "listed": cell(row, "DATE OF LISTING"),
                    "isin": cell(row, "ISIN NUMBER"),
                }
            )
    if not rows:
        raise ValueError("no companies in the file")
    return rows


def parse_bse_json(data: Any) -> list[dict[str, str]]:
    """BSE's ListofScripData: a list of rows, or {"Table": [...]} (the PEAD tool accepts
    both). Field names are matched without regard to case."""
    rows_in = data if isinstance(data, list) else (data or {}).get("Table") or []
    rows = []
    for raw in rows_in:
        if not isinstance(raw, dict):
            continue
        row = {str(k).lower(): ("" if v is None else str(v).strip()) for k, v in raw.items()}
        code = row.get("scrip_cd") or row.get("scripcode") or ""
        name = row.get("issuer_name") or row.get("scrip_name") or ""
        if not code or not name:
            continue
        if row.get("status") and row["status"].lower() != "active":
            continue
        rows.append(
            {
                "code": code,
                "id": row.get("scrip_id", ""),
                "name": name,
                "isin": row.get("isin_number") or row.get("isin") or "",
            }
        )
    if not rows:
        raise ValueError("no companies in the answer")
    return rows


def _row_key(source: str, row: Mapping[str, str]) -> str:
    return f"BSE:{row['code']}" if source == "bse" else f"NSE:{row['symbol']}"


def _entitlement(isin: str) -> bool:
    """A rights entitlement (security type 20, e.g. NATCO-RE, INE987B20018): both exchanges
    list one for the few days it trades, and it is not a stock to watch."""
    return isin.startswith("INE") and isin[7:9] == "20"


def _issuer(isin: str | None) -> str | None:
    """An Indian company's ISIN up to its serial: INE, the issuer, the security type."""
    return isin[:9] if isin and isin.startswith("INE") else None


def merge(
    sources: Mapping[str, Sequence[Mapping[str, str]]], first_seen: Mapping[str, str]
) -> list[Company]:
    """The three lists as one, by ISIN - or by issuer, when the exchanges' ISINs differ (see
    the top of this file). NSE's name wins for a company on both."""
    by_key: dict[str, dict[str, Any]] = {}

    def seen(entry: dict[str, Any], source: str, row: Mapping[str, str]) -> None:
        held = first_seen.get(_row_key(source, row))
        if held:
            day = date.fromisoformat(held)
            entry["first_seen"] = min(entry.get("first_seen") or day, day)

    for source in ("nse", "nse_sme"):
        for row in sources.get(source, ()):
            isin = row.get("isin", "").upper()
            if _entitlement(isin):
                continue
            valid = ISIN.fullmatch(isin) is not None
            key = isin if valid else _row_key(source, row)
            if key not in by_key:
                by_key[key] = {
                    "key": key,
                    "isin": isin if valid else None,
                    "name": row["name"],
                    "nse_symbol": row["symbol"],
                    "nse_series": row.get("series") or None,
                    "listed": _date(row.get("listed", "")),
                }
            seen(by_key[key], source, row)

    bse_rows = [r for r in sources.get("bse", ()) if not _entitlement(r.get("isin", "").upper())]
    nse_by_issuer: dict[str, list[str]] = {}
    for key, entry in by_key.items():
        if issuer := _issuer(entry["isin"]):
            nse_by_issuer.setdefault(issuer, []).append(key)
    bse_by_issuer: dict[str, int] = {}
    for row in bse_rows:
        if issuer := _issuer(row.get("isin", "").upper()):
            bse_by_issuer[issuer] = bse_by_issuer.get(issuer, 0) + 1

    for row in bse_rows:
        isin = row.get("isin", "").upper()
        valid = ISIN.fullmatch(isin) is not None
        key = isin if valid else _row_key("bse", row)
        entry = by_key.get(key)
        if entry is None and valid and (issuer := _issuer(isin)):
            same = nse_by_issuer.get(issuer, [])
            if len(same) == 1 and bse_by_issuer.get(issuer) == 1:
                entry = by_key[same[0]]
                if "bse_code" not in entry:
                    entry["bse_isin"] = isin
        if entry is None:
            if not row.get("name"):
                continue  # a code without a name (the PEAD tool's copy) can't be offered alone
            entry = by_key[key] = {"key": key, "isin": isin if valid else None, "name": row["name"]}
        if "bse_code" not in entry:
            entry["bse_code"] = row["code"]
            entry["bse_id"] = row.get("id") or None
        seen(entry, "bse", row)
    return [Company(**entry) for entry in by_key.values()]


# ---------------------------------------------------------------- the file


@dataclass
class SourceState:
    rows: list[dict[str, str]] = field(default_factory=list)
    fetched_at: datetime | None = None  # the last good download
    tried_at: datetime | None = None  # the last attempt, good or not
    error: str | None = None  # why the last attempt failed
    via: str | None = None  # "pead": rows from the PEAD tool's copy, not BSE (see _from_pead)


@dataclass
class ListFile:
    sources: dict[str, SourceState] = field(
        default_factory=lambda: {source: SourceState() for source in SOURCES}
    )
    first_seen: dict[str, str] = field(default_factory=dict)


def _when(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


def read_file(path: Path) -> ListFile:
    """The stored lists, or empty ones. Never raises."""
    state = ListFile()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    for source in SOURCES:
        held = (data.get("sources") or {}).get(source) or {}
        state.sources[source] = SourceState(
            rows=[r for r in held.get("rows") or [] if isinstance(r, dict)],
            fetched_at=_when(held.get("fetched_at")),
            tried_at=_when(held.get("tried_at")),
            error=held.get("error"),
            via=held.get("via"),
        )
    state.first_seen = {k: v for k, v in (data.get("first_seen") or {}).items() if v}
    return state


def _legacy(path: Path) -> list[dict[str, str]]:
    legacy = path.parent / Path(LEGACY_NSE_FILE).name
    try:
        return parse_nse_csv(legacy.read_bytes())
    except (OSError, ValueError):
        return []


def _write(path: Path, state: ListFile) -> None:
    data = {
        "note": "NSE, NSE SME and BSE company lists for the watchlist editor "
        "(app/watch/companies.py). Rewritten by every refresh.",
        "sources": {
            source: {
                "fetched_at": held.fetched_at.isoformat() if held.fetched_at else None,
                "tried_at": held.tried_at.isoformat() if held.tried_at else None,
                "error": held.error,
                "via": held.via,
                "count": len(held.rows),
                "rows": held.rows,
            }
            for source, held in state.sources.items()
        },
        "first_seen": state.first_seen,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.new")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    # Windows refuses a rename onto a file another process has open at that instant (a page
    # reading it): try again for a moment rather than lose the refresh.
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2)


@dataclass(frozen=True)
class ListStatus:
    source: str
    count: int
    fetched_at: datetime | None
    error: str | None
    via: str | None = None

    @property
    def label(self) -> str:
        return LABELS[self.source]

    @property
    def short_error(self) -> str | None:
        """The error in a few words for the page: "HTTP 403", else the message without its
        exception class."""
        if not self.error:
            return None
        if found := re.search(r"HTTP \d{3}", self.error):
            return found.group(0)
        return self.error.split(": ", 1)[-1][:80]


_cache: dict[Path, tuple[tuple[int, int], list[Company], list[ListStatus]]] = {}


def load(path: Path) -> tuple[list[Company], list[ListStatus]]:
    """The merged companies and each list's state, cached until the file changes. Before the
    first download, the NSE-only list of 2026-10-16 if it is there."""
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        legacy = _legacy(path)
        companies = merge({"nse": legacy}, {}) if legacy else []
        return companies, [ListStatus("nse", len(legacy), None, None)] if legacy else []
    held = _cache.get(path)
    if held is None or held[0] != stamp:
        state = read_file(path)
        companies = merge({s: v.rows for s, v in state.sources.items()}, state.first_seen)
        status = [
            ListStatus(s, len(v.rows), v.fetched_at, v.error, v.via)
            for s, v in state.sources.items()
        ]
        held = (stamp, companies, status)
        _cache[path] = held
    return held[1], held[2]


def load_companies(path: Path) -> list[Company]:
    return load(path)[0]


def due(path: Path, now: datetime) -> list[str]:
    """The lists to download now: never downloaded or a day old, and not tried in the last
    RETRY_AFTER."""
    state = read_file(path)
    found = []
    for source, held in state.sources.items():
        stale = held.fetched_at is None or now - held.fetched_at >= REFRESH_EVERY
        resting = held.tried_at is not None and now - held.tried_at < RETRY_AFTER
        if stale and not resting:
            found.append(source)
    return found


# (status, body) for a URL and headers, with a timeout: how BSE's list is fetched.
BseGet = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]


def _refused(status: int, body: bytes) -> bool:
    return status in (401, 403) or b"access denied" in body[:2000].lower()


def _bse_get(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    """BSE's list, asked two ways until one is let through:

    1. The PEAD tool's call, verbatim: `requests.get` with its headers. It downloaded the list
       on 2026-10-16 at 18:18; BSE refused httpx the same request the next morning.
    2. As a browser: curl_cffi impersonating Chrome (its TLS and HTTP/2 fingerprint as well as
       its headers), opening bseindia.com first for the cookies a browser would carry. BSE
       refused the PEAD tool's call too at 16:50 on 2026-10-17, from this laptop.

    Both libraries come with yfinance. Returns the first answer that isn't a refusal, or the
    last refusal."""
    import requests

    response = requests.get(url, headers=dict(headers), timeout=timeout)
    status, body = response.status_code, response.content
    if not _refused(status, body):
        return status, body
    log.info("BSE refused the PEAD tool's call (HTTP %s); asking as a browser", status)
    try:
        from curl_cffi import requests as browser

        with browser.Session(impersonate="chrome") as session:
            session.get("https://www.bseindia.com/", timeout=timeout)
            answer = session.get(
                url,
                headers={key: headers[key] for key in ("Referer", "Origin", "Accept")},
                timeout=timeout,
            )
            if not _refused(answer.status_code, answer.content):
                log.info("BSE answered the browser-like request")
            return answer.status_code, answer.content
    except Exception as exc:
        log.warning("the browser-like request to BSE failed: %s", exc)
        return status, body


def _fetch(
    client: httpx.Client, source: str, user_agent: str, bse_get: BseGet | None, timeout: float
) -> list[dict[str, str]]:
    if source == "bse" and bse_get is not None:
        status, body = bse_get(BSE_LIST_URL, BSE_HEADERS, timeout)
    else:
        if source == "bse":
            url, headers = BSE_LIST_URL, BSE_HEADERS
        else:
            url = NSE_MAIN_URL if source == "nse" else NSE_SME_URL
            headers = {"User-Agent": user_agent, "Accept": "text/csv,*/*"}
        response = client.get(url, headers=headers)
        status, body = response.status_code, response.content
    if _refused(status, body):
        raise RuntimeError(f"refused (HTTP {status})")
    if status >= 400:
        raise RuntimeError(f"HTTP {status}")
    if source == "bse":
        return parse_bse_json(json.loads(body))
    return parse_nse_csv(body)


PEAD_MASTER = "scrip_master.json"


def pead_master_path(announcements_db: str | None) -> Path | None:
    """The PEAD tool's scrip master, beside the announcements file BASIS already reads."""
    return Path(announcements_db).parent / PEAD_MASTER if announcements_db else None


def _from_pead(path: Path) -> list[dict[str, str]]:
    """BSE's codes and ISINs from the PEAD tool's scrip master ({"bse": {code: ISIN}}), which
    it builds from the same BSE list whenever it runs. It has no names, so it only gives the
    companies also on NSE their BSE code - which is what matching BSE filings and the .BO
    tickers need; a company only on BSE can't be offered until BSE's own list arrives.
    Empty when the file isn't there or isn't that."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        codes = data.get("bse") or {}
    except (OSError, ValueError, AttributeError):
        return []
    return [
        {"code": str(code), "id": "", "name": "", "isin": str(isin)}
        for code, isin in codes.items()
        if str(code).isdigit() and ISIN.fullmatch(str(isin))
    ]


def refresh(
    path: Path,
    user_agent: str,
    timeout: float = TIMEOUT,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
    lock_dir: Path | None = None,
    sources: Iterable[str] | None = None,
    bse_get: BseGet | None = None,
    pead_master: Path | None = None,
) -> str:
    """Download the lists that are due (`sources`, or all three) and write the merged file.
    Returns a line saying what happened; never raises. BSE's list comes through `bse_get`,
    `requests` by default; with a `transport` (tests) and no `bse_get`, everything goes
    through httpx. When BSE refuses and there is no good copy of its list, its codes come
    from the PEAD tool's own copy (`pead_master`, see `_from_pead`) until BSE answers."""
    now = now or datetime.now(UTC)
    wanted = [s for s in (sources if sources is not None else SOURCES) if s in SOURCES]
    if not wanted:
        return "company lists: nothing due"
    if bse_get is None and transport is None:
        bse_get = _bse_get
    try:
        if lock_dir is None:
            return _refresh(path, user_agent, timeout, transport, now, wanted, bse_get, pead_master)
        with job_lock(lock_dir, "companies") as acquired:
            if not acquired:
                return "company lists: another process is refreshing them"
            return _refresh(path, user_agent, timeout, transport, now, wanted, bse_get, pead_master)
    except Exception as exc:  # a refresh must never take its caller down
        log.warning("company lists not refreshed: %s", exc)
        return f"company lists not refreshed: {type(exc).__name__}: {exc}"


def _refresh(
    path: Path,
    user_agent: str,
    timeout: float,
    transport: httpx.BaseTransport | None,
    now: datetime,
    wanted: Sequence[str],
    bse_get: BseGet | None,
    pead_master: Path | None = None,
) -> str:
    state = read_file(path)
    if not path.exists() and not state.sources["nse"].rows:
        state.sources["nse"].rows = _legacy(path)  # so its companies don't all count as new
    today = now.astimezone(INDIA).date().isoformat()
    parts = []
    with httpx.Client(timeout=timeout, follow_redirects=True, transport=transport) as client:
        for source in wanted:
            held = state.sources[source]
            held.tried_at = now
            try:
                rows = _fetch(client, source, user_agent, bse_get, timeout)
            except Exception as exc:
                held.error = f"{type(exc).__name__}: {exc}"
                parts.append(f"{LABELS[source]} failed ({held.error})")
                # No good copy of BSE's own: its codes from the PEAD tool's copy meanwhile.
                usable = source == "bse" and (not held.rows or held.via) and pead_master
                if usable and (fallback := _from_pead(pead_master)):  # type: ignore[arg-type]
                    held.rows, held.via = fallback, "pead"
                    parts.append(f"BSE codes from the PEAD tool's copy {len(fallback)}")
                continue
            # A code that wasn't in the last good copy is new today - unless there was no
            # last copy, when nothing can be told apart from the companies always there.
            before = {_row_key(source, row) for row in held.rows}
            if before:
                for row in rows:
                    key = _row_key(source, row)
                    if key not in before:
                        state.first_seen.setdefault(key, today)
            held.rows, held.fetched_at, held.error, held.via = rows, now, None, None
            parts.append(f"{LABELS[source]} {len(rows)}")
    _write(path, state)
    return "company lists: " + ", ".join(parts)


# ---------------------------------------------------------------- finding one


def search(companies: Sequence[Company], query: str, limit: int = SUGGESTIONS) -> list[Company]:
    """Companies whose symbol, BSE code or name matches what is typed: an exact code first,
    then a code or a word of the name that starts with it, then the name containing it."""
    wanted = query.strip().lower().removesuffix(".ns").removesuffix(".bo")
    if len(wanted) < 2:
        return []
    ranked: list[tuple[int, int, str, Company]] = []
    for company in companies:
        codes = [c.lower() for c in (company.nse_symbol, company.bse_id, company.bse_code) if c]
        name = company.name.lower()
        isins = [i.lower() for i in (company.isin, company.bse_isin) if i]
        if wanted in codes or wanted in isins:
            rank = 0
        elif any(code.startswith(wanted) for code in codes) or name.startswith(wanted):
            rank = 1
        elif any(word.startswith(wanted) for word in re.split(r"[\s&().,-]+", name)):
            rank = 2
        elif wanted in name:
            rank = 3
        else:
            continue
        # The main board before SME and BSE-only, then shorter names.
        board = 0 if company.nse_symbol and not company.sme else 1 if company.nse_symbol else 2
        ranked.append((rank, board, f"{len(name):04d}{name}", company))
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked[:limit]]


def recent(companies: Sequence[Company], today: date, limit: int = SUGGESTIONS) -> list[Company]:
    """The newest listings, newest first: what to offer before anything is typed."""
    new = [c for c in companies if c.is_new(today)]
    new.sort(key=lambda c: (c.added_on or date.min, c.name), reverse=True)
    return new[:limit]


def find(companies: Sequence[Company], wanted: str) -> Company | None:
    """By key or either exchange's ISIN, Yahoo ticker, NSE symbol, or BSE code or id."""
    text = wanted.strip()
    if not text:
        return None
    upper = text.upper()
    bare = upper.removesuffix(".NS").removesuffix(".BO")
    for company in companies:
        if company.key == text or upper in (company.isin, company.bse_isin):
            return company
    for company in companies:
        if upper in (t.upper() for t in company.yahoo_candidates):
            return company
    for company in companies:
        if bare in {c.upper() for c in (company.nse_symbol, company.bse_id, company.bse_code) if c}:
            return company
    return None


def draft(company: Company, group: str | None = None, yahoo: str | None = None) -> WatchItem:
    """A first watchlist entry for a company, to be checked against real headlines before it
    is added: its name without "Limited" as a strong alias, and its exchange symbol - strong
    when it is distinctive (PFOCUS, DATAPATTNS), weak when short enough to be shared (HAL,
    BDL), where a weak alias counts only with a market word nearby. `yahoo` is the ticker
    the preview found Yahoo answers to; the first candidate otherwise. The ISIN is BSE's
    when the exchanges differ, since it is what BSE filings are matched on."""
    name = company.short_name
    strong, weak = [name], []
    for code in dict.fromkeys(c for c in (company.nse_symbol, company.bse_id) if c):
        if code.upper() != name.upper() and not code.isdigit():
            (strong if len(code) >= STRONG_SYMBOL_LENGTH else weak).append(code)
    symbol = yahoo if yahoo in company.yahoo_candidates else company.yahoo_symbol
    return WatchItem(
        type="stock",
        symbol=symbol,
        name=name,
        nse_symbol=company.nse_symbol,
        nse_name=company.name if company.nse_symbol else None,
        isin=company.bse_isin or company.isin,
        bse_code=company.bse_code,
        aliases=WatchAliases(strong=strong, weak=weak),
        group=group,
    )


def on_watchlist(company: Company, items: Iterable[WatchItem]) -> WatchItem | None:
    """The watchlist entry that already is this company, by ISIN or any of its tickers."""
    tickers = set(company.yahoo_candidates)
    isins = {i for i in (company.isin, company.bse_isin) if i}
    for item in items:
        if item.symbol in tickers or item.isin in isins:
            return item
    return None


def filing_keys(
    stocks: Iterable[WatchItem], companies: Sequence[Company]
) -> tuple[dict[str, str], dict[str, str]]:
    """What BSE filings are matched on - {ISIN: ticker} and {BSE code: ticker} - for the
    watchlist's stocks: each one's own `isin` and `bse_code`, and the ISINs and BSE code
    today's lists give its ticker, which stay right after a split changes the ISIN."""
    by_ticker = {ticker: c for c in companies for ticker in c.yahoo_candidates}
    by_isin = {isin: c for c in companies for isin in (c.isin, c.bse_isin) if isin}
    isins: dict[str, str] = {}
    codes: dict[str, str] = {}
    for stock in stocks:
        if stock.type != "stock":
            continue
        if stock.isin:
            isins[stock.isin] = stock.symbol
        if stock.bse_code:
            codes[stock.bse_code] = stock.symbol
        company = by_ticker.get(stock.symbol) or by_isin.get(stock.isin or "")
        if company is None:
            continue
        for isin in (company.isin, company.bse_isin):
            if isin:
                isins.setdefault(isin, stock.symbol)
        if company.bse_code:
            codes.setdefault(company.bse_code, stock.symbol)
    return isins, codes


def parse_aliases(text: str) -> list[str]:
    """Aliases as typed in the page: comma-separated, blanks and repeats dropped."""
    found: dict[str, None] = {}
    for part in text.split(","):
        alias = " ".join(part.split())
        if alias:
            found.setdefault(alias, None)
    return list(found)


# ---------------------------------------------------------------- in the web server


class ListRefresher:
    """Keeps the lists a day fresh from inside the web server, in a background thread, so the
    page never waits on NSE or BSE and works even when the scanner isn't running. `kick` is
    called by the watchlist pages; it starts a refresh only when one is due (or `force`, the
    page's "Try again"), and never two at once."""

    def __init__(
        self,
        path: Path,
        lock_dir: Path,
        user_agent: str,
        enabled: bool,
        pead_master: Path | None = None,
    ) -> None:
        self.path = path
        self.pead_master = pead_master
        self.lock_dir = lock_dir
        self.user_agent = user_agent
        self.enabled = enabled
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self.last_line: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def kick(self, force: bool = False) -> bool:
        """Start a refresh if one is due. True while one is running."""
        if not self.enabled:
            return False
        with self._guard:
            if self.running:
                return True
            now = datetime.now(UTC)
            sources = list(SOURCES) if force else due(self.path, now)
            if not sources:
                return False
            self._thread = threading.Thread(
                target=self._run, args=(sources,), name="company-lists", daemon=True
            )
            self._thread.start()
            return True

    def _run(self, sources: list[str]) -> None:
        self.last_line = refresh(
            self.path,
            self.user_agent,
            lock_dir=self.lock_dir,
            sources=sources,
            pead_master=self.pead_master,
        )
        log.info(self.last_line)
