import math
from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import Settings
from app.llm.client import LLMClient, ProviderError
from app.models import Article, Story
from app.pipeline.rank import (
    RERANK_FALLBACK_NOTE,
    rank_stories,
    rerank_stories,
    reserved_pool,
    score_articles,
    select_with_reserved,
)
from app.pipeline.sections import may_take_reserved_slot, only_from
from tests.conftest import NOW
from tests.fakes import FakeProvider


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


# ---------------------------------------------------------------- rerank


def _story(
    session: Session,
    headline: str,
    region: str = "US",
    url: str | None = None,
    score: float = 0.0,
) -> Story:
    story = Story(
        first_seen_at=NOW,
        updated_at=NOW,
        headline=headline,
        source_count=2,
        regions=["US"],
        importance_score=score,
    )
    session.add(story)
    session.flush()
    story.articles = [
        Article(
            url=url or f"https://example.com/news/world/{story.id}",
            source_name="Outlet",
            source_region=region,
            source_weight=2,
            title=headline,
            snippet="",
            published_at=NOW,
            fetched_at=NOW,
            story_id=story.id,
        )
    ]
    session.flush()
    return story


def test_a_503_rerank_is_tried_twice_then_falls_back(session: Session, settings: Settings) -> None:
    """The reasoning model has a 20-request day, so a failing rerank must not spend it: one
    retry, then the computed importance order (which is always good enough)."""
    stories = [_story(session, "first"), _story(session, "second")]
    fake = FakeProvider(
        responder=lambda _: ProviderError("503 unavailable", transient=True, status=503)
    )
    llm = LLMClient(settings.llm, fake, sleep=lambda seconds: None)

    ordered, note = rerank_stories(llm, stories, settings, limit=10)

    assert len(fake.calls) == 2  # the attempt and one retry, not llm.max_retries
    assert ordered == stories  # unchanged: the importance order stands
    assert note is not None and note.startswith(RERANK_FALLBACK_NOTE)


# ---------------------------------------------------------------- reserved slots


def _ranked(session: Session, settings: Settings) -> list[Story]:
    """Twenty-five international stories, then ten Indian ones: the real shape of a run,
    where India-only stories sit far below the cutoff."""
    stories = []
    for index in range(25):
        story = _story(
            session,
            f"World story {index}",
            region="GLOBAL",
            url=f"https://www.reuters.com/world/story-{index}",
            score=9.0 - index * 0.1,
        )
        stories.append(story)
    for index in range(10):
        story = _story(
            session,
            f"India story {index}",
            region="IN",
            url=f"https://www.livemint.com/market/stock-market-news/story-{index}-117.html",
            score=3.5 - index * 0.1,
        )
        stories.append(story)
    session.flush()
    return stories


def test_reserved_slots_go_to_single_region_stories(session: Session, settings: Settings) -> None:
    settings.pipeline.max_stories_per_run = 20
    settings.pipeline.reserved_slots = {"IN": 5}
    picked = select_with_reserved(_ranked(session, settings), settings)

    assert len(picked) == 20
    indian = [story for story in picked if only_from(story, "IN")]
    assert len(indian) == 5
    # The five best Indian ones, and the fifteen best of the rest.
    assert [story.headline for story in indian] == [f"India story {index}" for index in range(5)]
    assert sum(1 for story in picked if only_from(story, "GLOBAL")) == 15


def test_the_order_stays_the_order_it_was_given(session: Session, settings: Settings) -> None:
    """Reserved or not, the digest still reads most important first."""
    settings.pipeline.max_stories_per_run = 20
    settings.pipeline.reserved_slots = {"IN": 5}
    ordered = _ranked(session, settings)
    picked = select_with_reserved(ordered, settings)
    assert picked == [story for story in ordered if story in picked]


def test_a_reserved_slot_never_goes_to_sport(session: Session, settings: Settings) -> None:
    """The guard that matters when the rerank has failed and importance order is all we have."""
    settings.pipeline.max_stories_per_run = 8
    settings.pipeline.reserved_slots = {"IN": 2}
    sport = _story(
        session,
        "Asian Games: India's shooters take aim",
        region="IN",
        url="https://economictimes.indiatimes.com/news/sports/other-sports/asian-games/articleshow/1.cms",
        score=4.0,
    )
    business = _story(
        session,
        "India's software exports rise 8.2%",
        region="IN",
        url="https://www.livemint.com/market/stock-market-news/exports-117.html",
        score=3.0,
    )
    world = [
        _story(
            session,
            f"World {index}",
            region="GLOBAL",
            url=f"https://www.reuters.com/world/w{index}",
            score=9.0 - index * 0.1,  # all of them above both Indian stories
        )
        for index in range(8)
    ]
    session.flush()

    ordered = sorted([sport, business, *world], key=lambda s: -s.importance_score)
    picked = select_with_reserved(ordered, settings, eligible=may_take_reserved_slot)

    # business is below the cutoff and only gets in through the reserve; sport outranks it
    # and still does not, because a reserved slot is not for sport. The freed slot goes
    # back to the general list.
    assert business in picked
    assert sport not in picked
    assert sum(1 for story in picked if only_from(story, "GLOBAL")) == 7


def test_unused_reserved_slots_go_back_to_the_general_list(
    session: Session, settings: Settings
) -> None:
    settings.pipeline.max_stories_per_run = 6
    settings.pipeline.reserved_slots = {"IN": 5}
    world = [
        _story(
            session,
            f"World {index}",
            region="GLOBAL",
            url=f"https://www.reuters.com/world/w{index}",
            score=9.0 - index,
        )
        for index in range(9)
    ]
    india = _story(
        session,
        "One Indian story",
        region="IN",
        url="https://www.livemint.com/market/stock-market-news/only-117.html",
        score=1.0,
    )
    session.flush()
    picked = select_with_reserved(
        sorted([*world, india], key=lambda s: -s.importance_score), settings
    )

    assert len(picked) == 6  # not 1 + 5 empty
    assert india in picked
    assert sum(1 for story in picked if only_from(story, "GLOBAL")) == 5


def test_no_reserved_slots_configured_changes_nothing(session: Session, settings: Settings) -> None:
    settings.pipeline.max_stories_per_run = 20
    settings.pipeline.reserved_slots = {}
    ordered = _ranked(session, settings)
    assert select_with_reserved(ordered, settings) == ordered[:20]


def test_reserved_slots_cannot_exceed_the_run(session: Session, settings: Settings) -> None:
    settings.pipeline.max_stories_per_run = 3
    settings.pipeline.reserved_slots = {"IN": 5}
    picked = select_with_reserved(_ranked(session, settings), settings)
    assert len(picked) == 3


def test_the_candidate_pool_reaches_past_the_cutoff(session: Session, settings: Settings) -> None:
    """Indian stories rank ~50th, so the rerank would never see them without this."""
    settings.pipeline.reserved_slots = {"IN": 5}
    settings.pipeline.reserved_candidate_pool = 10
    stories = _ranked(session, settings)
    session.commit()
    top = stories[:25]  # what rank_stories would have returned

    pool = reserved_pool(session, settings, NOW, exclude=top)

    assert len(pool) == 10
    assert all(only_from(story, "IN") for story in pool)
    assert not set(pool) & set(top)
