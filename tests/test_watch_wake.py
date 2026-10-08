"""The market-hours wake: did the timer fire, did the network come up, did a scan run.

The resume events are shaped like this laptop's real ones (Power-Troubleshooter, event 1):
a power-button or lid resume names no source at all; a timer resume names the timer's owner.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import init_db, make_engine, make_session_factory
from app.locks import job_lock
from app.models import WatchRun
from app.watch.power import Resume, parse_resumes
from app.watch.report import wake_section
from app.watch.wake import classify_wake, run_wake

# Friday 2026-10-09, 08:30 IST.
WAKE = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)


def event(sleep: str, wake: str, target: int, text: str = "", owner: str = "") -> str:
    data = {
        "SleepTime": sleep,
        "WakeTime": wake,
        "TargetState": str(target),
        "EffectiveState": "5",
        "WakeSourceType": "0" if not owner else "3",
        "WakeSourceText": text,
        "WakeTimerOwner": owner,
    }
    fields = "".join(f"<Data Name='{k}'>{v}</Data>" for k, v in data.items())
    return (
        "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
        "<EventID>1</EventID></System>"
        f"<EventData>{fields}</EventData></Event>"
    )


LID_RESUME = event("2026-10-07T18:08:48.4762260Z", "2026-10-08T04:13:19.3332165Z", 6)
TIMER_RESUME = event(
    "2026-10-08T17:40:00.1234567Z",
    "2026-10-09T02:59:58.9876543Z",
    5,
    text="Timer",
    owner=r"\Device\HarddiskVolume3\Windows\System32\svchost.exe (SystemEventsBroker)",
)


@pytest.fixture
def db(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(tmp_path / "wake.db")
    init_db(engine)
    return make_session_factory(engine)


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class Pass:
    job: str = "feeds"
    entries: int = 1500
    new_articles: int = 2
    errors: list[dict[str, str]] | None = None

    def __post_init__(self) -> None:
        self.errors = self.errors or []


def wake(
    settings: Settings,
    db: sessionmaker[Session],
    locks: Path,
    clock: Clock,
    resumes: list[Resume],
    probe: Callable[[], str | None] = lambda: None,
    one_pass: Callable[[], list[Pass]] | None = None,
):  # noqa: ANN202
    calls: list[str] = []

    def default_pass() -> list[Pass]:
        calls.append("pass")
        return [Pass()]

    result = run_wake(
        settings,
        db,
        locks,
        one_pass or default_pass,
        on_ac=True,
        resumes=lambda: resumes,
        probe=probe,
        clock=clock,
        sleep=clock.sleep,
    )
    return result, calls


def feeds_run(db: sessionmaker[Session], at: datetime, new: int = 3, entries: int = 1480) -> None:
    with db() as session:
        session.add(
            WatchRun(
                job="feeds",
                started_at=at,
                finished_at=at,
                entries=entries,
                new_articles=new,
                errors=[],
            )
        )
        session.commit()


# ---------------------------------------------------------------- Windows's resume record


def test_resume_events_say_whether_a_timer_woke_the_laptop() -> None:
    lid, timer = parse_resumes(LID_RESUME + TIMER_RESUME)
    assert not lid.by_timer and lid.slept_as == "shut down"
    assert lid.woke_at == datetime(2026, 10, 8, 4, 13, 19, 333216, tzinfo=UTC)
    assert lid.woken_by.startswith("something other than a timer")
    assert timer.by_timer and timer.slept_as == "hibernating"
    assert "SystemEventsBroker" in timer.woken_by


def test_a_run_is_classed_by_the_resume_just_before_it() -> None:
    lid, timer = parse_resumes(LID_RESUME + TIMER_RESUME)
    assert classify_wake(WAKE, [lid, timer]) == ("timer", timer)
    assert classify_wake(WAKE + timedelta(hours=2), [lid, timer]) == ("awake", timer)
    lid_now = Resume(
        WAKE - timedelta(hours=9), WAKE - timedelta(seconds=20), "hibernating", 0, "", ""
    )
    assert classify_wake(WAKE, [lid_now])[0] == "resume"


# ---------------------------------------------------------------- run_wake


def test_a_timer_wake_waits_for_the_resident_scanners_scan(
    settings: Settings, db: sessionmaker[Session], tmp_path: Path
) -> None:
    clock = Clock(WAKE)
    resumes = parse_resumes(TIMER_RESUME)
    feeds_run(db, WAKE - timedelta(hours=10))  # last night's scan doesn't count

    def sleep(seconds: float) -> None:
        clock.now += timedelta(seconds=seconds)
        if clock.now >= WAKE + timedelta(seconds=30):
            feeds_run(db, WAKE + timedelta(seconds=15))  # the resident catches up

    clock.sleep = sleep  # type: ignore[method-assign]
    with job_lock(tmp_path, "watch"):  # the resident scanner holds its lock
        result, calls = wake(settings, db, tmp_path, clock, resumes)

    assert result.fired == "timer" and result.network_up and result.network_seconds == 0
    assert result.scan.by == "resident" and result.scan.new_articles == 3
    assert calls == []  # the task didn't scan itself
    text = "\n".join(result.lines)
    assert "fired: YES - woken by a wake timer" in text
    assert "network: up after 0 s" in text
    assert "scan: YES - by the resident scanner at 08:30:15" in text
    with db() as session:
        row = session.scalars(select(WatchRun).where(WatchRun.job == "wake")).one()
        assert row.details["fired"] == "timer" and row.details["scan"]["by"] == "resident"


def test_a_resident_that_doesnt_scan_is_covered_by_the_task(
    settings: Settings, db: sessionmaker[Session], tmp_path: Path
) -> None:
    clock = Clock(WAKE)
    with job_lock(tmp_path, "watch"):  # held, but the resident never scans (suspended)
        result, calls = wake(settings, db, tmp_path, clock, parse_resumes(TIMER_RESUME))
    assert calls == ["pass"]
    assert (
        result.scan.by == "this task" and "didn't complete a scan within 3 min" in result.scan.note
    )
    assert clock.now - WAKE >= timedelta(minutes=3)


def test_a_resident_scan_that_fetched_nothing_does_not_count(
    settings: Settings, db: sessionmaker[Session], tmp_path: Path
) -> None:
    """Right after a wake the resident scans before Wi-Fi is back: every feed fails."""
    feeds_run(db, WAKE + timedelta(seconds=5), new=0, entries=0)
    with job_lock(tmp_path, "watch"):
        result, calls = wake(settings, db, tmp_path, Clock(WAKE), parse_resumes(TIMER_RESUME))
    assert calls == ["pass"] and result.scan.by == "this task"


def test_with_no_resident_running_the_task_scans(
    settings: Settings, db: sessionmaker[Session], tmp_path: Path
) -> None:
    result, calls = wake(settings, db, tmp_path, Clock(WAKE), parse_resumes(TIMER_RESUME))
    assert calls == ["pass"] and result.scan.by == "this task"
    assert "wasn't running" in result.scan.note


def test_a_laptop_already_awake_needs_no_wake_and_reuses_the_last_scan(
    settings: Settings, db: sessionmaker[Session], tmp_path: Path
) -> None:
    later = WAKE + timedelta(hours=2)
    feeds_run(db, later - timedelta(minutes=4))
    with job_lock(tmp_path, "watch"):
        result, calls = wake(settings, db, tmp_path, Clock(later), parse_resumes(TIMER_RESUME))
    assert result.fired == "awake" and calls == []
    assert result.scan.by == "resident" and result.scan.note == "laptop was already awake"
    assert "fired: not needed - the laptop was already awake" in "\n".join(result.lines)


def test_a_wake_with_no_network_says_so(
    settings: Settings, db: sessionmaker[Session], tmp_path: Path
) -> None:
    clock = Clock(WAKE)

    def failing_pass() -> list[Pass]:
        return [Pass(entries=0, new_articles=0, errors=[{"feed": "CNBC-TV18", "error": "x"}])]

    result, _ = wake(
        settings,
        db,
        tmp_path,
        clock,
        parse_resumes(TIMER_RESUME),
        probe=lambda: "ConnectError: no route",
        one_pass=failing_pass,
    )
    assert not result.network_up and result.network_seconds is None
    assert clock.now - WAKE >= timedelta(minutes=2)  # it waited for the network first
    text = "\n".join(result.lines)
    assert "network: DOWN for all 120 s (ConnectError: no route)" in text
    assert "1 feeds failed" in text
    with db() as session:
        row = session.scalars(select(WatchRun).where(WatchRun.job == "wake")).one()
        assert row.errors == [{"stage": "network", "error": "ConnectError: no route"}]


# ---------------------------------------------------------------- the report


def test_the_report_gives_each_morning_a_verdict(
    settings: Settings, db: sessionmaker[Session], tmp_path: Path
) -> None:
    clock = Clock(WAKE)
    with job_lock(tmp_path, "watch"):
        feeds_run(db, WAKE + timedelta(seconds=15))
        wake(settings, db, tmp_path, clock, parse_resumes(TIMER_RESUME))
    # Monday: asleep through 08:30, woken by the lid at 09:15.
    monday_sleep = Resume(
        datetime(2026, 10, 11, 17, 0, tzinfo=UTC),
        datetime(2026, 10, 12, 3, 45, tzinfo=UTC),
        "hibernating",
        0,
        "",
        "",
    )
    with db() as session:
        runs = session.scalars(select(WatchRun)).all()
    lines = wake_section(
        runs,
        [*parse_resumes(TIMER_RESUME), monday_sleep],
        settings,
        WAKE - timedelta(hours=1),
        datetime(2026, 10, 12, 6, 0, tzinfo=UTC),
    )
    text = "\n".join(lines)
    assert "Fri 09 Oct: **the wake timer fired** at 08:30; network up after 0 s; scan YES" in text
    assert (
        "Mon 12 Oct: **the wake timer did not fire**: the laptop was hibernating from "
        "Sun 22:30 until Mon 09:15" in text
    )
    assert "| Sat 10 Oct" not in text  # weekends have no wake
