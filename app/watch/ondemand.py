"""Scanning one stock when its card is opened on /watchlist (user, 2026-10-18).

The web server never writes the database, and the scanner is the one process that stores
articles, groups stories and makes the watch-lane call. So opening a card leaves a request
for the scanner, which runs it in its own loop:

  web      write_request()   data/scan_requests/<SYMBOL>.json, written to a temporary file and
                             renamed, so the scanner never reads half of one
  scanner  pending_requests  between jobs, and every couple of seconds while it waits for the
                             next tick (app/watch/scan.py `serve_scan_requests`)
  scanner  a watch_scans row opened `running` *before* the request file is removed, so a
                             second click always finds one of the two and never asks twice
  scanner  the pass          Google News, NSE filings, BSE filings, price, analysing; each step
                             committed to the row as it ends
  scanner  write_heartbeat   data/scan_requests/_scanner.json every 10 s from a thread of its
                             own that writes only that file: alive, and busy with what
  web      scan_state()      the row, the request file and the heartbeat, read only

The request file is only an inbox; the row is the record the page reads.
"""

import contextlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import WatchScan
from app.watch.preview import Profile

STEPS = (
    ("google_news", "Google News"),
    ("nse", "NSE filings"),
    ("bse", "BSE filings"),
    ("price", "Price"),
    ("analyse", "Analysing"),
)
HEARTBEAT_FILE = "_scanner.json"
HEARTBEAT_EVERY = 10.0  # seconds between the scanner's heartbeats
HEARTBEAT_STALE = timedelta(seconds=30)  # three missed: the scanner isn't running
# The page asks this often while a scan is on its way, and this often while it only waits.
POLL_FAST = "1500ms"
POLL_SLOW = "5s"

START_SCANNER = (
    "Start-ScheduledTask -TaskPath '\\Newsdesk\\' -TaskName 'Newsdesk-watch'",
    "uv run newsdesk watch",
)
RESTART_FOR_SCANS = (
    "Restart the scanner to enable scans: it creates the table scans are recorded in when it "
    "starts with this version."
)

ScanKind = Literal[
    "unavailable",  # no watch_scans table yet: the scanner runs an older version
    "none",  # never scanned, nothing asked
    "queued",  # asked, the scanner is alive and hasn't taken it yet
    "waiting",  # asked, alive, but busy for longer than the pickup time
    "not_running",  # asked, and no heartbeat
    "gave_up",  # asked, and nothing came back within the give-up time
    "running",
    "stopped",  # the scanner stopped in the middle of a scan
    "done",
    "failed",
]


# ---------------------------------------------------------------- the inbox


@dataclass(frozen=True)
class ScanRequest:
    id: str
    symbol: str
    requested_at: datetime


