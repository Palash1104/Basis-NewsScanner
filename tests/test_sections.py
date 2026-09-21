from datetime import UTC, datetime, timedelta

from app.models import Article, Story
from app.pipeline.sections import (
    fresh_enough,
    is_soft_section,
    may_take_reserved_slot,
    only_from,
    regional_candidates,
    url_sections,
)

NOW = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)

# Real URLs from the six Indian feeds, warts and all.
HARD = [
    "https://economictimes.indiatimes.com/nri/latest-updates/mea-amends-passports-rules/articleshow/1.cms",
    "https://www.thehindu.com/news/national/kerala/think-gas-urges-state-agencies/article714.ece",
    "https://www.livemint.com/market/stock-market-news/top-stocks-in-focus-11789.html",
    "https://indianexpress.com/article/business/gst-on-upi-mdr-transactions-10882672",
    "https://www.hindustantimes.com/india-news/assam-tribunal-member-arrested-101789.html",
    # A business story that lives under a fashion segment, three deep.
    "https://economictimes.indiatimes.com/industry/cons-products/fashion-/-cosmetics/titan-q2/articleshow/9.cms",
]
SOFT = [
    "https://economictimes.indiatimes.com/news/sports/cricket/india-mens-team/articleshow/1.cms",
    "https://economictimes.indiatimes.com/news/sports/other-sports/asian-games-shooting/articleshow/2.cms",
    "https://www.thehindu.com/entertainment/art/unbound-art-exhibition/article1.ece",
    "https://www.livemint.com/news/trends/mumbai-mans-video-goes-viral-11789.html",
    "https://www.hindustantimes.com/trending/exhusband-goes-on-trial-101789.html",
    "https://indianexpress.com/article/sports/cricket/asia-cup-final-10882672",
]
OPAQUE = "https://news.google.com/rss/articles/CBMizAFBVV95cUxONkVlTlRqM1FD"


def _story(*urls: str, region: str = "IN", non_news: bool = False) -> Story:
    story = Story(first_seen_at=NOW, updated_at=NOW, headline="h", importance_score=3.0)
    story.articles = [
        Article(
            url=url,
            title=f"t{index}",
            source_name="Outlet",
            source_region=region,
            source_weight=2,
            published_at=NOW,
            fetched_at=NOW,
            non_news=non_news,
        )
        for index, url in enumerate(urls)
    ]
    return story


# ---------------------------------------------------------------- reading sections


def test_hard_news_sections_are_not_soft() -> None:
    for url in HARD:
        assert not is_soft_section(url), url


def test_sport_entertainment_and_filler_sections_are_soft() -> None:
    for url in SOFT:
        assert is_soft_section(url), url


def test_a_slug_is_never_mistaken_for_a_section() -> None:
    """ "sport" is inside passport, "ipl" inside diplomats, "celeb" inside celebrate."""
    for url in [
        "https://economictimes.indiatimes.com/nri/latest-updates/passports-rules-change/articleshow/1.cms",
        "https://www.thehindu.com/news/international/china-us-top-diplomats-discuss/article1.ece",
        "https://www.livemint.com/news/india/bjp-leaders-celebrate-win-11789.html",
        "https://www.hindustantimes.com/india-news/footballer-identity-papers-101789.html",
    ]:
        assert url_sections(url) and not is_soft_section(url), url


def test_a_google_news_redirect_reads_as_unknown() -> None:
    assert url_sections(OPAQUE) == []
    assert not is_soft_section(OPAQUE)


# ---------------------------------------------------------------- eligibility


def test_a_hard_news_story_may_take_a_reserved_slot() -> None:
    assert may_take_reserved_slot(_story(HARD[0]))


def test_a_sports_story_may_not() -> None:
    assert not may_take_reserved_slot(_story(SOFT[0], SOFT[1]))


def test_one_hard_source_is_enough() -> None:
    """Outlets file the same story under different sections; one hard section decides."""
    assert may_take_reserved_slot(_story(SOFT[0], HARD[1]))


def test_an_opaque_only_story_may_not_take_a_slot() -> None:
    """Unknown counts as no: hundreds of candidates compete for five slots, so waiting for a
    run where the rerank works is cheaper than risking a teqball final."""
    assert not may_take_reserved_slot(_story(OPAQUE))


def test_an_opaque_story_with_one_readable_source_may() -> None:
    assert may_take_reserved_slot(_story(OPAQUE, HARD[2]))


def test_non_news_articles_do_not_decide_eligibility() -> None:
    story = _story(HARD[0])
    story.articles.append(_story(SOFT[0], non_news=True).articles[0])
    assert may_take_reserved_slot(story)


# ---------------------------------------------------------------- regions


def test_only_from_reads_the_feeds_not_the_summary() -> None:
    indian = _story(HARD[0], region="IN")
    indian.regions = ["US"]  # what the summary said the story is about
    assert only_from(indian, "IN")
    assert not only_from(_story(HARD[0], region="US"), "IN")


def test_a_story_with_a_global_source_is_not_single_region() -> None:
    story = _story(HARD[0], region="IN")
    story.articles.append(_story(HARD[1], region="GLOBAL").articles[0])
    assert not only_from(story, "IN")


def test_regional_candidates_are_the_most_important_first() -> None:
    low = _story(HARD[0])
    low.importance_score = 1.0
    high = _story(HARD[1])
    high.importance_score = 5.0
    other = _story(HARD[2], region="US")
    picked = regional_candidates([low, high, other], "IN", limit=5)
    assert [story.importance_score for story in picked] == [5.0, 1.0]


def test_regional_candidates_respect_the_pool_size() -> None:
    stories = []
    for index in range(8):
        story = _story(HARD[index % len(HARD)])
        story.importance_score = float(index)
        stories.append(story)
    assert len(regional_candidates(stories, "IN", limit=3)) == 3


def test_recent_articles_are_what_regions_are_read_from() -> None:
    story = _story(HARD[0])
    story.articles[0].published_at = NOW - timedelta(days=2)
    assert only_from(story, "IN")


# ---------------------------------------------------------------- freshness


def test_a_recent_story_is_fresh_enough() -> None:
    story = _story(HARD[0])
    story.first_seen_at = NOW - timedelta(hours=23)
    assert fresh_enough(story, NOW, 24)


def test_a_story_older_than_the_cap_is_not() -> None:
    """Hundreds of Indian stories wait unsummarized; without a cap the reserve would work
    through the backlog instead of covering what is happening now."""
    story = _story(HARD[0])
    story.first_seen_at = NOW - timedelta(hours=25)
    assert not fresh_enough(story, NOW, 24)


def test_no_cap_means_no_age_limit() -> None:
    story = _story(HARD[0])
    story.first_seen_at = NOW - timedelta(days=30)
    assert fresh_enough(story, NOW, None)


def test_the_candidate_pool_drops_stale_stories() -> None:
    fresh = _story(HARD[0])
    fresh.first_seen_at = NOW - timedelta(hours=6)
    fresh.importance_score = 2.0
    stale = _story(HARD[1])
    stale.first_seen_at = NOW - timedelta(hours=40)
    stale.importance_score = 9.0  # more important, and still not eligible

    picked = regional_candidates([stale, fresh], "IN", limit=5, now=NOW, max_age_hours=24)

    assert picked == [fresh]
    # Without a cap the older, higher-scoring story wins.
    assert regional_candidates([stale, fresh], "IN", limit=5)[0] is stale
