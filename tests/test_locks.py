import os
from pathlib import Path

import pytest

from app.locks import job_lock


def test_a_second_holder_is_turned_away(tmp_path: Path) -> None:
    with job_lock(tmp_path, "pipeline") as first:
        assert first
        with job_lock(tmp_path, "pipeline") as second:
            assert not second


def test_the_lock_is_free_again_afterwards(tmp_path: Path) -> None:
    with job_lock(tmp_path, "pipeline") as first:
        assert first
    with job_lock(tmp_path, "pipeline") as again:
        assert again


def test_a_crash_inside_the_block_still_releases_it(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), job_lock(tmp_path, "pipeline") as acquired:
        assert acquired
        raise RuntimeError("run crashed")
    with job_lock(tmp_path, "pipeline") as again:
        assert again


def test_each_job_kind_has_its_own_lock(tmp_path: Path) -> None:
    """A slow pipeline run must not stop the digest that follows it."""
    with job_lock(tmp_path, "pipeline") as pipeline, job_lock(tmp_path, "digest") as digest:
        assert pipeline and digest


def test_the_lock_file_says_who_held_it(tmp_path: Path) -> None:
    # Read after release: Windows locks are mandatory, so while the lock is held the file
    # can't be read at all - which is itself a reliable "someone is running" signal there.
    with job_lock(tmp_path, "score") as acquired:
        assert acquired
    assert (
        (tmp_path / "score.lock")
        .read_text(encoding="utf-8")
        .startswith(f"pid {os.getpid()} since ")
    )
