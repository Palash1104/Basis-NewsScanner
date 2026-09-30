"""When the scheduler runs the pipeline, and how much a run does. Kept out of `app/cli.py` so
`app/health.py` can use it without importing the CLI."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import Settings


def pipeline_hours(every_hours: int, digest_times: Sequence[str]) -> list[int]:
    """Hours (local time) to run the pipeline: every `every_hours`, lined up with the first
    digest's hour so a run starts in the same hour as the digest."""
    anchor = int(digest_times[0].split(":")[0]) % every_hours if digest_times else 0
    return list(range(anchor, 24, every_hours))


@dataclass(frozen=True)
class CatchUp:
    """How much one run does, given how long it has been since the last one."""

    slots: int  # how many scheduled runs' worth of work this one does (1 = a normal run)
    lookback_hours: int
    max_stories: int
    reserved_slots: dict[str, int]

    @property
    def catching_up(self) -> bool:
        return self.slots > 1


def catch_up(settings: Settings, gap_hours: float | None) -> CatchUp:
    """A run after downtime does the work of the runs it replaced.

    The laptop is off most nights, and Windows starts one catch-up run when it wakes (the
    rest are thinned by `schedule.min_run_gap_minutes`). That one run used to do one run's
    work: read the last 12 hours and summarize 20 stories, after 19 hours of news. Now it
    reads back to the previous run and summarizes 20 stories per missed slot, reserved slots
    scaled alike, up to `catch_up_max_hours` and `catch_up_max_stories`.
    """
    pipeline = settings.pipeline
    every = settings.schedule.pipeline_every_hours
    slots = max(1, math.ceil(gap_hours / every)) if gap_hours else 1
    size = min(pipeline.max_stories_per_run * slots, pipeline.catch_up_max_stories)
    # An hour's overlap, so an article published just before the last run is still in reach.
    wanted = math.ceil(gap_hours) + 1 if gap_hours else 0
    lookback = min(max(pipeline.lookback_hours, wanted), pipeline.catch_up_max_hours)
    scale = size / pipeline.max_stories_per_run
    reserved = {region: round(count * scale) for region, count in pipeline.reserved_slots.items()}
    return CatchUp(slots, lookback, size, reserved)


def settings_for_run(settings: Settings, plan: CatchUp) -> Settings:
    """A copy of `settings` sized for one run. Everything downstream reads the pipeline
    settings, so resizing them here is the whole change - and the original is untouched."""
    if not plan.catching_up and plan.lookback_hours == settings.pipeline.lookback_hours:
        return settings
    pipeline = settings.pipeline.model_copy(
        update={
            "lookback_hours": plan.lookback_hours,
            "max_stories_per_run": plan.max_stories,
            "reserved_slots": plan.reserved_slots,
        }
    )
    return settings.model_copy(update={"pipeline": pipeline})


def start_of_news_day(settings: Settings, now: datetime) -> datetime | None:
    """When "today" began, for the digest and for the feed's first page: the most recent
    `delivery.day_starts_at` at or before `now`, in the reader's zone. None when the setting
    is null and no floor applies.

    It is 22:00, not midnight (user, 2026-09-23). The pipeline's last run of the evening is at
    22:00 and the evening digest goes out at 19:30, so a midnight boundary dropped everything
    that broke in between: too old for the morning digest, already past for the evening one.
    Starting the day at 22:00 makes the night's news part of tomorrow's.
    """
    if not settings.delivery.day_starts_at:
        return None
    hour, minute = (int(part) for part in settings.delivery.day_starts_at.split(":"))
    local = now.astimezone(settings.tz)
    start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return start if start <= local else start - timedelta(days=1)
