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


class LLMSettings(_Strict):
    summary_model: str
    reasoning_model: str
    temperature: dict[str, float] = Field(default_factory=dict)
    timeout_seconds: float = 60
    max_retries: int = 3

    def temperature_for(self, model: str) -> float | None:
        """Temperature to send for `model`, or None if it must not be sent."""
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
