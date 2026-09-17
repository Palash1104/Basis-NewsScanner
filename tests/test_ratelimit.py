from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import RateLimitSettings
from app.db import init_db, make_engine, make_session_factory
from app.llm.ratelimit import (
    DailyLimitReached,
    MemoryDailyUsageStore,
    MissingRateLimit,
    RateLimiter,
    SqlDailyUsageStore,
    estimate_input_tokens,
)

MODEL = "gemini-3.5-flash-lite"
PACIFIC = ZoneInfo("America/Los_Angeles")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _limiter(
    rpm: int = 100,
    tpm: int = 1_000_000,
    rpd: int = 1000,
    store=None,
    clock: FakeClock | None = None,
    now=lambda: datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
) -> tuple[RateLimiter, FakeClock]:
    clock = clock or FakeClock()
    limits = {
        MODEL: RateLimitSettings(
            requests_per_minute=rpm, input_tokens_per_minute=tpm, requests_per_day=rpd
        )
    }
    limiter = RateLimiter(
        limits,
        store if store is not None else MemoryDailyUsageStore(),
        PACIFIC,
        require_limits=True,
        clock=clock,
        sleep=clock.sleep,
        now=now,
    )
    return limiter, clock


def test_requests_per_minute_waits_for_the_window() -> None:
    limiter, clock = _limiter(rpm=2)
    limiter.acquire(MODEL, 10)
    clock.now += 5
    limiter.acquire(MODEL, 10)
    assert clock.sleeps == []
    # Third within a minute: wait until the first (t=0) leaves the window, from t=5, plus 1s margin.
    limiter.acquire(MODEL, 10)
    assert clock.sleeps == [pytest.approx(56.0)]


def test_input_tokens_per_minute_waits_and_uses_actual_counts() -> None:
    limiter, clock = _limiter(tpm=1000)
    first = limiter.acquire(MODEL, 600)
    limiter.settle(first, input_tokens=300, output_tokens=50)  # estimate was too high
    limiter.acquire(MODEL, 600)  # 300 + 600 fits
    assert clock.sleeps == []
    limiter.acquire(MODEL, 600)  # 900 + 600 doesn't: wait
    assert len(clock.sleeps) == 1


def test_oversized_request_is_capped_so_it_can_run_alone() -> None:
    limiter, clock = _limiter(tpm=1000)
    limiter.acquire(MODEL, 50_000)
    assert clock.sleeps == []


def test_daily_limit_raises_and_holds_across_limiter_instances() -> None:
    store = MemoryDailyUsageStore()
    limiter, _ = _limiter(rpd=2, store=store)
    limiter.acquire(MODEL, 10)
    limiter.acquire(MODEL, 10)
    with pytest.raises(DailyLimitReached, match="2/2 requests on 2026-09-17"):
        limiter.acquire(MODEL, 10)
    next_run, _ = _limiter(rpd=2, store=store)
    with pytest.raises(DailyLimitReached):
        next_run.acquire(MODEL, 10)


def test_daily_count_resets_at_midnight_pacific() -> None:
    store = MemoryDailyUsageStore()
    before, _ = _limiter(rpd=1, store=store, now=lambda: datetime(2026, 9, 17, 6, 59, tzinfo=UTC))
    after, _ = _limiter(rpd=1, store=store, now=lambda: datetime(2026, 9, 17, 7, 1, tzinfo=UTC))
    assert before.quota_day() == "2026-09-16" and after.quota_day() == "2026-09-17"
    before.acquire(MODEL, 10)
    after.acquire(MODEL, 10)  # a new Pacific day: allowed


def test_missing_limits() -> None:
    required = RateLimiter({}, MemoryDailyUsageStore(), PACIFIC, require_limits=True)
    with pytest.raises(MissingRateLimit):
        required.acquire(MODEL, 10)
    optional = RateLimiter({}, MemoryDailyUsageStore(), PACIFIC, require_limits=False)
    assert optional.acquire(MODEL, 10) is None


def test_sql_store_persists_counts(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "usage.db")
    init_db(engine)
    store = SqlDailyUsageStore(make_session_factory(engine), "gemini")
    limiter, _ = _limiter(rpd=3, store=store)
    reservation = limiter.acquire(MODEL, 10)
    limiter.settle(reservation, input_tokens=400, output_tokens=90)
    limiter.acquire(MODEL, 10)

    reopened = SqlDailyUsageStore(make_session_factory(engine), "gemini")
    assert reopened.requests("2026-09-17", MODEL) == 2
    assert (
        SqlDailyUsageStore(make_session_factory(engine), "anthropic").requests("2026-09-17", MODEL)
        == 0
    )


def test_estimate_is_conservative() -> None:
    assert estimate_input_tokens("a" * 300, "b" * 300) == 201
