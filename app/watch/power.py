"""Is the laptop on mains power? The market-hours wake is for AC only (user, 2026-10-07)."""

import sys


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
