"""HTTP requests with timeouts, retries and backoff, shared by feeds and Telegram."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.config import HttpSettings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 60.0

Sleep = Callable[[float], Awaitable[None]]


def make_client(settings: HttpSettings, **kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.timeout_seconds,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
        **kwargs,
    )


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return None  # HTTP-date form; fall back to normal backoff


def backoff_seconds(attempt: int, base: float) -> float:
    return base * 2 ** (attempt - 1) + random.uniform(0, base)


async def request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_attempts: int,
    backoff_base: float,
    sleep: Sleep = asyncio.sleep,
    log_label: str | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying timeouts, connection errors, 429 and 5xx.

    Returns the final response (which may still be an error status: callers decide).
    Raises the last transport error if every attempt failed to get a response.
    `log_label` replaces the URL in log lines (use it when the URL contains a secret).
    """
    label = log_label or url
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt == max_attempts:
                raise
            delay = backoff_seconds(attempt, backoff_base)
            # With a label the URL may be secret, and exception text can repeat the URL.
            detail = type(exc).__name__ if log_label else f"{type(exc).__name__}: {exc}"
            log.warning(
                "%s %s failed (%s), attempt %d/%d, retrying in %.1fs",
                method,
                label,
                detail,
                attempt,
                max_attempts,
                delay,
            )
        else:
            if response.status_code not in RETRYABLE_STATUS or attempt == max_attempts:
                return response
            delay = retry_after_seconds(response) or backoff_seconds(attempt, backoff_base)
            log.warning(
                "%s %s returned %d, attempt %d/%d, retrying in %.1fs",
                method,
                label,
                response.status_code,
                attempt,
                max_attempts,
                delay,
            )
        await sleep(delay)
    raise AssertionError("unreachable")
