from collections import defaultdict

import pytest
from pydantic import ValidationError

from app.config import (
    DeliverySettings,
    FeedsFile,
    LLMSettings,
    RateLimitSettings,
    ScheduleSettings,
    Settings,
    load_feeds,
)


def test_settings_file_loads(settings: Settings) -> None:
    assert settings.tz.key == "Asia/Kolkata"
    assert settings.llm.summary_model
    assert settings.llm.reasoning_model


def test_temperature_only_for_listed_models(settings: Settings) -> None:
    settings.llm.temperature = {"model-a": 0.2}
    assert settings.llm.temperature_for("model-a") == 0.2
    assert settings.llm.temperature_for("model-b") is None


def test_provenance_records_the_sampling_settings_sent(settings: Settings) -> None:
    settings.llm.temperature = {"model-a": 0.2}
    settings.llm.seed = 7
    call = settings.llm.provenance("model-a", "summary-v3")
    assert (call.model, call.prompt_version) == ("model-a", "summary-v3")
    assert (call.temperature, call.seed) == (0.2, 7)
    assert settings.llm.provenance("model-b", "summary-v3").temperature is None


def test_no_seed_is_recorded_for_a_provider_that_ignores_it(settings: Settings) -> None:
    settings.llm.seed = 7
    settings.llm.provider = "anthropic"
    assert settings.llm.seed_for("claude-haiku-4-5") is None


def test_feeds_file_loads_and_outlets_have_one_region() -> None:
    feeds = load_feeds(include_disabled=True)
    assert feeds
    regions: dict[str, set[str]] = defaultdict(set)
    for feed in feeds:
        regions[feed.name].add(feed.region)
    assert all(len(values) == 1 for values in regions.values()), regions


def test_disabled_feeds_are_skipped_by_default() -> None:
    enabled = load_feeds()
    everything = load_feeds(include_disabled=True)
    assert all(feed.enabled for feed in enabled)
    assert len(everything) >= len(enabled)


def test_duplicate_feed_urls_rejected() -> None:
    feed = {"name": "A", "url": "https://example.com/rss", "region": "US", "weight": 1}
    with pytest.raises(ValidationError, match="duplicate feed urls"):
        FeedsFile.model_validate({"feeds": [feed, {**feed, "name": "B"}]})


def test_bad_digest_time_rejected() -> None:
    with pytest.raises(ValidationError):
        DeliverySettings(
            digest_times=["7:30"], max_stories_per_digest=10, breaking_importance_threshold=5
        )


def test_google_news_detection() -> None:
    feeds = FeedsFile.model_validate(
        {
            "feeds": [
                {
                    "name": "G",
                    "url": "https://news.google.com/rss?hl=en",
                    "region": "US",
                    "weight": 1,
                },
                {
                    "name": "X",
                    "url": "https://example.com/?next=news.google.com",
                    "region": "US",
                    "weight": 1,
                },
            ]
        }
    ).feeds
    assert [feed.is_google_news for feed in feeds] == [True, False]


def test_llm_defaults_to_gemini(settings: Settings) -> None:
    assert settings.llm.provider == "gemini"
    assert settings.llm.api_key_env == "GEMINI_API_KEY"
    assert settings.llm.summary_model.startswith("gemini-")


def test_model_ids_must_match_provider() -> None:
    with pytest.raises(ValidationError, match="doesn't look like a gemini model"):
        LLMSettings(summary_model="claude-haiku-4-5", reasoning_model="gemini-3.8-flash")
    anthropic = LLMSettings(
        provider="anthropic", summary_model="claude-haiku-4-5", reasoning_model="claude-sonnet-5"
    )
    assert anthropic.api_key_env == "ANTHROPIC_API_KEY"


def test_budget_cannot_exceed_quota() -> None:
    with pytest.raises(ValidationError, match="can't exceed"):
        RateLimitSettings(
            requests_per_minute=15,
            input_tokens_per_minute=250_000,
            requests_per_day=500,
            requests_per_day_budget=600,
        )
    assert (
        RateLimitSettings(
            requests_per_minute=1, input_tokens_per_minute=1, requests_per_day=9
        ).daily_budget
        == 9
    )


def test_schedule_interval_must_divide_the_day() -> None:
    with pytest.raises(ValidationError, match="must divide 24"):
        ScheduleSettings(pipeline_every_hours=5)
    assert ScheduleSettings(pipeline_every_hours=3).pipeline_every_hours == 3


def test_configured_limits_budget_within_quota(settings: Settings) -> None:
    for limits in settings.llm.rate_limits.values():
        assert limits.daily_budget <= limits.requests_per_day
