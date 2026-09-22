"""Load and validate settings.yaml, feeds.yaml, assets.yaml and secrets from .env."""

import os
import re
from dataclasses import dataclass
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
GroupingMethod = Literal["embedding", "title"]


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
    # A fixed sampling seed, so identical requests give identical answers (Gemini only;
    # Anthropic has no equivalent and ignores it). None lets the provider choose.
    seed: int | None = None

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

    def seed_for(self, model: str) -> int | None:
        """The seed actually sent for `model`. Gemini takes one; Anthropic has no equivalent,
        so nothing is recorded there rather than recording a seed that was ignored."""
        return self.seed if self.provider == "gemini" else None

    def provenance(self, model: str, prompt_version: str) -> "CallProvenance":
        """What to store alongside an output produced by `model` (SPEC section 14)."""
        return CallProvenance(
            model=model,
            prompt_version=prompt_version,
            temperature=self.temperature_for(model),
            seed=self.seed_for(model),
        )


@dataclass(frozen=True)
class CallProvenance:
    """How one stored LLM output was produced. Stored on every row holding model output
    (stories, events, impacts, rule_disagreements) so the track record can be split if the
    model, the prompt, the temperature or the seed ever changes."""

    model: str
    prompt_version: str
    temperature: float | None = None
    seed: int | None = None


class HttpSettings(_Strict):
    timeout_seconds: float = 10
    max_attempts: int = Field(default=3, ge=1)
    backoff_base_seconds: float = 1.0
    user_agent: str


class PipelineSettings(_Strict):
    lookback_hours: int = Field(gt=0)
    # How many top-ranked stories the reasoning model reorders before summarizing (SPEC 7.4,
    # Phase 5). 0 keeps the computed importance order.
    rerank_candidates: int = Field(default=40, ge=0)
    story_attach_window_hours: int = Field(gt=0)
    max_stories_per_run: int = Field(gt=0)
    # Slots inside max_stories_per_run kept for stories carried only by one region's outlets
    # (SPEC 7.4). Without this, a domestic Indian story cannot reach the summarizer: it is
    # covered by 3-5 Indian outlets and one region, while the cutoff is set by international
    # stories with 8-10 outlets across three regions.
    reserved_slots: dict[Region, int] = Field(default_factory=dict)
    # How many of a region's stories join the rerank's candidates, so the model can order
    # them before the slots are filled.
    reserved_candidate_pool: int = Field(default=10, ge=0)
    # How old a story may be (from first_seen_at) and still take a reserved slot. The general
    # pool is unaffected: this only stops the reserve from spending its five slots on stories
    # that have been sitting unsummarized for days.
    reserved_max_age_hours: int | None = Field(default=48, gt=0)
    max_articles_per_story_for_llm: int = Field(gt=0)
    snippet_max_chars: int = Field(default=500, gt=0)

    @model_validator(mode="after")
    def _reserved_fit_in_the_run(self) -> "PipelineSettings":
        total = sum(self.reserved_slots.values())
        if total > self.max_stories_per_run:
            raise ValueError(
                f"reserved_slots total {total} exceeds max_stories_per_run "
                f"{self.max_stories_per_run}"
            )
        return self


class DedupeSettings(_Strict):
    same_source_title_similarity: float = Field(ge=0, le=100)
    same_source_window_hours: int = Field(gt=0)
    syndication_title_similarity: float = Field(ge=0, le=100)


class GroupingSettings(_Strict):
    # "embedding" (default): cosine similarity of an article to each story's centroid.
    # "title": the rapidfuzz title matcher, also the automatic fallback if the model can't load.
    method: GroupingMethod = "embedding"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_threshold: float = Field(default=0.55, gt=0, lt=1)
    # An article must also score at least this against the story's seed (earliest news
    # article), so stories can't drift into topic blobs. None disables the check.
    seed_threshold: float | None = Field(default=None, gt=0, lt=1)
    # Embedding matches scoring in this range are logged for later retuning.
    borderline_log_range: tuple[float, float] = (0.45, 0.65)
    # Title matcher settings (fallback).
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
    # "Already moved" (SPEC 7.8): the move must be at least this many times the asset's
    # typical daily move. Deliberately separate from scoring.hit_threshold_vol_multiple,
    # which answers a different question (was the call right).
    moved_vol_multiple: float = Field(default=1.0, gt=0)
    vol_min_returns: int = Field(default=10, gt=0)  # fewer usable days: show the move, no label
    # The LLM impact layer (SPEC 7.7 B) runs on at most this many stories per run, the most
    # significant ones after the rerank. 0 turns the layer off.
    llm_max_stories_per_run: int = Field(default=5, ge=0)
    price_stale_days: int = Field(default=5, gt=0)  # newest bar older than this: unusable


