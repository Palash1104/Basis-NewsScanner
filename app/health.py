"""`newsdesk health`: is the scheduler keeping up, and what are the LLM layers doing.

Reads the database and the settings only - no network and no LLM calls - so it is safe to run
while the scheduler is running.

Two of the numbers only exist from the run that first stored them (2026-09-21): layer B's
calls and declines are columns on `runs`, because a declined call writes no impacts and so
can't be counted afterwards. Older runs report zero and are left out of the rate.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import LLMDailyUsage, Run
from app.pipeline.rank import RERANK_FALLBACK_NOTE
from app.pipeline.scoring import TrackRow, track_record
from app.schedule import pipeline_hours

DEFAULT_DAYS = 7
# A slot this recent may still be running: it matches the scheduler's own misfire grace.
SLOT_GRACE = timedelta(minutes=15)


@dataclass
class SlotDay:
    """One local day's pipeline slots and what happened at each."""

    day: date
    ran: list[int] = field(default_factory=list)
    missed: list[int] = field(default_factory=list)
    pending: list[int] = field(default_factory=list)  # due, inside the grace window

    @property
    def expected(self) -> int:
        return len(self.ran) + len(self.missed) + len(self.pending)


def runs_since(session: Session, kind: str, since: datetime) -> list[Run]:
    query = select(Run).where(Run.kind == kind, Run.started_at >= since).order_by(Run.started_at)
    return list(session.scalars(query))


def slot_days(
    session: Session, settings: Settings, now: datetime, days: int = DEFAULT_DAYS
) -> list[SlotDay]:
    """Every pipeline slot of the last `days` days and whether a run covered it.

    A slot counts as covered by any pipeline run started between it and the next slot, so a
    manual `newsdesk run` counts: it does the same work as the scheduled one.
    """
    tz = settings.tz
    every = settings.schedule.pipeline_every_hours
    hours = pipeline_hours(every, settings.delivery.digest_times)
    local_now = now.astimezone(tz)
    first = datetime.combine(local_now.date() - timedelta(days=days - 1), time(0, 0), tzinfo=tz)
    starts = [run.started_at.astimezone(tz) for run in runs_since(session, "pipeline", first)]

    result: list[SlotDay] = []
    for offset in range(days):
        day = first.date() + timedelta(days=offset)
        entry = SlotDay(day=day)
        for hour in hours:
            slot = datetime.combine(day, time(hour), tzinfo=tz)
            if slot > local_now:
                continue  # not due yet
            window_end = slot + timedelta(hours=every)
            if any(slot <= start < window_end for start in starts):
                entry.ran.append(hour)
            elif local_now < slot + SLOT_GRACE:
                entry.pending.append(hour)
            else:
                entry.missed.append(hour)
        if entry.expected:
            result.append(entry)
    return result


def quota_days(settings: Settings, now: datetime, days: int = DEFAULT_DAYS) -> list[str]:
    """The last `days` quota days, newest first. Gemini's quota resets at midnight Pacific,
    so these are not local days."""
    tz = ZoneInfo(settings.llm.rate_limit_day_timezone)
    today = now.astimezone(tz).date()
    return [(today - timedelta(index)).isoformat() for index in range(days)]


def requests_per_day(
    session: Session, models: Sequence[str], days: Sequence[str]
) -> dict[tuple[str, str], int]:
    """Requests per (day, model), from the limiter's own daily counts."""
    query = select(LLMDailyUsage).where(
        LLMDailyUsage.day.in_(list(days)), LLMDailyUsage.model.in_(list(models))
    )
    return {(row.day, row.model): row.requests for row in session.scalars(query)}


def rerank_fallbacks(runs: Sequence[Run]) -> int:
    """Runs where the rerank failed and the computed importance order was kept."""
    return sum(
        1
        for run in runs
        if any(RERANK_FALLBACK_NOTE in str(error.get("error", "")) for error in run.errors or [])
    )


def layer_b_totals(runs: Sequence[Run]) -> tuple[int, int]:
    """Layer B calls and declines (`no_clear_impact`) across `runs`."""
    return (
        sum(run.llm_impact_calls for run in runs),
        sum(run.llm_impact_declines for run in runs),
    )


