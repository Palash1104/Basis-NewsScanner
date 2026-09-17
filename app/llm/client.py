"""The LLM layer.

The pipeline only uses `LLMClient.structured()`. Underneath, an `LLMProvider` sends one request
and returns text plus token counts. Two providers exist: `GeminiProvider` (default, Gemini API
over REST with httpx) and `AnthropicProvider` (anthropic SDK). The client adds what every
provider needs: rate limiting, retries for transient errors, pydantic validation with one
retry that includes the validation error (SPEC 14), and token accounting.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import anthropic
import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.config import LLMSettings, get_secret
from app.llm.prompts import validation_retry_prompt
from app.llm.ratelimit import (
    DailyLimitReached,
    MemoryDailyUsageStore,
    MissingRateLimit,
    RateLimiter,
    SqlDailyUsageStore,
    estimate_input_tokens,
)
from app.net import backoff_seconds, retry_after_seconds

log = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
TRANSIENT_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
MAX_RETRY_WAIT_SECONDS = 60.0

# ---------------------------------------------------------------- errors


class LLMError(Exception):
    pass


class LLMConfigError(LLMError):
    """The configured provider can't be used: missing API key or rate limits."""


class LLMCallError(LLMError):
    """The request failed: network, auth, bad request, or transient errors that didn't clear."""


class LLMQuotaError(LLMCallError):
    """A quota is used up (daily limit reached, or still rate limited after retries).
    Callers should stop making LLM calls for the rest of the run."""


class LLMOutputError(LLMError):
    """The model answered, but the output was blocked, truncated, or invalid after a retry."""


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        transient: bool,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.status = status
        self.retry_after = retry_after


# ---------------------------------------------------------------- provider interface

Finish = Literal["complete", "max_tokens", "blocked"]


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int  # includes thinking tokens when the provider reports them separately
    finish: Finish
    detail: str  # provider's own finish/stop reason, for logs and errors


class LLMProvider(Protocol):
    name: str

    def generate_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float | None,
    ) -> ProviderResponse:
        """Send one request asking for JSON matching `schema`. No retries.
        Raises ProviderError on failure (with `transient` set for retryable errors)."""
        ...


# ---------------------------------------------------------------- Gemini

# JSON Schema keywords the Gemini API accepts in responseJsonSchema (per its API reference).
GEMINI_SCHEMA_KEYWORDS = frozenset(
    {
        "$id",
        "$defs",
        "$ref",
        "$anchor",
        "type",
        "format",
        "title",
        "description",
        "enum",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "anyOf",
        "oneOf",
        "properties",
        "additionalProperties",
        "required",
        "propertyOrdering",
    }
)


def gemini_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """The pydantic model's JSON Schema, keeping only keywords Gemini supports."""

    def clean(node: Any) -> Any:
        if isinstance(node, list):
            return [clean(item) for item in node]
        if not isinstance(node, dict):
            return node
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key not in GEMINI_SCHEMA_KEYWORDS:
                continue
            if key in ("properties", "$defs"):
                result[key] = {name: clean(sub) for name, sub in value.items()}
            else:
                result[key] = clean(value)
        return result

    return clean(schema.model_json_schema())


