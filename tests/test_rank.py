import math
from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Article, Story
from app.pipeline.rank import rank_stories, score_articles
from tests.conftest import NOW


def _article(
    title: str,
    source: str,
    region: str,
    weight: int,
    hours_ago: float,
    story: Story | None = None,
) -> Article:
    return Article(
        url=f"https://example.com/{source}/{title.replace(' ', '-')}/{hours_ago}",
        source_name=source,
        source_region=region,
        source_weight=weight,
        title=title,
        snippet="",
        published_at=NOW - timedelta(hours=hours_ago),
        fetched_at=NOW,
        story=story,
    )


def test_score_follows_spec_formula(settings: Settings) -> None:
    articles = [
        _article("Storm makes landfall on east coast", "Wire A", "US", 3, 14),
        _article("Storm makes landfall on east coast", "Paper B", "IN", 1, 13),  # syndicated
        _article("Thousands evacuated as cyclone hits", "Paper C", "GLOBAL", 2, 12),
    ]
    result = score_articles(articles, settings, NOW)
    w = settings.ranking
    expected = (
        w.w_sources * math.log(1 + 2)
        + w.w_region_diversity * 3
        + w.w_source_weight * 2.0
        + w.w_recency * 0.5 ** (12 / w.recency_half_life_hours)
    )
    assert result.independent_sources == 2
    assert result.region_diversity == 3
    assert result.mean_source_weight == 2.0
    assert math.isclose(result.hours_since_latest, 12)
    assert math.isclose(result.score, expected)


def test_more_independent_sources_rank_higher(settings: Settings) -> None:
    single = [_article("Local council meets", "Paper A", "IN", 2, 1)]
    covered = [
        _article("Fed raises rates", "Paper A", "US", 2, 1),
        _article("Federal Reserve lifts interest rates again", "Paper B", "US", 2, 1),
        _article("US central bank hikes for first time in three years", "Paper C", "US", 2, 1),
    ]
    assert (
        score_articles(covered, settings, NOW).score > score_articles(single, settings, NOW).score
    )


def test_rank_stories_scores_recent_stories_and_returns_top_n(
    session: Session, settings: Settings
) -> None:
    big = Story(first_seen_at=NOW, updated_at=NOW, headline="big")
    small = Story(first_seen_at=NOW, updated_at=NOW, headline="small")
    old = Story(first_seen_at=NOW - timedelta(days=3), updated_at=NOW, headline="old")
    session.add_all(
        [
            _article("EU invites Canada as associate member", "BBC", "GLOBAL", 3, 2, big),
            _article("Canada could become EU associate member", "The Hindu", "IN", 3, 1, big),
            _article("EU offers Canada associate status", "CNBC", "US", 2, 1, big),
            _article("Chess olympiad opens", "Livemint", "IN", 2, 3, small),
            _article("Old news", "BBC", "GLOBAL", 3, 72, old),
        ]
    )
    session.flush()
    settings.pipeline.max_stories_per_run = 1

    top = rank_stories(session, settings, NOW)

    assert top == [big]
    assert big.source_count == 3 and big.region_diversity == 3
    assert small.importance_score > 0
    assert old.importance_score == 0  # nothing inside the lookback window


def test_non_news_articles_add_nothing_to_importance(settings: Settings) -> None:
    news = [_article("Storm makes landfall on east coast", "Wire A", "US", 2, 1)]
    explainer = _article("What is a cyclone? Explained", "Paper B", "IN", 3, 0.5)
    explainer.non_news = True
    assert score_articles([*news, explainer], settings, NOW) == score_articles(news, settings, NOW)
