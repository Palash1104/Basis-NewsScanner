"""When the scheduler runs the pipeline. Kept out of `app/cli.py` so `app/health.py` can use
it without importing the CLI."""

from collections.abc import Sequence


def pipeline_hours(every_hours: int, digest_times: Sequence[str]) -> list[int]:
    """Hours (local time) to run the pipeline: every `every_hours`, lined up with the first
    digest's hour so a run starts in the same hour as the digest."""
    anchor = int(digest_times[0].split(":")[0]) % every_hours if digest_times else 0
    return list(range(anchor, 24, every_hours))
