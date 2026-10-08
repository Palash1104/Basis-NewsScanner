"""The market-hours wake (`newsdesk watch --wake`, run by the Newsdesk-watch-wake task).

Each run answers three questions and records the answers (watch_runs, job "wake", in
`details`), so the morning after a test says plainly what happened:

  fired    Did a wake timer wake the laptop for this run? Windows's own resume record says
           what woke it; a run that follows no recent resume found the laptop already awake.
  network  Did the network come up, and how long did it take? The laptop's standby turns
           networking off, and a wake that brings no network can't scan.
  scan     Did a scan actually run? The resident scanner usually does it (it is overdue the
           moment the laptop wakes). If it hasn't within a few minutes - suspended, or not
           running - this task runs one pass itself.

A wake that never fired leaves no row at all: the report shows that morning from Windows's
resume log instead ("asleep from 23:10 until 09:15, woken by the power button").
"""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.locks import job_lock
from app.models import WatchRun, utcnow
from app.watch.power import Resume

log = logging.getLogger(__name__)

# How recent a resume must be for this run to count as its wake.
RESUME_WINDOW = timedelta(minutes=5)
NETWORK_WAIT = timedelta(minutes=2)
NETWORK_EVERY_SECONDS = 5.0
# How long to give the resident scanner to scan after a wake before doing it here.
RESIDENT_WAIT = timedelta(minutes=3)
POLL_SECONDS = 10.0


@dataclass
class ScanSummary:
    by: str  # resident | this task | none
    at: datetime | None = None
    entries: int = 0
    new_articles: int = 0
    feed_errors: int = 0
    note: str = ""


@dataclass
class WakeResult:
    started_at: datetime
    on_ac: bool | None
    fired: str  # timer | resume | awake
    resume: Resume | None
    network_up: bool
    network_seconds: float | None
    network_error: str | None
    scan: ScanSummary
    lines: list[str] = field(default_factory=list)

    def details(self) -> dict[str, Any]:
        resume = self.resume
        return {
            "fired": self.fired,
            "resume": None
            if resume is None
            else {
                "slept_at": resume.slept_at.isoformat(),
                "woke_at": resume.woke_at.isoformat(),
                "slept_as": resume.slept_as,
                "woken_by": resume.woken_by,
                "source_type": resume.source_type,
                "source_text": resume.source_text,
                "timer_owner": resume.timer_owner,
            },
            "network": {
                "up": self.network_up,
                "after_seconds": self.network_seconds,
                "error": self.network_error,
            },
            "scan": {
                "by": self.scan.by,
                "at": self.scan.at.isoformat() if self.scan.at else None,
                "entries": self.scan.entries,
                "new_articles": self.scan.new_articles,
                "feed_errors": self.scan.feed_errors,
                "note": self.scan.note,
            },
        }


def probe_network(url: str, user_agent: str, timeout: float = 5.0) -> str | None:
    """None if `url` answered at all (any status: the network is there), else the error.
    It sends the scanner's own User-Agent: NSE leaves a request with httpx's default one
    unanswered until it times out, which looked exactly like no network (2026-10-08)."""
    try:
        httpx.head(url, headers={"User-Agent": user_agent}, timeout=timeout)
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def classify_wake(started: datetime, resumes: Sequence[Resume]) -> tuple[str, Resume | None]:
    """timer: a wake timer woke the laptop just before this run. resume: something else woke
    it just before (the lid, the power button). awake: no resume in the last few minutes, so
    the laptop was already awake and nothing had to fire."""
    recent = [r for r in resumes if started - RESUME_WINDOW <= r.woke_at <= started + RESUME_WINDOW]
    if not recent:
        return "awake", (resumes[-1] if resumes else None)
    latest = recent[-1]
    return ("timer" if latest.by_timer else "resume"), latest


def run_wake(
    settings: Settings,
    session_factory: sessionmaker[Session],
    lock_dir: Path,
    one_pass: Callable[[], Sequence[Any]],
    *,
    on_ac: bool | None,
    resumes: Callable[[], Sequence[Resume]],
    probe: Callable[[], str | None],
    clock: Callable[[], datetime] = utcnow,
    sleep: Callable[[float], None] = time.sleep,
) -> WakeResult:
    """One run of the wake task. `one_pass` runs every scanner job once and returns their
    JobResults; it is only called when the resident scanner doesn't scan."""
    started = clock()

    # The network first: after a wake it can take a while to come back.
    network_error = probe()
    while network_error is not None and clock() - started < NETWORK_WAIT:
        sleep(NETWORK_EVERY_SECONDS)
        network_error = probe()
    network_up = network_error is None
    network_seconds = (clock() - started).total_seconds() if network_up else None

    # Windows logs the resume as it happens, so by now it is there to read.
    fired, resume = classify_wake(started, resumes())

    with job_lock(lock_dir, "watch") as free:
        if free:
            # No resident scanner (it isn't running): this task is the scan.
            scan = _own_pass(one_pass, clock, "the resident scanner wasn't running")
        else:
            scan = _resident_scan(settings, session_factory, started, fired, clock, sleep)
            if scan is None:
                with job_lock(lock_dir, "watch-wake") as mine:
                    scan = (
                        _own_pass(
                            one_pass,
                            clock,
                            "the resident scanner didn't complete a scan within "
                            f"{int(RESIDENT_WAIT.total_seconds() // 60)} min of the wake",
                        )
                        if mine
                        else ScanSummary("none", note="another wake run is scanning")
                    )
    result = WakeResult(
        started, on_ac, fired, resume, network_up, network_seconds, network_error, scan
    )
    result.lines = wake_lines(result, settings)
    with session_factory() as session:
        session.add(
            WatchRun(
                job="wake",
                started_at=started,
                finished_at=clock(),
                on_ac=on_ac,
                entries=scan.entries,
                new_articles=scan.new_articles,
                errors=[] if network_up else [{"stage": "network", "error": network_error}],
                details=result.details(),
            )
        )
        session.commit()
    return result


