"""The laptop's power: mains or battery, keeping it awake while work runs, and Windows's own
record of each resume - which is the only reliable answer to "did the wake timer fire?".

Measured on this laptop, 2026-10-08:
  - closing the lid or pressing the power button hibernates it (target state 5)
  - "Shut down" from the Start menu is logged as a hibernation too (target 6, effective 5:
    Fast Startup), and after it nothing runs until someone logs in
  - no resume in the previous ten days had a wake timer as its source
"""

import logging
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

log = logging.getLogger(__name__)

# SYSTEM_POWER_STATE, as Power-Troubleshooter writes it.
# Written to read "the laptop was hibernating from 23:10" or "had been shut down since".
SLEPT_AS = {2: "asleep (S1)", 3: "asleep (S2)", 4: "asleep (S3)", 5: "hibernating", 6: "shut down"}
_EVENT_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
_RESUME_QUERY = (
    "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and (EventID=1)]]"
)


def on_ac_power() -> bool | None:
    """True on AC, False on battery, None where the platform can't say (not Windows, or
    Windows doesn't know)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class SystemPowerStatus(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", wintypes.BYTE),
            ("BatteryFlag", wintypes.BYTE),
            ("BatteryLifePercent", wintypes.BYTE),
            ("SystemStatusFlag", wintypes.BYTE),
            ("BatteryLifeTime", wintypes.DWORD),
            ("BatteryFullLifeTime", wintypes.DWORD),
        ]

    status = SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    line = status.ACLineStatus & 0xFF
    return {0: False, 1: True}.get(line)  # 255 means unknown


@contextmanager
def keep_awake() -> Iterator[None]:
    """Ask Windows not to go back to sleep while the block runs. After a timer wake the
    machine would otherwise drop back into sleep within a couple of minutes, possibly in the
    middle of a scan. It can't override a lid being closed or the power button, and the
    request ends with the block."""
    if sys.platform != "win32":
        yield
        return
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    kernel32 = ctypes.windll.kernel32
    kernel32.SetThreadExecutionState(es_continuous | es_system_required)
    try:
        yield
    finally:
        kernel32.SetThreadExecutionState(es_continuous)


@dataclass(frozen=True)
class Resume:
    """One resume as Windows logged it (Power-Troubleshooter, event 1)."""

    slept_at: datetime  # UTC
    woke_at: datetime  # UTC
    slept_as: str  # hibernating, shut down, asleep (S3)...
    source_type: int
    source_text: str
    timer_owner: str  # set when a wake timer woke it: which program armed the timer

    @property
    def by_timer(self) -> bool:
        return bool(self.timer_owner) or "timer" in self.source_text.casefold()

    @property
    def woken_by(self) -> str:
        if self.by_timer:
            owner = f", armed by {self.timer_owner}" if self.timer_owner else ""
            return f"a wake timer{owner}"
        if self.source_text:
            return self.source_text
        # What every power-button and lid resume on this laptop looks like.
        return "something other than a timer (Windows names no source)"


def _when(value: str) -> datetime:
    # "2026-10-08T08:30:03.2676466Z": seven fractional digits, more than fromisoformat takes.
    head, _, fraction = value.rstrip("Z").partition(".")
    return datetime.fromisoformat(f"{head}.{(fraction + '000000')[:6]}").replace(tzinfo=UTC)


def parse_resumes(xml: str) -> list[Resume]:
    """Events as `wevtutil qe ... /f:xml` prints them: <Event> elements with no root."""
    root = ET.fromstring(f"<Events>{xml}</Events>")
    found = []
    for event in root.iter(f"{_EVENT_NS}Event"):
        data = {item.get("Name"): (item.text or "") for item in event.iter(f"{_EVENT_NS}Data")}
        try:
            target = int(data.get("TargetState") or 0)
            found.append(
                Resume(
                    slept_at=_when(data["SleepTime"]),
                    woke_at=_when(data["WakeTime"]),
                    slept_as=SLEPT_AS.get(target, f"state {target}"),
                    source_type=int(data.get("WakeSourceType") or 0),
                    source_text=data.get("WakeSourceText", ""),
                    timer_owner=data.get("WakeTimerOwner", ""),
                )
            )
        except (KeyError, ValueError):
            continue
    return sorted(found, key=lambda resume: resume.woke_at)


def recent_resumes(count: int = 40) -> list[Resume]:
    """The last `count` resumes from the System event log, oldest first. Empty off Windows or
    when the log can't be read; reading it needs no administrator rights."""
    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(
            [
                "wevtutil",
                "qe",
                "System",
                f"/q:{_RESUME_QUERY}",
                f"/c:{count}",
                "/rd:true",
                "/f:xml",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return parse_resumes(result.stdout) if result.returncode == 0 else []
    except Exception as exc:  # the wake log must still be written without it
        log.warning("could not read resume events: %s", exc)
        return []
