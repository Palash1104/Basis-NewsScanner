"""One job of each kind at a time.

The OS scheduler starts `newsdesk run` every three hours whether or not the last one
finished, so a slow run and the next one would otherwise overlap: two fetches, two sets of
LLM calls on the same stories, and two writers on one SQLite file.

The lock is an OS file lock, not a pid file, because the kernel drops it when the process
exits: a crashed or killed run never leaves a lock behind to be cleared by hand. Each job
kind has its own lock, so the 07:30 digest still goes out while a slow 07:00 run is going.

Windows locks are mandatory rather than advisory, so while a job holds its lock the file
can't even be read. That is why the holder's pid is written to the file but nothing reads
it: the log line is what says who skipped and why.
"""

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import IO

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

log = logging.getLogger(__name__)


def _acquire(handle: IO[str]) -> bool:
    """Take an exclusive lock on the file's first byte without waiting."""
    try:
        if sys.platform == "win32":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _release(handle: IO[str]) -> None:
    try:
        handle.seek(0)
        if sys.platform == "win32":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:  # closing the handle drops the lock anyway
        pass


@contextmanager
def job_lock(directory: Path, name: str) -> Iterator[bool]:
    """Hold the `name` lock for the block. Yields False when another process holds it, so the
    caller can skip cleanly instead of running the same job twice."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.lock"
    # O_CREAT without O_TRUNC: truncating a file another process has locked is not our
    # business, and the contents are only there to say who holds it.
    handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o644), "r+")
    try:
        if not _acquire(handle):
            log.warning("another %s is already running (%s); skipping this one", name, path)
            yield False
            return
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid {os.getpid()} since {datetime.now().isoformat(timespec='seconds')}\n")
        handle.flush()
        try:
            yield True
        finally:
            _release(handle)
    finally:
        handle.close()