def _resident_scan(
    settings: Settings,
    session_factory: sessionmaker[Session],
    started: datetime,
    fired: str,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> ScanSummary | None:
    """The resident scanner's feed scan that covers this wake, waiting for one if needed.
    Only a scan that fetched something counts: right after a wake the resident often scans
    before the network is back, every feed fails, and it tries again a minute later. None if
    no scan worked in time."""
    every = timedelta(minutes=settings.watch.feeds_every_minutes)
    # The laptop was awake all along: the resident's latest scan is this run's scan.
    since = (
        started - every - timedelta(minutes=1)
        if fired == "awake"
        else started - timedelta(minutes=1)
    )
    while True:
        with session_factory() as session:
            run = session.scalars(
                select(WatchRun)
                .where(WatchRun.job == "feeds", WatchRun.started_at >= since, WatchRun.entries > 0)
                .order_by(WatchRun.started_at.desc())
                .limit(1)
            ).first()
        if run is not None:
            failed = sum(1 for error in run.errors if error.get("feed"))
            return ScanSummary(
                "resident",
                run.started_at,
                run.entries,
                run.new_articles,
                failed,
                "laptop was already awake" if fired == "awake" else "",
            )
        if clock() - started >= RESIDENT_WAIT:
            return None
        sleep(POLL_SECONDS)


def _own_pass(
    one_pass: Callable[[], Sequence[Any]], clock: Callable[[], datetime], why: str
) -> ScanSummary:
    at = clock()
    try:
        results = list(one_pass())
    except Exception as exc:
        log.exception("wake pass failed")
        return ScanSummary("none", at, note=f"{why}; this task's pass failed: {exc}")
    feeds = next((r for r in results if r.job == "feeds"), None)
    return ScanSummary(
        "this task",
        at,
        sum(r.entries for r in results),
        sum(r.new_articles for r in results),
        sum(1 for error in feeds.errors if error.get("feed")) if feeds else 0,
        why,
    )


def wake_lines(result: WakeResult, settings: Settings) -> list[str]:
    """What the wake log says about one run, in words."""
    tz = settings.tz

    def at(value: datetime) -> str:
        return value.astimezone(tz).strftime("%H:%M:%S")

    on_ac = {True: "yes", False: "no", None: "unknown"}[result.on_ac]
    lines = [
        f"wake run at {result.started_at.astimezone(tz):%a %d %b %H:%M:%S %Z} (AC power: {on_ac})"
    ]
    resume = result.resume
    if result.fired == "timer" and resume is not None:
        lines.append(
            f"fired: YES - woken by {resume.woken_by} at {at(resume.woke_at)} (it had been "
            f"{resume.slept_as} since {resume.slept_at.astimezone(tz):%a %H:%M})"
        )
    elif result.fired == "resume" and resume is not None:
        lines.append(
            f"fired: NO - the laptop resumed at {at(resume.woke_at)}, woken by "
            f"{resume.woken_by} (it had been {resume.slept_as} since "
            f"{resume.slept_at.astimezone(tz):%a %H:%M}); this run followed that resume"
        )
    else:
        last = (
            f" (last resume {resume.woke_at.astimezone(tz):%a %H:%M}, by {resume.woken_by})"
            if resume is not None
            else ""
        )
        lines.append(f"fired: not needed - the laptop was already awake{last}")
    if result.network_up:
        lines.append(f"network: up after {result.network_seconds:.0f} s")
    else:
        lines.append(
            f"network: DOWN for all {int(NETWORK_WAIT.total_seconds())} s ({result.network_error})"
        )
    scan = result.scan
    if scan.by == "none" or scan.at is None:
        lines.append(f"scan: NONE - {scan.note}")
    else:
        who = "the resident scanner" if scan.by == "resident" else "this wake task"
        errors = f", {scan.feed_errors} feeds failed" if scan.feed_errors else ""
        note = f" ({scan.note})" if scan.note else ""
        lines.append(
            f"scan: YES - by {who} at {at(scan.at)}: {scan.entries} entries, "
            f"{scan.new_articles} new watchlist articles{errors}{note}"
        )
    return lines
