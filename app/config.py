"""Load and validate settings.yaml, feeds.yaml and secrets from .env."""

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"

Region = Literal["US", "IN", "GLOBAL"]
REGIONS: tuple[Region, ...] = ("US", "IN", "GLOBAL")

GroupingScorer = Literal["title_token_set", "title_token_sort", "title_snippet_blend"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


LLMProviderName = Literal["gemini", "anthropic"]

# Environment variable holding each provider's API key.
PROVIDER_KEY_ENV: dict[str, str] = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
_PROVIDER_MODEL_PREFIXES: dict[str, tuple[str, ...]] = {
    "gemini": ("gemini-", "gemma-"),
    "anthropic": ("claude-",),
}


class RateLimitSettings(_Strict):
    """A model's quota. For Gemini, copy these from https://aistudio.google.com/rate-limit."""

    requests_per_minute: int = Field(gt=0)
    input_tokens_per_minute: int = Field(gt=0)  # AI Studio's TPM counts input tokens
    requests_per_day: int = Field(gt=0)  # the hard quota
    # New work stops here; retries of requests already started may use the rest of the quota.
    requests_per_day_budget: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _budget_within_quota(self) -> "RateLimitSettings":
        if self.requests_per_day_budget is not None and (
            self.requests_per_day_budget > self.requests_per_day
        ):
            raise ValueError("requests_per_day_budget can't exceed requests_per_day")
        return self

    @property
    def daily_budget(self) -> int:
        return self.requests_per_day_budget or self.requests_per_day


class LLMSettings(_Strict):
    provider: LLMProviderName = "gemini"
    summary_model: str
    reasoning_model: str
    temperature: dict[str, float] = Field(default_factory=dict)
    timeout_seconds: float = 60
    max_retries: int = Field(default=3, ge=0)
    rate_limits: dict[str, RateLimitSettings] = Field(default_factory=dict)
    # Daily request quotas reset at midnight in this time zone (Pacific time for Gemini).
    rate_limit_day_timezone: str = "America/Los_Angeles"

    @model_validator(mode="after")
    def _models_match_provider(self) -> "LLMSettings":
        prefixes = _PROVIDER_MODEL_PREFIXES[self.provider]
        for field_name in ("summary_model", "reasoning_model"):
            model = getattr(self, field_name)
            if not model.startswith(prefixes):
                raise ValueError(
                    f"llm.{field_name} {model!r} doesn't look like a {self.provider} model "
                    f"(expected a name starting with {' or '.join(prefixes)})"
                )
        ZoneInfo(self.rate_limit_day_timezone)
        return self

    @property
    def api_key_env(self) -> str:
        return PROVIDER_KEY_ENV[self.provider]

    def temperature_for(self, model: str) -> float | None:
        """Temperature to send for `model`, or None to use the model's default."""
        return self.temperature.get(model)


class HttpSettings(_Strict):
    timeout_seconds: float = 10
    max_attempts: int = Field(default=3, ge=1)
    backoff_base_seconds: float = 1.0
    user_agent: str


class PipelineSettings(_Strict):
    lookback_hours: int = Field(gt=0)
    story_attach_window_hours: int = Field(gt=0)
    max_stories_per_run: int = Field(gt=0)
    max_articles_per_story_for_llm: int = Field(gt=0)
    snippet_max_chars: int = Field(default=500, gt=0)


class DedupeSettings(_Strict):
    same_source_title_similarity: float = Field(ge=0, le=100)
    same_source_window_hours: int = Field(gt=0)
    syndication_title_similarity: float = Field(ge=0, le=100)


class GroupingSettings(_Strict):
    scorer: GroupingScorer
    threshold: float = Field(ge=0, le=100)


class RankingSettings(_Strict):
    w_sources: float
    w_region_diversity: float
    w_source_weight: float
    w_recency: float
    recency_half_life_hours: float = Field(gt=0)


class ImpactSettings(_Strict):
    max_impacts_per_story: int = Field(gt=0)


class ScoringSettings(_Strict):
    horizons_trading_days: list[int]
    vol_lookback_days: int
    hit_threshold_vol_multiple: float
    min_samples_to_show_rate: int


class DeliverySettings(_Strict):
    digest_times: list[str]
    max_stories_per_digest: int = Field(gt=0)
    breaking_alerts: bool = False
    breaking_importance_threshold: float

    @field_validator("digest_times")
    @classmethod
    def _check_times(cls, value: list[str]) -> list[str]:
        for item in value:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", item):
                raise ValueError(f"digest time must be HH:MM, got {item!r}")
        return value


class ScheduleSettings(_Strict):
    """For `newsdesk scheduler`: run the pipeline every N hours, on the hour, aligned so a run
    starts in the same hour as each digest time."""

    pipeline_every_hours: int

    @field_validator("pipeline_every_hours")
    @classmethod
    def _divides_day(cls, value: int) -> int:
        if value <= 0 or 24 % value:
            raise ValueError("pipeline_every_hours must divide 24 (1, 2, 3, 4, 6, 8, 12 or 24)")
        return value


class PathSettings(_Strict):
    database: str = "data/newsdesk.db"
    log_dir: str = "data/logs"


class Settings(_Strict):
    timezone: str
    llm: LLMSettings
    http: HttpSettings
    pipeline: PipelineSettings
    dedupe: DedupeSettings
    grouping: GroupingSettings
    ranking: RankingSettings
    impacts: ImpactSettings
    scoring: ScoringSettings
    delivery: DeliverySettings
    schedule: ScheduleSettings
    paths: PathSettings = Field(default_factory=PathSettings)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # raises if unknown
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def resolve_path(self, relative: str) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else ROOT_DIR / path


class FeedConfig(_Strict):
    """One RSS/Atom feed. Several entries may share a `name`: they are the same outlet."""

    name: str
    url: str
    region: Region
    weight: int = Field(ge=1, le=3)
    category_hint: str | None = None
    enabled: bool = True
    # Other spellings of the outlet name, e.g. how Google News labels it.
    aliases: list[str] = Field(default_factory=list)

    @property
    def is_google_news(self) -> bool:
        return urlsplit(self.url).hostname == "news.google.com"


class FeedsFile(_Strict):
    feeds: list[FeedConfig]

    @model_validator(mode="after")
    def _check_unique_urls(self) -> "FeedsFile":
        urls = [feed.url for feed in self.feeds]
        duplicates = {url for url in urls if urls.count(url) > 1}
        if duplicates:
            raise ValueError(f"duplicate feed urls: {sorted(duplicates)}")
        return self


def _read_yaml(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_settings(path: Path | None = None) -> Settings:
    return Settings.model_validate(_read_yaml(path or CONFIG_DIR / "settings.yaml"))


def load_feeds(path: Path | None = None, include_disabled: bool = False) -> list[FeedConfig]:
    feeds = FeedsFile.model_validate(_read_yaml(path or CONFIG_DIR / "feeds.yaml")).feeds
    return feeds if include_disabled else [feed for feed in feeds if feed.enabled]


def load_env() -> None:
    """Load .env from the project root without overriding real environment variables."""
    load_dotenv(ROOT_DIR / ".env", override=False)


def get_secret(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None