def proven_rules(session: Session, settings: Settings) -> list[TrackRow]:
    """Rules with enough judged calls for their hit rate to be shown (SPEC 7.9)."""
    minimum = settings.scoring.min_samples_to_show_rate
    stories = settings.scoring.min_stories_to_show_rate
    return [row for row in track_record(session, "rule_id") if row.shows_rate(minimum, stories)]


def _hours(hours: Sequence[int]) -> str:
    return ", ".join(f"{hour:02d}:00" for hour in hours)


def health_lines(
    session: Session, settings: Settings, now: datetime, days: int = DEFAULT_DAYS
) -> list[str]:
    tz = settings.tz
    local_now = now.astimezone(tz)
    every = settings.schedule.pipeline_every_hours
    slots = slot_days(session, settings, now, days)
    ran = sum(len(day.ran) for day in slots)
    missed = sum(len(day.missed) for day in slots)
    pending = sum(len(day.pending) for day in slots)

    lines = [f"BASIS health · {local_now:%d %b %Y %H:%M %Z} · last {days} days", ""]
    hours = pipeline_hours(every, settings.delivery.digest_times)
    lines.append(
        f"Pipeline slots (every {every}h at {_hours(hours)}; any run inside a slot counts)"
    )
    for day in slots:
        if day.missed and not day.ran:
            note = "  no runs"  # listing every hour of a dead day says nothing extra
        elif day.missed:
            note = f"  missed {_hours(day.missed)}"
        else:
            note = ""
        due = f"  due now {_hours(day.pending)}" if day.pending else ""
        lines.append(f"  {day.day:%a %d %b}  {len(day.ran)}/{day.expected}{note}{due}")
    summary = f"  {ran} of {ran + missed + pending} slots ran, {missed} missed"
    lines.append(summary + (f", {pending} due now" if pending else ""))

    first = datetime.combine(local_now.date() - timedelta(days=days - 1), time(0, 0), tzinfo=tz)
    pipeline_runs = runs_since(session, "pipeline", first)
    for kind, label in (("digest", "digests"), ("score", "scoring runs")):
        other = runs_since(session, kind, first)
        failed = sum(1 for run in other if run.errors)
        last = max((run.started_at.astimezone(tz) for run in other), default=None)
        when = f", last {last:%d %b %H:%M}" if last else ""
        lines.append(f"  {label}: {len(other)}, {failed} with errors{when}")

    lines += ["", "LLM requests per quota day (Pacific; budget stops new work, cap is the quota)"]
    # The rerank shares the summary model, so the same id can appear twice.
    models = list(dict.fromkeys([settings.llm.summary_model, settings.llm.reasoning_model]))
    days_listed = quota_days(settings, now, days)
    counts = requests_per_day(session, models, days_listed)
    width = max(len(model) for model in models) + 4
    over_budget = False
    lines.append("  " + "day".ljust(12) + "".join(model.ljust(width) for model in models))
    for day in days_listed:
        cells = []
        for model in models:
            limits = settings.llm.rate_limits.get(model)
            used = counts.get((day, model), 0)
            if limits is None:
                cells.append(f"{used} (no limits set)".ljust(width))
                continue
            # Past the budget is not an error: it stops new work, and retries of work already
            # started may use the rest of the quota.
            over = "*" if used > limits.daily_budget else ""
            cell = f"{used}{over} / {limits.daily_budget} (cap {limits.requests_per_day})"
            over_budget = over_budget or bool(over)
            cells.append(cell.ljust(width))
        lines.append("  " + day.ljust(12) + "".join(cells).rstrip())
    if over_budget:
        lines.append("  * past the budget: retries may use the rest of the quota")

    lines += [
        "",
        f"Rerank: {rerank_fallbacks(pipeline_runs)} of {len(pipeline_runs)} runs fell "
        "back to the computed importance order",
    ]
    calls, declines = layer_b_totals(pipeline_runs)
    if calls:
        lines.append(
            f"Layer B: {calls} calls, {declines} declined "
            f"({declines / calls:.0%} said no clear impact)"
        )
    else:
        lines.append("Layer B: no calls recorded in this window")

    rules = proven_rules(session, settings)
    minimum = settings.scoring.min_samples_to_show_rate
    lines.append(f"Rules at n>={minimum} judged calls:")
    if not rules:
        lines.append("  none yet")
    for row in rules:
        lines.append(
            f"  {row.key:<28} {row.horizon_days}d  {row.hits}/{row.judged} "
            f"({row.rate:.0%}), {len(row.stories)} stories"
        )
    return lines
