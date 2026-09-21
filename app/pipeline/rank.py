"""Score stories by importance (SPEC 7.4) and pick the top ones for this run."""

import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Article, Story
from app.pipeline.dedupe import count_independent_sources
from app.pipeline.sections import only_from, regional_candidates

if TYPE_CHECKING:  # a type hint only: rank.py must not import the LLM client
    from app.llm.client import LLMClient


@dataclass(frozen=True)
class Importance:
    score: float
    independent_sources: int
    region_diversity: int
    mean_source_weight: float
    hours_since_latest: float


def score_articles(articles: Sequence[Article], settings: Settings, now: datetime) -> Importance:
    """importance = w_sources * log(1 + independent sources)
    + w_region_diversity * distinct regions + w_source_weight * mean(source_weight)
    + w_recency * 0.5 ** (hours since latest article / half-life)
    """
    weights = settings.ranking
    # Explainers and roundups add nothing to a story's importance.
    articles = [article for article in articles if not article.non_news] or list(articles)
    sources = count_independent_sources(articles, settings.dedupe.syndication_title_similarity)
    regions = len({article.source_region for article in articles})
    mean_weight = sum(article.source_weight for article in articles) / len(articles)
    latest = max(article.published_at for article in articles)
    hours = max((now - latest).total_seconds() / 3600, 0.0)
    score = (
        weights.w_sources * math.log(1 + sources)
        + weights.w_region_diversity * regions
        + weights.w_source_weight * mean_weight
        + weights.w_recency * 0.5 ** (hours / weights.recency_half_life_hours)
    )
    return Importance(score, sources, regions, mean_weight, hours)


def rank_stories(
    session: Session, settings: Settings, now: datetime, limit: int | None = None
) -> list[Story]:
    """Rescore every story with an article inside the lookback window, store the scores, and
    return the top `limit` (by default `max_stories_per_run`), most important first."""
    cutoff = now - timedelta(hours=settings.pipeline.lookback_hours)
    recent_story_ids = select(Article.story_id).where(
        Article.story_id.is_not(None), Article.published_at >= cutoff
    )
    articles = session.scalars(select(Article).where(Article.story_id.in_(recent_story_ids))).all()

    by_story: dict[int, list[Article]] = defaultdict(list)
    for article in articles:
        if article.story_id is not None:
            by_story[article.story_id].append(article)

    ranked: list[tuple[float, datetime, Story]] = []
    for story_id, story_articles in by_story.items():
        story = session.get(Story, story_id)
        if story is None:
            continue
        importance = score_articles(story_articles, settings, now)
        story.importance_score = importance.score
        story.source_count = importance.independent_sources
        story.region_diversity = importance.region_diversity
        latest = max(article.published_at for article in story_articles)
        ranked.append((importance.score, latest, story))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    top = limit if limit is not None else settings.pipeline.max_stories_per_run
    return [story for _, _, story in ranked[:top]]


def pending_stories(session: Session, settings: Settings, now: datetime) -> list[Story]:
    """Stories whose summary was skipped for quota on an earlier run and that still have an
    article inside the lookback window, most important first."""
    cutoff = now - timedelta(hours=settings.pipeline.lookback_hours)
    recent_story_ids = select(Article.story_id).where(
        Article.story_id.is_not(None), Article.published_at >= cutoff
    )
    return list(
        session.scalars(
            select(Story)
            .where(Story.summary_pending.is_(True), Story.id.in_(recent_story_ids))
            .order_by(Story.importance_score.desc())
        )
    )


# What a run records when the rerank failed and the computed order was kept. `newsdesk
# health` counts these, so the text is a constant rather than a string written twice.
RERANK_FALLBACK_NOTE = "rerank skipped, keeping the importance order"

# One retry, not `llm.max_retries`. The reasoning model 503s often and has a 20-request day,
# so retrying it costs quota the summaries need, and the fallback (the computed importance
# order) is good enough that a missed rerank barely shows. Seen 2026-09-20: 18 requests
# against a budget of 15, all of them retries of 503s.
RERANK_MAX_RETRIES = 1


