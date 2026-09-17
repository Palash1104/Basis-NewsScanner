"""Send messages through the Telegram Bot HTTP API. The bot token is part of every URL, so it
is kept out of log lines and error messages."""

import asyncio
from collections.abc import Sequence
from typing import Any

import httpx

from app.config import HttpSettings
from app.net import Sleep, make_client, request_with_retries

API_BASE = "https://api.telegram.org"


class TelegramError(Exception):
    pass


def _result(response: httpx.Response, what: str) -> Any:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.is_success and body.get("ok"):
        return body.get("result")
    description = body.get("description") or f"HTTP {response.status_code}"
    raise TelegramError(f"{what} failed: {description}")


async def _call(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    settings: HttpSettings,
    what: str,
    sleep: Sleep,
    payload: dict[str, Any] | None = None,
) -> Any:
    try:
        response = await request_with_retries(
            client,
            "POST",
            f"{API_BASE}/bot{token}/{method}",
            max_attempts=settings.max_attempts,
            backoff_base=settings.backoff_base_seconds,
            sleep=sleep,
            log_label=f"telegram {what}",
            json=payload or {},
        )
    except httpx.TransportError as exc:
        detail = str(exc).replace(token, "***")
        raise TelegramError(f"{what} failed: {type(exc).__name__}: {detail}") from None
    return _result(response, what)


async def send_messages(
    messages: Sequence[str],
    token: str,
    chat_id: str,
    settings: HttpSettings,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
) -> int:
    """Send messages in order with HTML parse mode and link previews off.
    Stops at the first failure (raising TelegramError). Returns how many were sent."""
    sent = 0
    async with make_client(settings, transport=transport) as client:
        for index, text in enumerate(messages, start=1):
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            }
            what = f"sendMessage {index}/{len(messages)}"
            await _call(client, token, "sendMessage", settings, what, sleep, payload)
            sent += 1
    return sent


async def get_me(
    token: str,
    settings: HttpSettings,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
) -> dict[str, Any]:
    async with make_client(settings, transport=transport) as client:
        return await _call(client, token, "getMe", settings, "getMe", sleep)