class GeminiProvider:
    """Gemini API `models.generateContent` with structured JSON output. The API key goes in
    the x-goog-api-key header, never in the URL."""

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._http = httpx.Client(
            base_url=GEMINI_API_BASE, timeout=timeout_seconds, transport=transport
        )

    def generate_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float | None,
    ) -> ProviderResponse:
        generation_config: dict[str, Any] = {
            "responseMimeType": "application/json",
            "responseJsonSchema": gemini_json_schema(schema),
            "maxOutputTokens": max_tokens,
        }
        if temperature is not None:
            generation_config["temperature"] = temperature
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": generation_config,
        }
        try:
            response = self._http.post(
                f"/models/{model}:generateContent",
                json=body,
                headers={"x-goog-api-key": self._api_key},
            )
        except httpx.TransportError as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc}", transient=True) from None
        if response.status_code != 200:
            raise self._error(response)

        data = response.json()
        usage = data.get("usageMetadata") or {}
        input_tokens = int(usage.get("promptTokenCount") or 0)
        output_tokens = int(usage.get("candidatesTokenCount") or 0) + int(
            usage.get("thoughtsTokenCount") or 0
        )
        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            return ProviderResponse(
                "", input_tokens, output_tokens, "blocked", f"prompt blocked: {block_reason}"
            )
        candidates = data.get("candidates") or []
        if not candidates:
            return ProviderResponse("", input_tokens, output_tokens, "blocked", "no candidates")

        candidate = candidates[0]
        reason = candidate.get("finishReason") or ""
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        finish: Finish
        if reason in ("", "STOP"):
            finish = "complete"
        elif reason == "MAX_TOKENS":
            finish = "max_tokens"
        else:
            finish = "blocked"
        return ProviderResponse(text, input_tokens, output_tokens, finish, reason or "STOP")

    @staticmethod
    def _error(response: httpx.Response) -> ProviderError:
        try:
            message = (response.json().get("error") or {}).get("message") or ""
        except ValueError:
            message = response.text[:300]
        return ProviderError(
            f"HTTP {response.status_code}: {message}".strip(),
            transient=response.status_code in TRANSIENT_STATUS,
            status=response.status_code,
            retry_after=retry_after_seconds(response),
        )

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------- Anthropic


class AnthropicProvider:
    """Anthropic Messages API with a JSON-schema output format. SDK retries are off: the
    client's retry loop handles them so every attempt passes through the rate limiter."""

    name = "anthropic"

    def __init__(self, api_key: str | None, timeout_seconds: float, client: Any = None) -> None:
        self._client = client or anthropic.Anthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=0
        )

    def generate_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float | None,
    ) -> ProviderResponse:
        # anthropic SDK 1.x dropped sampling parameters from its signatures; models that still
        # accept them get the configured value through extra_body.
        extra_body = {"temperature": temperature} if temperature is not None else None
        try:
            response = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}
                },
                extra_body=extra_body,
            )
        except anthropic.APIStatusError as exc:
            retry_after = None
            header = exc.response.headers.get("retry-after") if exc.response is not None else None
            if header:
                try:
                    retry_after = float(header)
                except ValueError:
                    retry_after = None
            raise ProviderError(
                f"{type(exc).__name__}: {exc}",
                transient=exc.status_code in TRANSIENT_STATUS or exc.status_code >= 500,
                status=exc.status_code,
                retry_after=retry_after,
            ) from None
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc}", transient=True) from None
        except anthropic.AnthropicError as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc}", transient=False) from None

        text = "".join(block.text for block in response.content if block.type == "text")
        finish: Finish = {"refusal": "blocked", "max_tokens": "max_tokens"}.get(
            response.stop_reason, "complete"
        )
        return ProviderResponse(
            text,
            response.usage.input_tokens,
            response.usage.output_tokens,
            finish,
            str(response.stop_reason),
        )


# ---------------------------------------------------------------- client used by the pipeline


@dataclass
class TokenUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    value: T
    model: str
    input_tokens: int
    output_tokens: int


def describe_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "output"
        lines.append(f"- {location}: {error['msg']}")
    return "\n".join(lines)


