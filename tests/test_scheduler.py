from app.cli import build_scheduler, pipeline_hours
from app.config import Settings


def test_pipeline_hours_line_up_with_digest_hours() -> None:
    digests = ["07:30", "19:30"]
    assert pipeline_hours(1, digests) == list(range(24))
    assert pipeline_hours(2, digests) == [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23]
    assert pipeline_hours(3, digests) == [1, 4, 7, 10, 13, 16, 19, 22]
    assert pipeline_hours(4, []) == [0, 4, 8, 12, 16, 20]


def test_build_scheduler_jobs(settings: Settings) -> None:
    settings.schedule.pipeline_every_hours = 2
    scheduler = build_scheduler(settings, lambda: None, lambda: None)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"pipeline", "digest-07:30", "digest-19:30"}
    assert (
        str(jobs["pipeline"].trigger) == "cron[hour='1,3,5,7,9,11,13,15,17,19,21,23', minute='0']"
    )
    assert str(jobs["digest-07:30"].trigger) == "cron[hour='7', minute='30']"
    assert str(jobs["pipeline"].trigger.timezone) == "Asia/Kolkata"
    assert jobs["pipeline"].max_instances == 1


# ---------------------------------------------------------------- catch-up runs


def test_a_normal_run_does_a_normal_runs_work(settings: Settings) -> None:
    from app.schedule import catch_up, settings_for_run

    for gap in (None, 0.5, 3.0):
        plan = catch_up(settings, gap)
        assert not plan.catching_up
        assert plan.max_stories == settings.pipeline.max_stories_per_run
        assert plan.lookback_hours == settings.pipeline.lookback_hours
        assert settings_for_run(settings, plan) is settings  # nothing copied, nothing changed


def test_a_run_after_a_night_off_does_the_missed_runs_work(settings: Settings) -> None:
    """The laptop is off most nights and Windows starts one run when it wakes. Measured
    2026-09-24: 19 hours of news, one run's 20 places, 1 of 251 new stories summarized."""
    from app.schedule import catch_up, settings_for_run

    settings.pipeline.max_stories_per_run = 20
    settings.pipeline.reserved_slots = {"IN": 5}
    settings.pipeline.catch_up_max_stories = 60
    settings.pipeline.catch_up_max_hours = 24
    settings.schedule.pipeline_every_hours = 3

    plan = catch_up(settings, 19.0)
    assert plan.slots == 7  # ceil(19 / 3)
    assert plan.max_stories == 60  # 7 x 20 = 140, held to the ceiling
    assert plan.lookback_hours == 20  # back to the last run, with an hour's overlap
    assert plan.reserved_slots == {"IN": 15}  # the reserve scales with the run

    sized = settings_for_run(settings, plan)
    assert sized.pipeline.max_stories_per_run == 60 and sized.pipeline.lookback_hours == 20
    assert settings.pipeline.max_stories_per_run == 20  # the original is untouched

    # Days off still read no further back than the ceiling.
    assert catch_up(settings, 72.0).lookback_hours == 24
