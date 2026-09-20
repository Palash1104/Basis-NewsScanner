"""Test doubles: a fake LLM provider, a fake Anthropic SDK client, Gemini response bodies,
and small RSS builders."""

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from email.utils import format_datetime
from html import escape
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.llm.client import ProviderResponse


def fake_response(
    text: str, stop_reason: str = "end_turn", input_tokens: int = 120, output_tokens: int = 60
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class FakeMessages:
    """Returns queued responses (or raises queued exceptions), or calls a responder."""

    def __init__(
        self,
        responses: list[Any] | None = None,
        responder: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.responder(kwargs) if self.responder else self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeAnthropic:
    def __init__(self, **kwargs: Any) -> None:
        self.messages = FakeMessages(**kwargs)


def summary_json(**overrides: Any) -> str:
    data = {
        "headline": "Parliament passes new trade bill",
        "summary": "Lawmakers approved a trade bill on Tuesday. It lowers tariffs on imports.",
        "category": "Economy & Markets",
        "regions": ["India"],
        "sources_disagree": False,
        "disagreement_note": None,
    }
    data.update(overrides)
    return json.dumps(data)


def provider_response(
    text: str,
    finish: str = "complete",
    input_tokens: int = 120,
    output_tokens: int = 60,
    detail: str = "STOP",
) -> ProviderResponse:
    return ProviderResponse(text, input_tokens, output_tokens, finish, detail)  # type: ignore[arg-type]


class FakeProvider:
    """An LLMProvider returning queued items (responses or exceptions) or a responder's."""

    name = "fake"

    def __init__(
        self,
        responses: list[Any] | None = None,
        responder: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    def generate_json(self, **kwargs: Any) -> ProviderResponse:
        self.calls.append(kwargs)
        item = self.responder(kwargs) if self.responder else self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def event_json(**overrides: Any) -> str:
    data = {
        "event_type": "sanctions_trade_policy",
        "countries": ["United States", "India"],
        "entities": ["US Congress"],
        "companies": [],
        "channels": ["tariffs_trade"],
        "severity": "escalation",
        "policy_stance": "not_applicable",
        "is_new_development": True,
    }
    data.update(overrides)
    return json.dumps(data)


def echo_summary_responder(kwargs: dict[str, Any]) -> ProviderResponse:
    """A valid summary whose headline is the first article title in the prompt, or a fixed
    valid event for event-extraction requests."""
    if kwargs["schema"].__name__ == "EventExtraction":
        return provider_response(event_json())
    match = re.search(r'published="[^"]*">([^\n<]*)', kwargs["user"])
    title = match.group(1) if match else "Untitled story"
    headline = " ".join(title.split()[:12])
    return provider_response(summary_json(headline=headline, regions=["Global"], category="Other"))


def gemini_body(
    text: str,
    finish: str = "STOP",
    prompt_tokens: int = 200,
    candidate_tokens: int = 80,
    thought_tokens: int = 15,
) -> dict[str, Any]:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}
        ],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": candidate_tokens,
            "thoughtsTokenCount": thought_tokens,
            "totalTokenCount": prompt_tokens + candidate_tokens + thought_tokens,
        },
    }


def rss(items: list[tuple[str, str, datetime, str]]) -> bytes:
    """items: (title, link, published_at, description)."""
    entries = "".join(
        f"<item><title>{escape(title)}</title><link>{escape(link)}</link>"
        f"<description>{escape(description)}</description>"
        f"<pubDate>{format_datetime(published, usegmt=True)}</pubDate></item>"
        for title, link, published, description in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>Test</title><link>https://example.com</link><description>t</description>"
        f"{entries}</channel></rss>"
    ).encode()


class FakeEmbedder:
    """Deterministic stand-in for the sentence-transformers model: a normalized bag of words
    hashed into 256 dimensions, so cosine similarity tracks shared words."""

    name = "fake-bag-of-words"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        vectors = np.zeros((len(texts), 256), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                if len(word) > 2:
                    vectors[row, hash(word) % 256] += 1.0
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.where(norms == 0, 1, norms)


class FakePrices:
    """A PriceProvider over hand-made bars: {symbol: {interval: [Bar, ...]}}. Records the
    (symbol, interval) of every request so tests can check the cache avoids re-fetching."""

    def __init__(self, series: dict[str, dict[str, list[Any]]] | None = None) -> None:
        self.series = series or {}
        self.calls: list[tuple[str, str]] = []
        self.fail: set[str] = set()

    def bars(self, symbol: str, interval: str, start: datetime, end: datetime) -> list[Any]:
        from app.pipeline.prices import PriceUnavailable

        self.calls.append((symbol, interval))
        if symbol in self.fail:
            raise PriceUnavailable("HTTPError: 429 too many requests")
        bars = self.series.get(symbol, {}).get(interval, [])
        return [bar for bar in bars if start <= bar.ts <= end]


def bar_series(
    start: datetime,
    count: int,
    step: timedelta,
    price: float,
    volume: float = 1000.0,
    drift: float = 1.0,
) -> list[Any]:
    """`count` bars from `start`, each `drift` times the previous close."""
    from app.pipeline.prices import Bar

    bars = []
    for index in range(count):
        close = price * (drift**index)
        bars.append(Bar(start + step * index, close, close, close, close, volume))
    return bars
