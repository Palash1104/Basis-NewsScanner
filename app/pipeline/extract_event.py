"""Event extraction (SPEC 7.6): describe each freshly summarized story in fixed categories
(event type, countries, channels, severity, ...) that the playbook can match.

Extraction follows the summary. A story is extracted right after each successful (re)summary,
from the same articles, so it's re-extracted only when its article set materially changed
(the summary rule). Summary and extraction are separate calls: a failed extraction never
touches the summary, and the event prompt can change without rewriting summaries.

Each extraction adds an `events` row; the latest is the story's current event. Failures:
- quota used up: the rest of the step stops; skipped stories get `event_pending` and are
  extracted first on the next run (like summaries);
- API or network error: `event_pending`, retried next run;
- output still invalid after the validation retry: no event, retried only after the story's
  next re-summary (so a bad input doesn't burn quota every run).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.llm.client import (
    LLMCallError,
    LLMClient,
    LLMConfigError,
    LLMOutputError,
    LLMQuotaError,
    StructuredResult,
)
from app.llm.prompts import EVENT_PROMPT_VERSION, EVENT_SYSTEM, event_user_prompt
from app.llm.schemas import EventExtraction
from app.models import Article, Event, Story
from app.pipeline.countries import normalize_countries
from app.pipeline.summarize import news_articles, select_articles

log = logging.getLogger(__name__)

# Gemini counts thinking tokens against the output cap, so leave room beyond the JSON itself.
EVENT_MAX_TOKENS = 2048
SUMMARIZED_STATUSES = ("summarized", "analyzed")


@dataclass
class ExtractResult:
    extracted: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)  # invalid output: no event
    call_errors: list[tuple[int, str]] = field(default_factory=list)  # left event_pending
    stopped: str | None = None  # a quota ran out or config is missing
    skipped_quota: list[int] = field(default_factory=list)  # left event_pending by `stopped`
    # Things worth a look, shown in the run output: unmapped country names, "none" returned
    # alongside real channels.
    notes: list[str] = field(default_factory=list)


def normalize_event(extraction: EventExtraction) -> tuple[EventExtraction, list[str]]:
    """Canonical country names and a consistent channel list, plus notes on what was fixed."""
    notes = []
    channels = list(extraction.channels)
    if "none" in channels and len(channels) > 1:
        others = [channel for channel in channels if channel != "none"]
        notes.append(f"model returned 'none' together with {', '.join(others)}; dropped 'none'")
        channels = others
    if not channels:
        notes.append("model returned no channels; using none")
        channels = ["none"]
    countries, unmapped = normalize_countries(extraction.countries)
    if unmapped:
        notes.append(f"unmapped country name(s), kept as written: {', '.join(unmapped)}")
    return extraction.model_copy(update={"channels": channels, "countries": countries}), notes


def event_articles(story: Story, settings: Settings) -> list[Article]:
    """The articles the summary is written from (same selection as summarize)."""
    return select_articles(news_articles(story), settings.pipeline.max_articles_per_story_for_llm)


def request_event(
    llm: LLMClient, story: Story, articles: Sequence[Article], model: str
) -> StructuredResult[EventExtraction]:
    """One extraction call for a summarized story. Raises the LLMClient errors."""
    assert story.summary is not None
    return llm.structured(
        model=model,
        system=EVENT_SYSTEM,
        user=event_user_prompt(story.headline, story.summary, articles),
        schema=EventExtraction,
        max_tokens=EVENT_MAX_TOKENS,
        purpose=f"extract event story {story.id}",
    )


def extract_events(
    session: Session,
    stories: Sequence[Story],
    llm: LLMClient,
    settings: Settings,
    now: datetime,
) -> ExtractResult:
    """Extract an event for each story (all must have a summary). Commits after every story;
    one failing story never stops the others, but a used-up quota stops the step."""
    result = ExtractResult()
    model = settings.llm.summary_model
    provenance = settings.llm.provenance(model, EVENT_PROMPT_VERSION)
    for index, story in enumerate(stories):
        carried = ", carried over from an earlier run" if story.event_pending else ""
        articles = event_articles(story, settings)
        log.info("extracting event for story %d%s", story.id, carried)
        try:
            output = request_event(llm, story, articles, model)
        except (LLMQuotaError, LLMConfigError) as exc:
            result.stopped = str(exc)
            for waiting in stories[index:]:
                waiting.event_pending = True
                result.skipped_quota.append(waiting.id)
            session.commit()
            log.warning(
                "stopping event extraction for this run (%d stories left for the next run): %s",
                len(result.skipped_quota),
                exc,
            )
            break
        except LLMCallError as exc:
            log.warning("story %d: event extraction failed, retrying next run: %s", story.id, exc)
            story.event_pending = True
            session.commit()
            result.call_errors.append((story.id, str(exc)))
            continue
        except LLMOutputError as exc:
            log.warning("story %d: no event (invalid output): %s", story.id, exc)
            story.event_pending = False
            session.commit()
            result.failed.append((story.id, str(exc)))
            continue

        event, notes = normalize_event(output.value)
        for note in notes:
            log.warning("story %d event: %s", story.id, note)
            result.notes.append(f"story {story.id}: {note}")
        session.add(
            Event(
                story=story,
                event_type=event.event_type,
                countries=event.countries,
                regions=list(story.regions or []),
                entities=event.entities,
                companies=event.companies,
                channels=list(event.channels),
                severity=event.severity,
                policy_stance=event.policy_stance,
                policy_actor=event.policy_actor,
                is_new_development=event.is_new_development,
                model=output.model,
                prompt_version=provenance.prompt_version,
                temperature=provenance.temperature,
                seed=provenance.seed,
                created_at=now,
            )
        )
        story.event_pending = False
        session.commit()
        result.extracted.append(story.id)
    return result


def pending_event_stories(session: Session, settings: Settings, now: datetime) -> list[Story]:
    """Summarized stories whose extraction was skipped on an earlier run and that still have an
    article inside the lookback window, most important first."""
    cutoff = now - timedelta(hours=settings.pipeline.lookback_hours)
    recent_story_ids = select(Article.story_id).where(
        Article.story_id.is_not(None), Article.published_at >= cutoff
    )
    return list(
        session.scalars(
            select(Story)
            .where(
                Story.event_pending.is_(True),
                Story.status.in_(SUMMARIZED_STATUSES),
                Story.id.in_(recent_story_ids),
            )
            .order_by(Story.importance_score.desc())
        )
    )
