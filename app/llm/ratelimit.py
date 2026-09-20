"""Client-side rate limiting so LLM calls stay inside the provider's quotas.

Three limits per model, matching how the Gemini API measures quota: requests per minute,
input tokens per minute, and requests per day. Minute limits use a sliding 60-second window
and wait for room. With the database-backed window (`SqlMinuteWindow`) every request is
recorded in `llm_requests`, so the window counts calls from all processes using the database
(a smoke test just before a run, a manual run while the scheduler is up), not just this one.
The daily count is stored in the database so it holds across runs, and
resets at midnight in the configured time zone (Pacific time for Gemini; that is early
afternoon in India). When the daily limit is reached, `acquire` raises instead of waiting.

Two daily numbers: the quota (`requests_per_day`) and an optional budget
(`requests_per_day_budget`). New work stops at the budget; retries of work already started may
continue up to the quota.
"""

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import RateLimitSettings
from app.models import LLMDailyUsage, LLMRequest

log = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0
SAFETY_SECONDS = 1.0  # wait slightly past the window edge so clock skew can't trip the limit
KEEP_SECONDS = 3600.0  # request rows older than this are deleted


class RateLimitError(Exception):
    pass


class DailyLimitReached(RateLimitError):
    pass


class MissingRateLimit(RateLimitError):
    pass


class DailyUsageStore(Protocol):
    def requests(self, day: str, model: str) -> int: ...

    def add(
        self, day: str, model: str, requests: int = 0, input_tokens: int = 0, output_tokens: int = 0
    ) -> None: ...


class MemoryDailyUsageStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])

    def requests(self, day: str, model: str) -> int:
        return self.rows[(day, model)][0]

    def add(
        self, day: str, model: str, requests: int = 0, input_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        row = self.rows[(day, model)]
        row[0] += requests
        row[1] += input_tokens
        row[2] += output_tokens


class SqlDailyUsageStore:
    """Persists counts in `llm_daily_usage`, committing each update in its own session."""

    def __init__(self, session_factory: sessionmaker[Session], provider: str) -> None:
        self._session_factory = session_factory
        self._provider = provider

    def _row(self, session: Session, day: str, model: str) -> LLMDailyUsage | None:
        return session.scalars(
            select(LLMDailyUsage).where(
                LLMDailyUsage.day == day,
                LLMDailyUsage.provider == self._provider,
                LLMDailyUsage.model == model,
            )
        ).one_or_none()

    def requests(self, day: str, model: str) -> int:
        with self._session_factory() as session:
            row = self._row(session, day, model)
            return row.requests if row else 0

    def add(
        self, day: str, model: str, requests: int = 0, input_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        with self._session_factory() as session:
            row = self._row(session, day, model)
            if row is None:
                row = LLMDailyUsage(
                    day=day,
                    provider=self._provider,
                    model=model,
                    requests=0,
                    input_tokens=0,
                    output_tokens=0,
                )
                session.add(row)
            row.requests += requests
            row.input_tokens += input_tokens
            row.output_tokens += output_tokens
            session.commit()


class MinuteWindow(Protocol):
    """Recent requests per model, for the per-minute limits. Times are epoch seconds.

    A caller reserves a slot first and then checks what was reserved ahead of it, so two
    processes can't both take the last slot: the earlier reservation wins.
    """

    def reserve(self, model: str, at: float, input_tokens: float) -> int: ...

    def ahead(self, model: str, reservation: int, since: float) -> tuple[int, float, float | None]:
        """(requests, input tokens, oldest time) reserved after `since`, before `reservation`."""
        ...

    def release(self, reservation: int) -> None: ...

    def settle(self, reservation: int, input_tokens: float) -> None: ...


class MemoryMinuteWindow:
    """This process only (tests, or no database)."""

    def __init__(self) -> None:
        self._rows: dict[int, list] = {}  # id -> [model, at, input tokens]
        self._next_id = 1

    def reserve(self, model: str, at: float, input_tokens: float) -> int:
        for row_id in [i for i, row in self._rows.items() if row[1] < at - KEEP_SECONDS]:
            del self._rows[row_id]
        row_id, self._next_id = self._next_id, self._next_id + 1
        self._rows[row_id] = [model, at, input_tokens]
        return row_id

    def ahead(self, model: str, reservation: int, since: float) -> tuple[int, float, float | None]:
        rows = [
            row
            for row_id, row in self._rows.items()
            if row_id < reservation and row[0] == model and row[1] > since
        ]
        return len(rows), sum(row[2] for row in rows), min((row[1] for row in rows), default=None)

    def release(self, reservation: int) -> None:
        self._rows.pop(reservation, None)

    def settle(self, reservation: int, input_tokens: float) -> None:
        if reservation in self._rows:
            self._rows[reservation][2] = input_tokens


def _utc(epoch_seconds: float) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, UTC)


class SqlMinuteWindow:
    """Shared by every process using the database (`llm_requests`), one commit per change."""

    def __init__(self, session_factory: sessionmaker[Session], provider: str) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._pruned_at: float | None = None

    def reserve(self, model: str, at: float, input_tokens: float) -> int:
        with self._session_factory() as session:
            # Pruning is housekeeping, not per-request work: doing it on every reservation
            # means a write on every LLM call, which fights the pipeline for the database.
            if self._pruned_at is None or at - self._pruned_at > KEEP_SECONDS:
                session.execute(
                    delete(LLMRequest).where(LLMRequest.requested_at < _utc(at - KEEP_SECONDS))
                )
                self._pruned_at = at
            row = LLMRequest(
                provider=self._provider,
                model=model,
                requested_at=_utc(at),
                input_tokens=round(input_tokens),
            )
            session.add(row)
            session.commit()
            return row.id

    def ahead(self, model: str, reservation: int, since: float) -> tuple[int, float, float | None]:
        with self._session_factory() as session:
            count, tokens, oldest = session.execute(
                select(
                    func.count(LLMRequest.id),
                    func.coalesce(func.sum(LLMRequest.input_tokens), 0),
                    func.min(LLMRequest.requested_at),
                ).where(
                    LLMRequest.provider == self._provider,
                    LLMRequest.model == model,
                    LLMRequest.id < reservation,
                    LLMRequest.requested_at > _utc(since),
                )
            ).one()
        # func.min bypasses the column type, so SQLite hands back a naive UTC string.
        if isinstance(oldest, str):
            oldest = datetime.fromisoformat(oldest)
        if oldest is not None and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        return int(count), float(tokens), oldest.timestamp() if oldest is not None else None

    def release(self, reservation: int) -> None:
        with self._session_factory() as session:
            session.execute(delete(LLMRequest).where(LLMRequest.id == reservation))
            session.commit()

    def settle(self, reservation: int, input_tokens: float) -> None:
        with self._session_factory() as session:
            row = session.get(LLMRequest, reservation)
            if row is not None:
                row.input_tokens = round(input_tokens)
                session.commit()


@dataclass(frozen=True)
class QuotaStatus:
    model: str
    day: str  # quota day (YYYY-MM-DD in the quota time zone)
    used: int
    budget: int
    limit: int
    resets_at: datetime  # next midnight in the quota time zone, timezone-aware


@dataclass
class Reservation:
    model: str
    day: str
    window_id: int  # this request's slot in the minute window


class RateLimiter:
    def __init__(
        self,
        limits: Mapping[str, RateLimitSettings],
        store: DailyUsageStore,
        day_timezone: ZoneInfo,
        require_limits: bool,
        # Wall-clock epoch seconds (not monotonic): the window may be shared across processes.
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        display_timezone: ZoneInfo | None = None,
        window: MinuteWindow | None = None,
    ) -> None:
        self._limits = limits
        self._store = store
        self._window = window if window is not None else MemoryMinuteWindow()
        self._tz = day_timezone
        self._require = require_limits
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._display_tz = display_timezone or day_timezone

    def quota_day(self) -> str:
        return self._now().astimezone(self._tz).date().isoformat()

    def next_reset(self) -> datetime:
        """The next midnight in the quota time zone."""
        local = self._now().astimezone(self._tz)
        tomorrow = local.date() + timedelta(days=1)
        return datetime.combine(tomorrow, dt_time(0, 0), tzinfo=self._tz)

    def format_reset(self) -> str:
        return self.next_reset().astimezone(self._display_tz).strftime("%d %b %H:%M %Z")

    def status(self, model: str) -> QuotaStatus | None:
        limits = self._limits.get(model)
        if limits is None:
            return None
        day = self.quota_day()
        return QuotaStatus(
            model=model,
            day=day,
            used=self._store.requests(day, model),
            budget=limits.daily_budget,
            limit=limits.requests_per_day,
            resets_at=self.next_reset(),
        )

    def acquire(
        self, model: str, estimated_input_tokens: int, retry: bool = False
    ) -> Reservation | None:
        """Block until a request to `model` fits the minute limits, then reserve it.

        `retry` marks a retry of work already started: it may use the quota beyond the budget.
        Raises DailyLimitReached when the day's budget (or, for retries, the quota) is used up,
        and MissingRateLimit if limits are required but not configured for the model.
        """
        limits = self._limits.get(model)
        if limits is None:
            if self._require:
                raise MissingRateLimit(f"no rate limits configured for {model}")
            return None

        day = self.quota_day()
        used_today = self._store.requests(day, model)
        cap, label = (
            (limits.requests_per_day, "daily quota")
            if retry
            else (limits.daily_budget, "daily budget")
        )
        if used_today >= cap:
            raise DailyLimitReached(
                f"{model}: {label} reached ({used_today}/{cap} requests on quota day {day}; "
                f"resets {self.format_reset()})"
            )

        tokens = float(min(max(estimated_input_tokens, 1), limits.input_tokens_per_minute))
        while True:
            now = self._clock()
            window_id = self._window.reserve(model, now, tokens)
            used_requests, used_tokens, oldest = self._window.ahead(
                model, window_id, now - WINDOW_SECONDS
            )
            if (
                used_requests < limits.requests_per_minute
                and used_tokens + tokens <= limits.input_tokens_per_minute
            ):
                break
            self._window.release(window_id)
            wait = max((oldest or now) + WINDOW_SECONDS - now + SAFETY_SECONDS, 0.1)
            log.info(
                "rate limit %s: %d/%d requests and %d/%d input tokens in the last minute; "
                "waiting %.1fs",
                model,
                used_requests,
                limits.requests_per_minute,
                used_tokens,
                limits.input_tokens_per_minute,
                wait,
            )
            self._sleep(wait)

        self._store.add(day, model, requests=1)
        return Reservation(model, day, window_id)

    def settle(
        self, reservation: Reservation | None, input_tokens: int, output_tokens: int
    ) -> None:
        """Replace the token estimate with the actual count reported by the provider."""
        if reservation is None:
            return
        if input_tokens > 0:
            self._window.settle(reservation.window_id, float(input_tokens))
        self._store.add(
            reservation.day,
            reservation.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def estimate_input_tokens(*texts: str) -> int:
    """Conservative estimate (about 3 characters per token) used until the real count is known."""
    return sum(len(text) for text in texts) // 3 + 1
