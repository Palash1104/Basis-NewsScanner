"""Summarize the top stories (SPEC 7.5), skipping stories that haven't materially changed."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import Settings
from app.llm.client import (
    LLMCallError,
    LLMClient,
    LLMConfigError,
    LLMOutputError,
    LLMQuotaError,
)
from app.llm.prompts import SUMMARY_PROMPT_VERSION, SUMMARY_SYSTEM, summary_user_prompt
from app.llm.schemas import StorySummary
from app.models import Article, Story
from app.pipeline.dedupe import normalize_source

log = logging.getLogger(__name__)

# Gemini counts thinking tokens against the output cap, so leave room beyond the JSON itself.
SUMMARY_MAX_TOKENS = 2048
MIN_NEW_ARTICLES = 2
# Set by regrouping when a summarized story's article set changed: its summary may be wrong, so
# it's kept out of digests until it gains a new article and is summarized again.
STALE_STATUS = "needs_resummary"


@dataclass
class SummarizeResult:
    summarized: list[int] = field(default_factory=list)
    skipped_unchanged: int = 0
    failed: list[tuple[int, str]] = field(default_factory=list)  # status set to "failed"
    call_errors: list[tuple[int, str]] = field(default_factory=list)  # status unchanged
    # Set when a quota ran out or config is missing; remaining stories were left for later.
    stopped: str | None = None
    # Stories that still need a summary but were skipped because of `stopped`. They are marked
    # summary_pending so the next run does them first.
    skipped_quota: list[int] = field(default_factory=list)


def news_articles(story: Story) -> list[Article]:
    """The story's news articles: explainers and roundups aren't summarized or counted."""
    articles = list(story.articles)
    return [article for article in articles if not article.non_news] or articles


def source_regions(articles: Sequence[Article]) -> list[str]:
    return sorted({article.source_region for article in articles})


def resummarize_reason(story: Story, articles: Sequence[Article]) -> str | None:
    """Why the story needs a (new) summary, or None if it hasn't materially changed."""
    if story.status == "new":
        return "new story"
    added = len(articles) - story.processed_article_count
    if story.status == STALE_STATUS:
        return f"stale after regrouping, {added} new article(s)" if added > 0 else None
    if added >= MIN_NEW_ARTICLES:
        return f"{added} new articles"
    new_regions = set(source_regions(articles)) - set(story.processed_source_regions or [])
    if new_regions:
        return f"new source region {', '.join(sorted(new_regions))}"
    return None


def select_articles(articles: Sequence[Article], limit: int) -> list[Article]:
    """Up to `limit` articles: first one per region, then one per outlet, then the rest.
    Within each pass, prominent outlets and newer articles come first."""
    ordered = sorted(articles, key=lambda a: (-a.source_weight, -a.published_at.timestamp()))
    chosen: list[Article] = []
    used_regions: set[str] = set()
    used_sources: set[str] = set()

    def take(article: Article) -> None:
        chosen.append(article)
        used_regions.add(article.source_region)
        used_sources.add(normalize_source(article.source_name))

    for article in ordered:
        source = normalize_source(article.source_name)
        if article.source_region not in used_regions and source not in used_sources:
            take(article)
    for article in ordered:
        if article not in chosen and normalize_source(article.source_name) not in used_sources:
            take(article)
    for article in ordered:
        if article not in chosen:
            take(article)
    return chosen[:limit]


def summarize_stories(
    session: Session,
    stories: Sequence[Story],
    llm: LLMClient,
    settings: Settings,
    now: datetime,
) -> SummarizeResult:
    """Summarize each story that needs it. Commits after every story, so progress survives a
    later failure. One failing story never stops the others. A used-up quota stops the loop:
    that story and every later one that still needs a summary are marked summary_pending."""
    result = SummarizeResult()
    model = settings.llm.summary_model
    for index, story in enumerate(stories):
        articles = news_articles(story)
        reason = resummarize_reason(story, articles)
        if reason is None:
            result.skipped_unchanged += 1
            story.summary_pending = False
            continue

        chosen = select_articles(articles, settings.pipeline.max_articles_per_story_for_llm)
        carried = ", carried over from an earlier run" if story.summary_pending else ""
        log.info(
            "summarizing story %d (%s%s) from %d articles", story.id, reason, carried, len(chosen)
        )
        try:
            output = llm.structured(
                model=model,
                system=SUMMARY_SYSTEM,
                user=summary_user_prompt(chosen),
                schema=StorySummary,
                max_tokens=SUMMARY_MAX_TOKENS,
                purpose=f"summarize story {story.id}",
            )
        except (LLMQuotaError, LLMConfigError) as exc:
            result.stopped = str(exc)
            for waiting in stories[index:]:
                if resummarize_reason(waiting, news_articles(waiting)) is not None:
                    waiting.summary_pending = True
                    result.skipped_quota.append(waiting.id)
            session.commit()
            log.warning(
                "stopping summaries for this run (%d stories left for the next run): %s",
                len(result.skipped_quota),
                exc,
            )
            break
        except LLMCallError as exc:
            log.warning("story %d: %s", story.id, exc)
            result.call_errors.append((story.id, str(exc)))
            continue
        except LLMOutputError as exc:
            log.warning("story %d marked failed: %s", story.id, exc)
            story.status = "failed"
            story.summary_pending = False
            _record_processed(story, articles, model)
            session.commit()
            result.failed.append((story.id, str(exc)))
            continue

        summary = output.value
        story.headline = summary.headline
        story.summary = summary.summary
        story.category = summary.category
        story.regions = list(summary.regions)
        story.sources_disagree = summary.sources_disagree
        story.disagreement_note = summary.disagreement_note
        story.status = "summarized"
        story.summary_pending = False
        story.updated_at = now
        _record_processed(story, articles, model)
        session.commit()
        result.summarized.append(story.id)
    return result


def _record_processed(story: Story, articles: Sequence[Article], model: str) -> None:
    story.processed_article_count = len(articles)
    story.processed_source_regions = source_regions(articles)
    story.prompt_version = SUMMARY_PROMPT_VERSION
    story.model = model