class ScoringSettings(_Strict):
    horizons_trading_days: list[int]
    vol_lookback_days: int
    # How far the excess return must move to count as a hit or a miss. Not the same as
    # impacts.moved_vol_multiple, which asks whether the news is already in the price.
    hit_threshold_vol_multiple: float
    min_samples_to_show_rate: int
    # A rate also needs this many distinct stories behind it. n counts asset-calls, and one
    # story calls ten assets at once, so five calls from one story is one observation.
    min_stories_to_show_rate: int = Field(default=3, gt=0)
    # Below this many stories a rate is shown, but marked early: it is a direction, not a
    # measurement.
    early_rate_below_stories: int = Field(default=10, gt=0)
    # An impact that still has no reference price this long after its story is unscorable.
    reference_grace_days: int = Field(default=7, gt=0)
    # Extra days beyond a horizon's expected completion before giving up on its data.
    score_grace_days: int = Field(default=7, gt=0)


class DeliverySettings(_Strict):
    digest_times: list[str]
    max_stories_per_digest: int = Field(gt=0)
    # Impacts shown per story before the rest become a "+N more" line.
    max_impacts_in_digest: int = Field(default=6, gt=0)
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
    # Daily scoring, after the US close (SPEC 7.9), in settings.timezone.
    score_time: str = "03:30"

    @field_validator("score_time")
    @classmethod
    def _check_score_time(cls, value: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f"score_time must be HH:MM, got {value!r}")
        return value

    @field_validator("pipeline_every_hours")
    @classmethod
    def _divides_day(cls, value: int) -> int:
        if value <= 0 or 24 % value:
            raise ValueError("pipeline_every_hours must divide 24 (1, 2, 3, 4, 6, 8, 12 or 24)")
        return value


class PathSettings(_Strict):
    database: str = "data/newsdesk.db"
    log_dir: str = "data/logs"
    # One lock file per job kind, so a slow run and the next scheduled one can't overlap.
    lock_dir: str = "data/locks"


class WebSettings(_Strict):
    """The web UI's own tunables. The watchlist is a starting point, not a store: the page's
    "Edit watchlist" keeps a per-browser choice, because the web app never writes."""

    watchlist: list[str] = Field(default_factory=list)
    watchlist_max: int = Field(default=12, gt=0)


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
    web: WebSettings = Field(default_factory=WebSettings)
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


AssetType = Literal["commodity", "fx", "rate", "index", "stock", "etf"]
# One sector per asset (approved list), so the web UI can group assets and label stories by
# the sectors their impacts touch.
Sector = Literal[
    "Energy",
    "Metals",
    "Agriculture",
    "Financials",
    "Technology",
    "Pharma",
    "Consumer",
    "Industrials",
    "Utilities",
    "Real Estate",
    "Telecom",
    "Transport",
    "Defence",
    "Macro",
]


class AssetConfig(_Strict):
    """One asset in config/assets.yaml (SPEC §8). `symbol` is the Yahoo Finance ticker."""

    symbol: str = Field(min_length=1)
    name: str
    display_name: str
    type: AssetType
    country: str
    sector: Sector
    tags: list[str] = Field(default_factory=list)
    # Yahoo's exchange code (e.g. NSI), currency and exchange time zone, copied from the
    # validate-tickers report. The time zone makes trading-day counting exact (SPEC 7.9).
    exchange: str | None = None
    currency: str | None = None
    timezone: str | None = None
    up_means: str | None = None  # e.g. "rupee weaker" for USD/INR
    # Yahoo names a person confirmed are this asset, so the name check stops flagging them.
    approved_yahoo_names: list[str] = Field(default_factory=list)


class AssetsFile(_Strict):
    assets: list[AssetConfig]

    @model_validator(mode="after")
    def _check_unique_symbols(self) -> "AssetsFile":
        symbols = [asset.symbol for asset in self.assets]
        duplicates = {symbol for symbol in symbols if symbols.count(symbol) > 1}
        if duplicates:
            raise ValueError(f"duplicate asset symbols: {sorted(duplicates)}")
        return self


def _read_yaml(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_settings(path: Path | None = None) -> Settings:
    return Settings.model_validate(_read_yaml(path or CONFIG_DIR / "settings.yaml"))


def load_feeds(path: Path | None = None, include_disabled: bool = False) -> list[FeedConfig]:
    feeds = FeedsFile.model_validate(_read_yaml(path or CONFIG_DIR / "feeds.yaml")).feeds
    return feeds if include_disabled else [feed for feed in feeds if feed.enabled]


def load_assets(path: Path | None = None) -> list[AssetConfig]:
    return AssetsFile.model_validate(_read_yaml(path or CONFIG_DIR / "assets.yaml")).assets


def load_env() -> None:
    """Load .env from the project root without overriding real environment variables."""
    load_dotenv(ROOT_DIR / ".env", override=False)


def get_secret(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None