def rerank_stories(
    llm: "LLMClient", stories: Sequence[Story], settings: Settings, limit: int
) -> tuple[list[Story], str | None]:
    """SPEC 7.4 (Phase 5): let the reasoning model reorder the candidates by real-world
    significance. Returns the new order and a note if anything went wrong; on any failure the
    importance order is kept, so a bad or missing rerank can never cost a run."""
    from app.llm.client import LLMError
    from app.llm.prompts import RERANK_SYSTEM, rerank_user_prompt
    from app.llm.schemas import StoryRanking

    candidates = list(stories)[:limit]
    if len(candidates) < 2:
        return list(stories), None
    rest = list(stories)[limit:]
    try:
        output = llm.structured(
            model=settings.llm.reasoning_model,
            system=RERANK_SYSTEM,
            user=rerank_user_prompt(
                [
                    (story.id, story.headline, story.source_count, story.regions or [])
                    for story in candidates
                ]
            ),
            schema=StoryRanking,
            max_tokens=2048,
            purpose=f"rerank {len(candidates)} stories",
            max_retries=RERANK_MAX_RETRIES,
        )
    except LLMError as exc:
        return list(stories), f"{RERANK_FALLBACK_NOTE}: {exc}"

    by_id = {story.id: story for story in candidates}
    ordered: list[Story] = []
    unknown = 0
    for story_id in output.value.story_ids:
        story = by_id.pop(story_id, None)
        if story is None:
            unknown += 1
            continue
        ordered.append(story)
    missing = [story for story in candidates if story.id in by_id]
    ordered.extend(missing)  # anything the model left out keeps its importance order
    note = None
    if unknown or missing:
        note = (
            f"rerank returned {unknown} unknown id(s) and left out {len(missing)} story(ies); "
            "those kept their importance order"
        )
    return ordered + rest, note


def reserved_pool(
    session: Session, settings: Settings, now: datetime, exclude: Sequence[Story] = ()
) -> list[Story]:
    """Stories from a single region's outlets, to put in front of the rerank.

    They never reach the top 40 on importance alone, so without this the model never sees
    them and the reserved slots would be filled from the computed order only.
    """
    pool = settings.pipeline.reserved_candidate_pool
    if not settings.pipeline.reserved_slots or not pool:
        return []
    cutoff = now - timedelta(hours=settings.pipeline.lookback_hours)
    recent = select(Article.story_id).where(
        Article.story_id.is_not(None), Article.published_at >= cutoff
    )
    stories = session.scalars(select(Story).where(Story.id.in_(recent))).all()
    seen = {story.id for story in exclude}
    picked: list[Story] = []
    for region in settings.pipeline.reserved_slots:
        candidates = regional_candidates(
            stories, region, pool, now, settings.pipeline.reserved_max_age_hours
        )
        for story in candidates:
            if story.id not in seen:
                seen.add(story.id)
                picked.append(story)
    return picked


def select_with_reserved(
    ordered: Sequence[Story],
    settings: Settings,
    eligible: Callable[[Story], bool] | None = None,
) -> list[Story]:
    """Pick this run's stories from `ordered`, keeping `reserved_slots` for single-region ones.

    The reserved picks are taken in the order given (the rerank's, or importance when it
    failed), and only from stories `eligible` accepts: that is what keeps a slot away from
    an Asian Games final. Slots nobody qualifies for go back to the general list, and the
    result keeps `ordered`'s order, so the digest still reads most important first.
    """
    total = settings.pipeline.max_stories_per_run
    chosen: set[int] = set()
    for region, count in settings.pipeline.reserved_slots.items():
        for story in ordered:
            if len(chosen) >= total or count <= 0:
                break
            if story.id in chosen or not only_from(story, region):
                continue
            if eligible is not None and not eligible(story):
                continue
            chosen.add(story.id)
            count -= 1
    for story in ordered:
        if len(chosen) >= total:
            break
        chosen.add(story.id)
    return [story for story in ordered if story.id in chosen][:total]
