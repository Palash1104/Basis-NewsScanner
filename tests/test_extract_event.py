import itertools
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.llm.client import LLMClient, ProviderError
from app.llm.prompts import EVENT_PROMPT_VERSION, event_user_prompt
from app.llm.schemas import EventExtraction
from app.models import Article, Event, Story
from app.pipeline.countries import canonical_country, normalize_countries
from app.pipeline.extract_event import extract_events, normalize_event, pending_event_stories
from tests.conftest import NOW
from tests.fakes import FakeProvider, event_json, provider_response

_urls = itertools.count()


def _llm(settings: Settings, provider: FakeProvider) -> LLMClient:
    return LLMClient(settings.llm, provider, sleep=lambda seconds: None)


def _story(session: Session, hours_ago: float = 1, **fields: object) -> Story:
    story = Story(
        first_seen_at=NOW,
        updated_at=NOW,
        headline="US passes sanctions bill",
        summary="Congress passed a bill. It allows tariffs on India.",
        category="Geopolitics",
        regions=["US", "India"],
        status="summarized",
    )
    for key, value in fields.items():
        setattr(story, key, value)
    story.articles = [
        Article(
            url=f"https://example.com/{next(_urls)}",
            source_name="BBC",
            source_region="GLOBAL",
            source_weight=3,
            title="House passes Russia sanctions bill",
            snippet="The bill allows 100% tariffs <b>on</b> buyers of Russian oil.",
            published_at=NOW - timedelta(hours=hours_ago),
            fetched_at=NOW,
        )
    ]
    session.add(story)
    session.flush()
    return story


def _event(**overrides: object) -> EventExtraction:
    return EventExtraction.model_validate_json(event_json(**overrides))


# ---------------------------------------------------------------- schema and normalization


@pytest.mark.parametrize(
    "change",
    [
        {"event_type": "war"},
        {"channels": ["oil_supply", "vibes"]},
        {"severity": "catastrophic"},
        {"policy_stance": "hawkish-ish"},
        {"regions": ["US"]},  # regions come from the story, not the model
    ],
)
def test_schema_rejects_values_outside_the_spec(change: dict) -> None:
    with pytest.raises(ValidationError):
        _event(**change)


def test_names_are_cleaned_and_deduplicated() -> None:
    event = _event(entities=["  Federal   Reserve", "federal reserve", "", "RBI"])
    assert event.entities == ["Federal Reserve", "RBI"]


def test_none_mixed_with_real_channels_is_dropped_and_noted() -> None:
    event, notes = normalize_event(_event(channels=["none", "oil_supply", "shipping_routes"]))
    assert event.channels == ["oil_supply", "shipping_routes"]
    assert notes == [
        "model returned 'none' together with oil_supply, shipping_routes; dropped 'none'"
    ]


def test_empty_channels_become_none() -> None:
    event, notes = normalize_event(_event(channels=[]))
    assert event.channels == ["none"] and "no channels" in notes[0]


def test_countries_are_made_canonical_and_unmapped_names_noted() -> None:
    raw = ["U.S.", "the United States", "India", "Türkiye", "European Union", "Korea"]
    event, notes = normalize_event(_event(countries=raw))
    assert event.countries == ["United States", "India", "Turkey", "European Union", "Korea"]
    assert notes == ["unmapped country name(s), kept as written: European Union, Korea"]


def test_country_aliases() -> None:
    assert canonical_country("USA") == canonical_country("united states of america")
    assert canonical_country("UAE") == "United Arab Emirates"
    assert canonical_country("the Philippines") == "Philippines"
    assert canonical_country("Congo") is None  # ambiguous: left for a person to see
    assert normalize_countries(["Gaza", "Palestine"]) == (["Palestine"], [])


def test_prompt_escapes_text_and_states_the_severity_rule() -> None:
    story = Story(headline="A <b> headline", summary="Rates rose. Markets fell.")
    prompt = event_user_prompt(story.headline, story.summary, [])
    assert "<headline>A &lt;b&gt; headline</headline>" in prompt
    assert "de_escalation: the development eases" in prompt
    assert "whatever its size" in prompt
    assert EVENT_PROMPT_VERSION == "event-v3"


