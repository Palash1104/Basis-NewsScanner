"""Regroup stored articles with the embedding matcher, without calling the LLM.

Used once when grouping switched from title matching to embeddings. New groups are matched back
to the old stories they overlap most, so story ids (and summaries) survive where possible:
- a story whose article set is unchanged keeps everything;
- a story whose set changed and had a summary becomes `needs_resummary`: it's kept out of
  digests, and is summarized again only after it gains a new article in a later run;
- an old story whose articles all moved elsewhere is deleted;
- a group with no old story becomes a new story (status "new").
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Article, Story
from app.pipeline.classify import is_non_news
from app.pipeline.cluster import group_embeddings
from app.pipeline.embed import Embedder, article_text
from app.pipeline.summarize import STALE_STATUS, source_regions

SUMMARIZED_STATES = frozenset({"summarized", "analyzed", "failed", STALE_STATUS})


@dataclass
class RegroupReport:
    articles: int = 0
    non_news: int = 0
    non_news_attached: int = 0
    non_news_left_out: int = 0
    stories_before: int = 0
    stories_after: int = 0
    unchanged: int = 0
    changed: int = 0
    created: int = 0
    deleted: int = 0
    stale: int = 0  # changed stories that had a summary: now needs_resummary
    stale_ids: list[int] | None = None


def regroup_articles(
    session: Session,
    articles: Sequence[Article],
    settings: Settings,
    embedder: Embedder,
    now: datetime,
) -> RegroupReport:
    """Regroup `articles` (and re-flag non-news) in place. The caller commits."""
    report = RegroupReport(articles=len(articles), stale_ids=[])
    report.stories_before = session.scalar(select(func.count(Story.id))) or 0
    articles = sorted(articles, key=lambda a: a.published_at)
    for article in articles:
        article.non_news = is_non_news(article.title)
    report.non_news = sum(article.non_news for article in articles)

    old_members: dict[int, set[int]] = {}
    for article in articles:
        if article.story_id is not None:
            old_members.setdefault(article.story_id, set()).add(article.id)

    decisions = group_embeddings(
        [article.published_at for article in articles],
        embedder.embed([article_text(a.title, a.snippet) for a in articles]),
        [article.non_news for article in articles],
        settings.grouping.embedding_threshold,
        timedelta(hours=settings.pipeline.story_attach_window_hours),
        settings.grouping.seed_threshold,
    )
    groups: dict[int, list[Article]] = {}
    for article, decision in zip(articles, decisions, strict=True):
        if decision.group is None:
            report.non_news_left_out += 1
            continue
        if article.non_news:
            report.non_news_attached += 1
        groups.setdefault(decision.group, []).append(article)

    # Give each group the old story it overlaps most, largest overlaps first.
    overlaps = sorted(
        (
            (count, group, story_id)
            for group, members in groups.items()
            for story_id, count in Counter(
                a.story_id for a in members if a.story_id is not None
            ).items()
        ),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    owner: dict[int, int] = {}  # group -> old story id
    claimed: set[int] = set()
    for _, group, story_id in overlaps:
        if group not in owner and story_id not in claimed:
            owner[group] = story_id
            claimed.add(story_id)

    for group, members in groups.items():
        news = [a for a in members if not a.non_news] or members
        member_ids = {a.id for a in members}
        story_id = owner.get(group)
        if story_id is None:
            story = Story(
                first_seen_at=min(a.published_at for a in news),
                updated_at=now,
                headline=news[0].title,
                status="new",
            )
            session.add(story)
            report.created += 1
        else:
            story = session.get(Story, story_id)
            assert story is not None
            if member_ids == old_members.get(story_id, set()):
                report.unchanged += 1
            else:
                report.changed += 1
                story.first_seen_at = min(a.published_at for a in news)
                if story.status in SUMMARIZED_STATES:
                    story.status = STALE_STATUS
                    story.summary_pending = False
                    # Baseline for "gained a new article" (see summarize.resummarize_reason).
                    story.processed_article_count = len(news)
                    story.processed_source_regions = source_regions(news)
                    report.stale += 1
                    report.stale_ids.append(story_id)  # type: ignore[union-attr]
        for article in members:
            article.story = story

    for article, decision in zip(articles, decisions, strict=True):
        if decision.group is None:
            article.story = None
    session.flush()

    for story_id in set(old_members) - claimed:
        story = session.get(Story, story_id)
        if story is not None:
            session.delete(story)
            report.deleted += 1
    session.flush()
    report.stories_after = session.scalar(select(func.count(Story.id))) or 0
    return report
