"""Client-side rate limiting so LLM calls stay inside the provider's quotas.

Three limits per model, matching how the Gemini API measures quota: requests per minute,
input tokens per minute, and requests per day. Minute limits use a sliding 60-second window
and wait for room. The daily count is stored in the database so it holds across runs, and
resets at midnight in the configured time zone (Pacific time for Gemini). When the daily
limit is reached, `acquire` raises instead of waiting.
"""

import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import RateLimitSettings
from app.models import LLMDailyUsage

log = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0
SAFETY_SECONDS = 1.0  # wait slightly past the window edge so clock skew can't trip the limit


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


@dataclass
class Reservation:
    model: str
    day: str
    event: list[float]  # [timestamp, input tokens] inside the minute window


class RateLimiter:
    def __init__(
        self,
        limits: Mapping[str, RateLimitSettings],
        store: DailyUsageStore,
        day_timezone: ZoneInfo,
        require_limits: bool,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._limits = limits
        self._store = store
        self._tz = day_timezone
        self._require = require_limits
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._events: dict[str, deque[list[float]]] = defaultdict(deque)

    def quota_day(self) -> str:
        return self._now().astimezone(self._tz).date().isoformat()

    def acquire(self, model: str, estimated_input_tokens: int) -> Reservation | None:
        """Block until a request to `model` fits the minute limits, then reserve it.

        Raises DailyLimitReached when today's requests are used up, and MissingRateLimit if
        limits are required but not configured for the model.
        """
        limits = self._limits.get(model)
        if limits is None:
            if self._require:
                raise MissingRateLimit(f"no rate limits configured for {model}")
            return None

        day = self.quota_day()
        used_today = self._store.requests(day, model)
        if used_today >= limits.requests_per_day:
            raise DailyLimitReached(
                f"{model}: daily limit reached ({used_today}/{limits.requests_per_day} requests "
                f"on {day}, resets at midnight {self._tz.key})"
            )

        tokens = float(min(max(estimated_input_tokens, 1), limits.input_tokens_per_minute))
        events = self._events[model]
        while True:
            now = self._clock()
            while events and events[0][0] <= now - WINDOW_SECONDS:
                events.popleft()
            used_requests = len(events)
            used_tokens = sum(event[1] for event in events)
            if (
                used_requests < limits.requests_per_minute
                and used_tokens + tokens <= limits.input_tokens_per_minute
            ):
                break
            wait = max(events[0][0] + WINDOW_SECONDS - now + SAFETY_SECONDS, 0.1)
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

        event = [now, tokens]
        events.append(event)
        self._store.add(day, model, requests=1)
        return Reservation(model, day, event)

    def settle(
        self, reservation: Reservation | None, input_tokens: int, output_tokens: int
    ) -> None:
        """Replace the token estimate with the actual count reported by the provider."""
        if reservation is None:
            return
        if input_tokens > 0:
            reservation.event[1] = float(input_tokens)
        self._store.add(
            reservation.day,
            reservation.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def estimate_input_tokens(*texts: str) -> int:
    """Conservative estimate (about 3 characters per token) used until the real count is known."""
    return sum(len(text) for text in texts) // 3 + 1