class LLMClient:
    def __init__(
        self,
        settings: LLMSettings,
        provider: LLMProvider,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.limiter = limiter
        self.usage = TokenUsage()
        self._sleep = sleep

    def structured[T: BaseModel](
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int,
        purpose: str,
    ) -> StructuredResult[T]:
        """Ask for JSON matching `schema`. If it fails validation, retry once with the rejected
        answer and the validation error included.

        Raises LLMQuotaError (stop calling), LLMCallError, or LLMOutputError.
        """
        prompt = user
        input_tokens = output_tokens = 0
        error_text = ""
        for attempt in (1, 2):
            response = self._generate(model, system, prompt, schema, max_tokens, purpose)
            input_tokens += response.input_tokens
            output_tokens += response.output_tokens
            if response.finish == "blocked":
                raise LLMOutputError(f"{purpose}: no usable output ({response.detail})")
            if response.finish == "max_tokens":
                raise LLMOutputError(f"{purpose}: output was cut off at max_tokens={max_tokens}")
            try:
                value = schema.model_validate_json(response.text)
            except ValidationError as exc:
                error_text = describe_validation_error(exc)
                log.warning(
                    "llm %s: attempt %d failed validation:\n%s", purpose, attempt, error_text
                )
                prompt = validation_retry_prompt(user, response.text, error_text)
                continue
            return StructuredResult(value, model, input_tokens, output_tokens)
        raise LLMOutputError(f"{purpose}: output invalid after retry:\n{error_text}")

    def _generate(
        self,
        model: str,
        system: str,
        prompt: str,
        schema: type[BaseModel],
        max_tokens: int,
        purpose: str,
    ) -> ProviderResponse:
        """One logical request: rate-limited, with retries for transient errors."""
        attempts = self.settings.max_retries + 1
        temperature = self.settings.temperature_for(model)
        for attempt in range(1, attempts + 1):
            reservation = None
            if self.limiter is not None:
                try:
                    reservation = self.limiter.acquire(model, estimate_input_tokens(system, prompt))
                except DailyLimitReached as exc:
                    raise LLMQuotaError(f"{purpose}: {exc}") from None
                except MissingRateLimit as exc:
                    raise LLMConfigError(f"{purpose}: {exc}") from None
            try:
                response = self.provider.generate_json(
                    model=model,
                    system=system,
                    user=prompt,
                    schema=schema,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except ProviderError as exc:
                if not exc.transient:
                    raise LLMCallError(f"{purpose}: {exc}") from None
                if attempt == attempts:
                    if exc.status == 429:
                        raise LLMQuotaError(
                            f"{purpose}: still rate limited after {attempts} attempts: {exc}"
                        ) from None
                    raise LLMCallError(
                        f"{purpose}: failed after {attempts} attempts: {exc}"
                    ) from None
                delay = min(
                    exc.retry_after or backoff_seconds(attempt, 1.0), MAX_RETRY_WAIT_SECONDS
                )
                log.warning(
                    "llm %s: attempt %d/%d failed (%s), retrying in %.1fs",
                    purpose,
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                self._sleep(delay)
                continue

            if self.limiter is not None:
                self.limiter.settle(reservation, response.input_tokens, response.output_tokens)
            self.usage.calls += 1
            self.usage.input_tokens += response.input_tokens
            self.usage.output_tokens += response.output_tokens
            log.info(
                "llm %s: provider=%s model=%s input_tokens=%d output_tokens=%d finish=%s",
                purpose,
                self.provider.name,
                model,
                response.input_tokens,
                response.output_tokens,
                response.detail,
            )
            return response
        raise AssertionError("unreachable")


def make_llm_client(
    settings: LLMSettings,
    session_factory: sessionmaker[Session] | None = None,
) -> LLMClient:
    """Build the client for `settings.provider`.

    Raises LLMConfigError if the provider's API key is missing, or if the provider is Gemini
    and the summary model has no rate limits configured (they must come from AI Studio).
    """
    key_env = settings.api_key_env
    api_key = get_secret(key_env)
    if not api_key:
        raise LLMConfigError(f"{key_env} is not set (llm.provider is {settings.provider})")

    provider: LLMProvider
    if settings.provider == "gemini":
        missing = [m for m in (settings.summary_model,) if m not in settings.rate_limits]
        if missing:
            raise LLMConfigError(
                f"llm.rate_limits has no entry for {', '.join(missing)}. Copy the model's "
                "requests per minute, input tokens per minute and requests per day from "
                "https://aistudio.google.com/rate-limit into config/settings.yaml."
            )
        provider = GeminiProvider(api_key, settings.timeout_seconds)
    else:
        provider = AnthropicProvider(api_key, settings.timeout_seconds)

    store = (
        SqlDailyUsageStore(session_factory, provider.name)
        if session_factory is not None
        else MemoryDailyUsageStore()
    )
    limiter = RateLimiter(
        settings.rate_limits,
        store,
        ZoneInfo(settings.rate_limit_day_timezone),
        require_limits=settings.provider == "gemini",
    )
    return LLMClient(settings, provider, limiter)