def request_path(inbox: Path, symbol: str) -> Path:
    """The inbox file for one stock: its ticker with anything but letters, digits, dots,
    dashes and underscores replaced, so no symbol can name a path outside the inbox."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", symbol).strip(".")
    if not safe or safe.startswith("_scanner"):
        raise ValueError(f"not a stock symbol: {symbol!r}")
    return inbox / f"{safe}.json"


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write to a temporary file beside `path`, then rename it over: a reader sees the old
    file or the new one, never half of one. Windows refuses a rename onto a file another
    process has open for that moment, so it is tried again shortly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    for _ in range(10):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    for _ in range(3):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except PermissionError:  # being replaced this instant (Windows)
            time.sleep(0.05)
            continue
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None
    return None


def write_request(inbox: Path, symbol: str, now: datetime) -> ScanRequest:
    request = ScanRequest(uuid.uuid4().hex, symbol, now)
    _write_atomic(
        request_path(inbox, symbol),
        {"id": request.id, "symbol": symbol, "requested_at": now.isoformat()},
    )
    return request


def _parse_request(data: dict[str, Any] | None) -> ScanRequest | None:
    try:
        if data is None:
            return None
        return ScanRequest(
            str(data["id"]), str(data["symbol"]), datetime.fromisoformat(data["requested_at"])
        )
    except (KeyError, TypeError, ValueError):
        return None


def read_request(inbox: Path, symbol: str) -> ScanRequest | None:
    try:
        path = request_path(inbox, symbol)
    except ValueError:
        return None
    return _parse_request(_read_json(path))


def pending_requests(inbox: Path) -> list[ScanRequest]:
    """Every request in the inbox, oldest first. A file that doesn't read as one is
    removed: nothing could ever take it."""
    if not inbox.is_dir():
        return []
    found = []
    for path in inbox.glob("*.json"):
        if path.name == HEARTBEAT_FILE:
            continue
        request = _parse_request(_read_json(path))
        if request is None:
            path.unlink(missing_ok=True)
            continue
        found.append(request)
    return sorted(found, key=lambda request: request.requested_at)


def remove_request(inbox: Path, request: ScanRequest) -> None:
    """Take a request out of the inbox - only if the file still holds that request (the
    page may have written a newer one meanwhile). A file the page is reading this instant
    can't be removed on Windows, so it is tried again shortly; one left behind anyway is
    skipped next time, since its row already exists."""
    path = request_path(inbox, request.symbol)
    for _ in range(10):
        current = _parse_request(_read_json(path))
        if current is None or current.id != request.id:
            return
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.05)


# ---------------------------------------------------------------- the heartbeat


@dataclass(frozen=True)
class Heartbeat:
    beat_at: datetime
    activity: str  # "idle", a job ("feeds", "analyse"...), or "scan HAL.NS"
    since: datetime
    pid: int

    def alive(self, now: datetime) -> bool:
        return now - self.beat_at <= HEARTBEAT_STALE


def write_heartbeat(inbox: Path, activity: str, since: datetime, now: datetime) -> None:
    _write_atomic(
        inbox / HEARTBEAT_FILE,
        {
            "beat_at": now.isoformat(),
            "activity": activity,
            "since": since.isoformat(),
            "pid": os.getpid(),
        },
    )


def read_heartbeat(inbox: Path) -> Heartbeat | None:
    data = _read_json(inbox / HEARTBEAT_FILE)
    try:
        if data is None:
            return None
        return Heartbeat(
            datetime.fromisoformat(data["beat_at"]),
            str(data["activity"]),
            datetime.fromisoformat(data["since"]),
            int(data["pid"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def clear_heartbeat(inbox: Path) -> None:
    """On a clean stop: the page knows at once that nobody will take a request."""
    with contextlib.suppress(OSError):
        (inbox / HEARTBEAT_FILE).unlink(missing_ok=True)


# ---------------------------------------------------------------- the steps and the profile


def new_steps() -> list[dict[str, str]]:
    return [{"key": key, "label": label, "state": "pending", "detail": ""} for key, label in STEPS]


def with_step(
    steps: list[dict[str, str]], key: str, state: str, detail: str = ""
) -> list[dict[str, str]]:
    """A new list (a JSON column is only saved when it is replaced, not changed in place)."""
    return [
        step | {"state": state, "detail": detail} if step["key"] == key else dict(step)
        for step in steps
    ]


def profile_to_json(profile: Profile) -> dict[str, Any]:
    return {
        "closes": [[at.isoformat(), close] for at, close in profile.closes],
        "currency": profile.currency,
        "sector": profile.sector,
        "industry": profile.industry,
        "market_cap": profile.market_cap,
        "summary": profile.summary,
        "error": profile.error,
    }


def profile_from_json(data: dict[str, Any] | None) -> Profile | None:
    if not data:
        return None
    return Profile(
        closes=[(datetime.fromisoformat(at), float(close)) for at, close in data.get("closes", [])],
        currency=data.get("currency"),
        sector=data.get("sector"),
        industry=data.get("industry"),
        market_cap=data.get("market_cap"),
        summary=data.get("summary"),
        error=data.get("error"),
    )


# ---------------------------------------------------------------- what the page shows


@dataclass(frozen=True)
class ScanState:
    kind: ScanKind
    latest: WatchScan | None = None  # the newest row, whatever its status
    finished: WatchScan | None = None  # the newest finished row: results, "last scanned"
    previous: WatchScan | None = None  # the finished row before it: what "new" is new since
    request: ScanRequest | None = None
    heartbeat: Heartbeat | None = None
    message: str | None = None
    polling: Literal["fast", "slow"] | None = None

    @property
    def steps(self) -> list[dict[str, str]]:
        if self.kind == "running" and self.latest is not None:
            return self.latest.steps
        return new_steps()

    @property
    def profile(self) -> Profile | None:
        """Yahoo's profile from the newest scan that fetched one."""
        for row in (self.latest, self.finished, self.previous):
            if row is not None and row.profile:
                return profile_from_json(row.profile)
        return None

    def new_since(self) -> datetime | None:
        """Stories stored after this are "new": since the stock's previous scan, or, on its
        first, those this scan found."""
        if self.finished is None:
            return None
        return (self.previous or self.finished).started_at


def _seconds(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds()))


def busy_message(heartbeat: Heartbeat, now: datetime) -> str:
    activity = heartbeat.activity
    started = _seconds(now - heartbeat.since)
    if activity.startswith("scan "):
        doing = f"scanning {activity.removeprefix('scan ')}"
    elif activity == "idle":
        doing = "between passes"
    else:
        doing = f"finishing its regular {activity} pass"
    return f"The scanner is {doing} (for {started} s); your scan runs as soon as that ends."