# ---------------------------------------------------------------- extraction step


def test_extraction_stores_a_normalized_event(session: Session, settings: Settings) -> None:
    story = _story(session, event_pending=True)
    raw = event_json(countries=["USA", "India", "EU"], channels=["tariffs_trade", "none"])
    fake = FakeProvider(responses=[provider_response(raw)])

    result = extract_events(session, [story], _llm(settings, fake), settings, NOW)

    assert result.extracted == [story.id] and not story.event_pending
    event = story.latest_event
    assert event is not None
    assert event.countries == ["United States", "India", "EU"]
    assert event.channels == ["tariffs_trade"]
    assert event.regions == ["US", "India"]  # copied from the story
    assert (event.model, event.prompt_version) == (settings.llm.summary_model, "event-v3")
    assert event.temperature == settings.llm.temperature_for(settings.llm.summary_model)
    assert event.seed == settings.llm.seed
    assert event.created_at == NOW
    assert [note.split(": ", 1)[1].split(" ")[0] for note in result.notes] == ["model", "unmapped"]
    # The prompt carries the summary and the (escaped) articles it was written from.
    user = fake.calls[0]["user"]
    assert "It allows tariffs on India." in user and "100% tariffs &lt;b&gt;on&lt;/b&gt;" in user


def test_re_extraction_adds_a_row_and_the_latest_is_current(
    session: Session, settings: Settings
) -> None:
    story = _story(session)
    responses = [
        provider_response(event_json(severity="escalation")),
        provider_response(event_json(severity="de_escalation")),
    ]
    llm = _llm(settings, FakeProvider(responses=responses))
    extract_events(session, [story], llm, settings, NOW)
    extract_events(session, [story], llm, settings, NOW + timedelta(hours=3))
    assert [event.severity for event in story.events] == ["escalation", "de_escalation"]
    assert story.latest_event is not None and story.latest_event.severity == "de_escalation"


def test_invalid_output_keeps_the_summary_and_is_not_retried_every_run(
    session: Session, settings: Settings
) -> None:
    story = _story(session, event_pending=True)
    bad = provider_response('{"event_type": "war"}')
    result = extract_events(
        session, [story], _llm(settings, FakeProvider(responses=[bad, bad])), settings, NOW
    )
    assert result.failed and result.failed[0][0] == story.id
    assert story.events == [] and not story.event_pending
    assert story.status == "summarized" and story.summary is not None


def test_api_error_leaves_the_story_pending_and_continues(
    session: Session, settings: Settings
) -> None:
    broken, fine = _story(session), _story(session)
    fake = FakeProvider(
        responses=[
            ProviderError("HTTP 400: bad request", transient=False, status=400),
            provider_response(event_json()),
        ]
    )
    result = extract_events(session, [broken, fine], _llm(settings, fake), settings, NOW)
    assert result.call_errors[0][0] == broken.id and broken.event_pending
    assert result.extracted == [fine.id]


def test_quota_stops_the_step_and_everything_left_is_pending(
    session: Session, settings: Settings
) -> None:
    settings.llm.max_retries = 1
    first, second = _story(session), _story(session)
    fake = FakeProvider(responses=[ProviderError("HTTP 429", transient=True, status=429)] * 2)
    result = extract_events(session, [first, second], _llm(settings, fake), settings, NOW)
    assert result.stopped and result.skipped_quota == [first.id, second.id]
    assert first.event_pending and second.event_pending
    assert session.scalars(select(Event)).all() == []


def test_pending_stories_need_a_summary_and_a_recent_article(
    session: Session, settings: Settings
) -> None:
    wanted = _story(session, event_pending=True)
    _story(session, event_pending=False)
    _story(session, event_pending=True, status="failed")
    _story(session, event_pending=True, hours_ago=settings.pipeline.lookback_hours + 1)
    assert pending_event_stories(session, settings, NOW) == [wanted]
