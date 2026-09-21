from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import Settings
from app.health import (
    health_lines,
    layer_b_totals,
    proven_rules,
    quota_days,
    rerank_fallbacks,
    slot_days,
)
from app.models import Event, Impact, ImpactScore, LLMDailyUsage, Run, Story
from app.pipeline.rank import RERANK_FALLBACK_NOTE

IST = ZoneInfo("Asia/Kolkata")
# Monday 21 Sep 2026, 11:20 IST: the 10:00 slot is over, the next one is 13:00.
NOW = datetime(2026, 9, 21, 11, 20, tzinfo=IST).astimezone(UTC)


def _run(session: Session, kind: str, at: datetime, **fields: object) -> Run:
    run = Run(kind=kind, started_at=at, finished_at=at, **fields)
    session.add(run)
    session.flush()
    return run


def _pipeline_at(session: Session, local: datetime, **fields: object) -> Run:
    return _run(session, "pipeline", local.astimezone(UTC), **fields)


def _local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=IST)


# ---------------------------------------------------------------- pipeline slots


def test_slots_count_a_run_anywhere_inside_them(session: Session, settings: Settings) -> None:
    _pipeline_at(session, _local(21, 7, 2))  # the scheduled run, two minutes late
    _pipeline_at(session, _local(21, 5, 30))  # a manual run inside the 04:00 slot

    today = slot_days(session, settings, NOW, days=1)[0]
    assert today.ran == [4, 7]
    # 01:00 and 10:00 are over with no run; 13:00 and later are not due yet.
    assert today.missed == [1, 10] and today.pending == []
    assert today.expected == 4


def test_a_slot_inside_the_grace_window_is_not_yet_missed(
    session: Session, settings: Settings
) -> None:
    just_after_ten = datetime(2026, 9, 21, 10, 5, tzinfo=IST).astimezone(UTC)
    today = slot_days(session, settings, just_after_ten, days=1)[0]
    assert today.pending == [10] and 10 not in today.missed
    assert today.missed == [1, 4, 7]


def test_a_day_with_no_runs_reports_every_slot_missed(session: Session, settings: Settings) -> None:
    days = slot_days(session, settings, NOW, days=3)
    assert [day.day.day for day in days] == [19, 20, 21]
    assert [len(day.missed) for day in days] == [8, 8, 4]
    assert all(not day.ran for day in days)


def test_health_lines_name_the_missed_hours(session: Session, settings: Settings) -> None:
    for hour in (1, 4, 7):
        _pipeline_at(session, _local(21, hour))
    lines = "\n".join(health_lines(session, settings, NOW, days=1))
    assert "3/4  missed 10:00" in lines
    assert "3 of 4 slots ran, 1 missed" in lines


# ---------------------------------------------------------------- quota days


def test_quota_days_are_pacific_not_local(settings: Settings) -> None:
    # 11:20 IST on the 21st is still the 20th in Pacific time, where the quota resets.
    assert quota_days(settings, NOW, days=2) == ["2026-09-20", "2026-09-19"]


def test_usage_is_shown_against_the_budget_and_the_cap(
    session: Session, settings: Settings
) -> None:
    model = settings.llm.summary_model
    limits = settings.llm.rate_limits[model]
    session.add(
        LLMDailyUsage(
            day="2026-09-20",
            provider=settings.llm.provider,
            model=model,
            requests=limits.daily_budget + 3,
        )
    )
    session.flush()
    lines = "\n".join(health_lines(session, settings, NOW, days=1))
    used = limits.daily_budget + 3
    # The star marks a day past the budget, which retries are allowed to be.
    assert f"{used}* / {limits.daily_budget} (cap {limits.requests_per_day})" in lines
    assert "* past the budget: retries may use the rest of the quota" in lines


# ---------------------------------------------------------------- LLM layers


def test_only_a_real_rerank_fallback_counts(session: Session) -> None:
    kept = _pipeline_at(
        session, _local(21, 4), errors=[{"stage": "rank", "error": f"{RERANK_FALLBACK_NOTE}: 503"}]
    )
    partial = _pipeline_at(
        session,
        _local(21, 7),
        errors=[{"stage": "rank", "error": "rerank returned 2 unknown id(s) and left out 1"}],
    )
    clean = _pipeline_at(session, _local(21, 10))
    assert rerank_fallbacks([kept, partial, clean]) == 1


def test_layer_b_decline_rate(session: Session, settings: Settings) -> None:
    _pipeline_at(session, _local(21, 4), llm_impact_calls=5, llm_impact_declines=3)
    _pipeline_at(session, _local(21, 7), llm_impact_calls=5, llm_impact_declines=2)
    runs = session.query(Run).all()
    assert layer_b_totals(runs) == (10, 5)
    lines = "\n".join(health_lines(session, settings, NOW, days=1))
    assert "Layer B: 10 calls, 5 declined (50% said no clear impact)" in lines


def test_runs_that_predate_the_counters_say_so(session: Session, settings: Settings) -> None:
    _pipeline_at(session, _local(21, 4))
    lines = "\n".join(health_lines(session, settings, NOW, days=1))
    assert "Layer B: no calls recorded in this window" in lines


# ---------------------------------------------------------------- track record


def _judged(session: Session, rule: str, count: int, hits: int) -> None:
    for index in range(count):
        story = Story(
            first_seen_at=NOW - timedelta(days=3),
            updated_at=NOW - timedelta(days=3),
            headline=f"h{index}",
            status="analyzed",
        )
        event = Event(
            story=story,
            event_type="geopolitical_conflict",
            countries=[],
            regions=[],
            entities=[],
            companies=[],
            channels=["oil_supply"],
            severity="escalation",
            policy_stance="not_applicable",
            is_new_development=True,
            model="m",
            prompt_version="event-v3",
            created_at=NOW - timedelta(days=3),
        )
        impact = Impact(
            story=story,
            event=event,
            symbol="BZ=F",
            direction="up",
            mechanism="m",
            order="first",
            confidence="high",
            origin="playbook",
            rule_id=rule,
            created_at=NOW - timedelta(days=3),
        )
        session.add_all([story, event, impact])
        session.flush()
        session.add(
            ImpactScore(
                impact_id=impact.id,
                horizon_days=1,
                outcome="hit" if index < hits else "miss",
                scored_at=NOW,
            )
        )
    session.flush()


def test_rules_are_listed_once_they_have_enough_judged_calls(
    session: Session, settings: Settings
) -> None:
    minimum = settings.scoring.min_samples_to_show_rate
    _judged(session, "oil_supply_shock", minimum - 1, hits=1)
    assert proven_rules(session, settings) == []
    assert "  none yet" in health_lines(session, settings, NOW, days=1)

    _judged(session, "oil_supply_shock", minimum, hits=minimum)
    (row,) = proven_rules(session, settings)
    assert row.key == "oil_supply_shock" and row.judged == 2 * minimum - 1
    lines = "\n".join(health_lines(session, settings, NOW, days=1))
    assert f"oil_supply_shock             1d  {minimum + 1}/{2 * minimum - 1}" in lines
