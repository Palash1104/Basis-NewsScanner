from collections import defaultdict

import pytest
from pydantic import ValidationError

from app.config import DeliverySettings, FeedsFile, Settings, load_feeds


def test_settings_file_loads(settings: Settings) -> None:
    assert settings.tz.key == "Asia/Kolkata"
    assert settings.llm.summary_model
    assert settings.llm.reasoning_model


def test_temperature_only_for_listed_models(settings: Settings) -> None:
    settings.llm.temperature = {"model-a": 0.2}
    assert settings.llm.temperature_for("model-a") == 0.2
    assert settings.llm.temperature_for("model-b") is None


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
