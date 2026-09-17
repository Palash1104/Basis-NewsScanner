import asyncio

import httpx
import pytest

from app.net import request_with_retries


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _run(handler, max_attempts: int = 3) -> tuple[httpx.Response, int, SleepRecorder]:
    calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return handler(request, calls["n"])

    sleep = SleepRecorder()

    async def go() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.MockTransport(counting)) as client:
            return await request_with_retries(
                client,
                "GET",
                "https://example.com/feed",
                max_attempts=max_attempts,
                backoff_base=1.0,
                sleep=sleep,
            )

    return asyncio.run(go()), calls["n"], sleep


def test_retries_server_errors_then_succeeds() -> None:
    response, calls, sleep = _run(lambda req, n: httpx.Response(503 if n < 3 else 200))
    assert response.status_code == 200
    assert calls == 3
    # exponential backoff with up to 1s of jitter: [1, 2) then [2, 3)
    assert len(sleep.delays) == 2
    assert 1 <= sleep.delays[0] < 2 <= sleep.delays[1] < 3


def test_honors_retry_after() -> None:
    response, _, sleep = _run(
        lambda req, n: (
            httpx.Response(429, headers={"Retry-After": "7"}) if n == 1 else httpx.Response(200)
        )
    )
    assert response.status_code == 200
    assert sleep.delays == [7.0]


def test_does_not_retry_client_errors() -> None:
    response, calls, sleep = _run(lambda req, n: httpx.Response(404))
    assert response.status_code == 404
    assert calls == 1
    assert sleep.delays == []


def test_returns_last_error_response_after_max_attempts() -> None:
    response, calls, _ = _run(lambda req, n: httpx.Response(500), max_attempts=2)
    assert response.status_code == 500
    assert calls == 2


def test_retries_timeouts_and_raises_when_exhausted() -> None:
    def handler(request: httpx.Request, n: int) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(httpx.ConnectTimeout):
        _run(handler, max_attempts=3)