def scan_state(
    session: Session, inbox: Path, symbol: str, now: datetime, settings: Settings
) -> ScanState:
    """Where this stock's scan stands, from the scanner's row, the page's request file and the
    scanner's heartbeat. Reads only."""
    watch = settings.watch
    try:
        rows = session.scalars(
            select(WatchScan)
            .where(WatchScan.symbol == symbol)
            .order_by(WatchScan.id.desc())
            .limit(12)
        ).all()
    except OperationalError:  # the scanner hasn't created watch_scans yet
        return ScanState("unavailable", message=RESTART_FOR_SCANS)
    latest = rows[0] if rows else None
    finished = [row for row in rows if row.finished_at is not None]
    done = dict(
        latest=latest,
        finished=finished[0] if finished else None,
        previous=finished[1] if len(finished) > 1 else None,
    )
    request = read_request(inbox, symbol)
    heartbeat = read_heartbeat(inbox)
    alive = heartbeat is not None and heartbeat.alive(now)
    taken = request is not None and latest is not None and latest.request_id == request.id
    if request is not None and not taken:
        waited = now - request.requested_at
        base = dict(done, request=request, heartbeat=heartbeat)
        if waited > timedelta(seconds=watch.scan_give_up_seconds):
            why = (
                "the scanner isn't running"
                if not alive
                else "the scanner was busy the whole time"  # heartbeat is not None here
            )
            return ScanState(
                "gave_up",
                **base,  # type: ignore[arg-type]
                message=f"Nothing came back within {watch.scan_give_up_seconds // 60} minutes: "
                f"{why}. What is already stored is below.",
            )
        if not alive:
            return ScanState(
                "not_running",
                **base,  # type: ignore[arg-type]
                message="The scanner isn't running, so nothing was fetched.",
                polling="slow",
            )
        if waited >= timedelta(seconds=watch.scan_pickup_seconds):
            assert heartbeat is not None
            return ScanState(
                "waiting",
                **base,  # type: ignore[arg-type]
                message=busy_message(heartbeat, now),
                polling="slow",
            )
        return ScanState("queued", **base, polling="fast")  # type: ignore[arg-type]
    if latest is not None and latest.status == "running":
        if not alive and now - latest.updated_at > HEARTBEAT_STALE:
            return ScanState(
                "stopped",
                **done,  # type: ignore[arg-type]
                heartbeat=heartbeat,
                message="The scanner stopped in the middle of this scan. What it stored is "
                "below; scan again once it is running.",
            )
        return ScanState("running", **done, heartbeat=heartbeat, polling="fast")  # type: ignore[arg-type]
    if latest is not None:
        kind: ScanKind = "failed" if latest.status == "failed" else "done"
        return ScanState(kind, **done, heartbeat=heartbeat)  # type: ignore[arg-type]
    return ScanState("none", heartbeat=heartbeat)


def should_ask(state: ScanState, now: datetime, settings: Settings, again: bool) -> bool:
    """Whether a click asks the scanner for a new scan. Never while one is asked for or on its
    way; not within `scan_reuse_seconds` of the last, unless "Scan again"; and not before the
    scanner can record one."""
    if state.kind in ("unavailable", "queued", "waiting", "not_running", "running"):
        return False
    if state.kind in ("done", "failed") and state.finished is not None and not again:
        age = now - (state.finished.finished_at or state.finished.started_at)
        return age > timedelta(seconds=settings.watch.scan_reuse_seconds)
    return True


def ago(moment: datetime, now: datetime) -> str:
    seconds = _seconds(now - moment)
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min ago"
    return f"{round(seconds / 3600)} h ago"


def footer_line(scan: WatchScan) -> str:
    """ "Found 3 new articles, 1 filing, 1 new story · 1 LLM call", or what went wrong."""

    def count(n: int, one: str, many: str) -> str:
        return f"{n} {one if n == 1 else many}"

    found = (
        f"Found {count(scan.new_articles, 'new article', 'new articles')}, "
        f"{count(scan.new_filings, 'filing', 'filings')}, "
        f"{count(scan.new_stories, 'new story', 'new stories')} · "
        f"{count(scan.calls, 'LLM call', 'LLM calls')}"
    )
    skipped = scan.skipped_stale or 0
    if skipped:
        found += f" · {count(skipped, 'result', 'results')} older than a week skipped"
    if scan.status == "failed":
        first = scan.errors[0] if scan.errors else {}
        return f"The scan failed: {first.get('error', 'see the scanner log')}"
    return found
