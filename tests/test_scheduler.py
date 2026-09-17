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
