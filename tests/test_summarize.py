from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import Settings
from app.llm.client import LLMClient, ProviderError
from app.llm.prompts import SUMMARY_PROMPT_VERSION
from app.models import Article, Story
from app.pipeline.summarize import resummarize_reason, select_articles, summarize_stories
from tests.conftest import NOW
from tests.fakes import FakeProvider, provider_response, summary_json


def _llm(settings: Settings, provider: FakeProvider) -> LLMClient:
    return LLMClient(settings.llm, provider, sleep=lambda seconds: None)


def _article(source: str, region: str = "US", weight: int = 2, hours_ago: float = 1) -> Article:
    return Article(
        url=f"https://example.com/{source}/{region}/{weight}/{hours_ago}",
        source_name=source,
        source_region=region,
        source_weight=weight,
        title=f"Headline from {source}",
        snippet="",
        published_at=NOW - timedelta(hours=hours_ago),
        fetched_at=NOW,
    )


def _story(session: Session, articles: list[Article], **fields: object) -> Story:
    story = Story(first_seen_at=NOW, updated_at=NOW - timedelta(hours=5), headline="raw title")
    for key, value in fields.items():
        setattr(story, key, value)
    story.articles = articles
    session.add(story)
    session.flush()
    return story


# ---------------------------------------------------------------- when to summarize


def test_new_story_needs_summary() -> None:
    story = Story(status="new", processed_article_count=0, processed_source_regions=[])
    assert resummarize_reason(story, [_article("A")]) == "new story"


def test_summarized_story_rules() -> None:
    story = Story(status="summarized", processed_article_count=2, processed_source_regions=["US"])
    assert resummarize_reason(story, [_article("A"), _article("B")]) is None
    assert resummarize_reason(story, [_article("A"), _article("B"), _article("C")]) is None
    four = [_article("A"), _article("B"), _article("C"), _article("D")]
    assert resummarize_reason(story, four) == "2 new articles"
    new_region = [_article("A"), _article("B"), _article("C", region="IN")]
    assert resummarize_reason(story, new_region) == "new source region IN"


def test_failed_story_retried_only_when_it_changes() -> None:
    story = Story(status="failed", processed_article_count=3, processed_source_regions=["US"])
    three = [_article("A"), _article("B"), _article("C")]
    assert resummarize_reason(story, three) is None
    assert resummarize_reason(story, [*three, _article("D"), _article("E")]) == "2 new articles"


def test_select_articles_prefers_distinct_regions_then_outlets() -> None:
    articles = [
        _article("BBC", "GLOBAL", 3, hours_ago=1),
        _article("BBC", "GLOBAL", 3, hours_ago=2),
        _article("CNBC", "US", 2),
        _article("Politico", "US", 2),
        _article("The Hindu", "IN", 3),
        _article("Hindu", "IN", 3, hours_ago=3),  # same outlet as The Hindu
    ]
    chosen = select_articles(articles, 4)
    assert len(chosen) == 4
    assert {a.source_region for a in chosen[:3]} == {"GLOBAL", "US", "IN"}
    assert len({a.source_name.removeprefix("The ") for a in chosen}) == 4


# ---------------------------------------------------------------- summarize_stories


def test_success_updates_story(session: Session, settings: Settings) -> None:
    story = _story(session, [_article("A"), _article("B", region="IN")])
    fake = FakeProvider(
        responses=[
            provider_response(
                summary_json(sources_disagree=True, disagreement_note="Counts differ.")
            )
        ]
    )
    result = summarize_stories(session, [story], _llm(settings, fake), settings, NOW)

    assert result.summarized == [story.id]
    assert story.status == "summarized"
    assert story.headline == "Parliament passes new trade bill"
    assert story.category == "Economy & Markets" and story.regions == ["India"]
    assert story.sources_disagree and story.disagreement_note == "Counts differ."
    assert story.processed_article_count == 2
    assert story.processed_source_regions == ["IN", "US"]
    assert story.prompt_version == SUMMARY_PROMPT_VERSION
    assert story.model == settings.llm.summary_model
    assert story.updated_at == NOW


def test_unchanged_story_makes_no_llm_call(session: Session, settings: Settings) -> None:
    story = _story(
        session,
        [_article("A")],
        status="summarized",
        processed_article_count=1,
        processed_source_regions=["US"],
    )
    fake = FakeProvider(responses=[])
    result = summarize_stories(session, [story], _llm(settings, fake), settings, NOW)
    assert result.skipped_unchanged == 1 and fake.calls == []


def test_invalid_output_marks_failed_and_is_not_retried_next_run(
    session: Session, settings: Settings
) -> None:
    story = _story(session, [_article("A"), _article("B")])
    bad = provider_response(summary_json(summary="One sentence only."))
    fake = FakeProvider(responses=[bad, bad])
    llm = _llm(settings, fake)

    result = summarize_stories(session, [story], llm, settings, NOW)
    assert story.status == "failed" and len(result.failed) == 1
    assert story.processed_article_count == 2
    assert story.summary is None

    again = summarize_stories(session, [story], llm, settings, NOW)
    assert again.skipped_unchanged == 1 and len(fake.calls) == 2


def test_api_error_leaves_story_new_and_continues(session: Session, settings: Settings) -> None:
    broken = _story(session, [_article("A")])
    fine = _story(session, [_article("B")])
    fake = FakeProvider(
        responses=[
            ProviderError("HTTP 400: bad request", transient=False, status=400),
            provider_response(summary_json()),
        ]
    )
    result = summarize_stories(session, [broken, fine], _llm(settings, fake), settings, NOW)
    assert broken.status == "new" and broken.processed_article_count == 0
    assert result.call_errors[0][0] == broken.id
    assert result.summarized == [fine.id]


def test_quota_error_stops_remaining_stories(session: Session, settings: Settings) -> None:
    settings.llm.max_retries = 1
    first = _story(session, [_article("A")])
    second = _story(session, [_article("B")])
    fake = FakeProvider(responses=[ProviderError("HTTP 429", transient=True, status=429)] * 2)

    result = summarize_stories(session, [first, second], _llm(settings, fake), settings, NOW)

    assert result.stopped and "rate limited" in result.stopped
    assert len(fake.calls) == 2  # both attempts for the first story, none for the second
    assert first.status == "new" and second.status == "new"
    assert result.summarized == [] and result.call_errors == []
