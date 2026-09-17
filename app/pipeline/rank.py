"""Score stories by importance (SPEC 7.4) and pick the top ones for this run."""

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Article, Story
from app.pipeline.dedupe import count_independent_sources


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


def rank_stories(session: Session, settings: Settings, now: datetime) -> list[Story]:
    """Rescore every story with an article inside the lookback window, store the scores,
    and return the top `max_stories_per_run`, most important first."""
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
    return [story for _, _, story in ranked[: settings.pipeline.max_stories_per_run]]


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
