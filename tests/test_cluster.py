from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Article, Story
from app.pipeline.cluster import Grouper, assign_to_stories, make_text_key
from tests.conftest import NOW

WINDOW = timedelta(hours=36)

EU_CANADA = [
    "EU chief backs plan for Canada to become 'associate member'",
    "EU's von der Leyen wants Canada to become bloc's first associate member",
    "Canada invited to become EU's first 'associate member' as Trump trade war intensifies",
]
GAZA = "At least 20 killed as multi-storey building collapses in Gaza City"


def test_text_key_removes_stopwords_and_punctuation() -> None:
    key = make_text_key("The Fed's latest update: rates to rise", "Live updates on the decision.")
    assert key.title == "fed rates rise"
    assert key.title_snippet == "fed rates rise decision"


def test_similar_titles_group_and_unrelated_title_starts_new_group() -> None:
    grouper: Grouper[int] = Grouper("title_token_set", 64, WINDOW)
    grouper.add(1, EU_CANADA[0], "", NOW)
    for title in EU_CANADA[1:]:
        assert grouper.match(title, "", NOW).key == 1
    unrelated = grouper.match(GAZA, "", NOW)
    assert unrelated.key is None
    assert unrelated.best_score is not None and unrelated.best_score < 64


def test_groups_outside_attach_window_are_not_candidates() -> None:
    grouper: Grouper[int] = Grouper("title_token_set", 64, WINDOW)
    grouper.add(1, EU_CANADA[0], "", NOW - timedelta(hours=40))
    match = grouper.match(EU_CANADA[1], "", NOW)
    assert match.key is None and match.best_score is None


def test_window_measured_from_latest_article_in_group() -> None:
    grouper: Grouper[int] = Grouper("title_token_set", 64, WINDOW)
    grouper.add(1, EU_CANADA[0], "", NOW - timedelta(hours=40))
    grouper.add(1, EU_CANADA[1], "", NOW - timedelta(hours=10))
    assert grouper.match(EU_CANADA[2], "", NOW).key == 1


def test_best_matching_group_wins() -> None:
    grouper: Grouper[str] = Grouper("title_token_set", 50, WINDOW)
    grouper.add("gaza", GAZA, "", NOW)
    grouper.add("eu", EU_CANADA[0], "", NOW)
    assert grouper.match(EU_CANADA[1], "", NOW).key == "eu"


def _article(title: str, published_at: datetime, url: str) -> Article:
    return Article(
        url=url,
        source_name="Outlet",
        source_region="GLOBAL",
        source_weight=2,
        title=title,
        snippet="",
        published_at=published_at,
        fetched_at=NOW,
    )


def test_assign_to_stories_attaches_to_existing_and_creates_new(
    session: Session, settings: Settings
) -> None:
    settings.grouping.scorer = "title_token_set"
    settings.grouping.threshold = 64

    first_batch = [
        _article(EU_CANADA[0], NOW - timedelta(hours=5), "https://example.com/1"),
        _article(GAZA, NOW - timedelta(hours=4), "https://example.com/2"),
    ]
    session.add_all(first_batch)
    session.flush()
    result = assign_to_stories(session, first_batch, settings, NOW - timedelta(hours=3))
    session.commit()
    assert (result.attached, result.created) == (0, 2)

    later = NOW
    second_batch = [
        _article(EU_CANADA[1], NOW - timedelta(hours=6), "https://example.com/3"),
        _article(
            "Chess olympiad opens in Budapest", NOW - timedelta(hours=1), "https://example.com/4"
        ),
    ]
    session.add_all(second_batch)
    session.flush()
    result = assign_to_stories(session, second_batch, settings, later)
    session.commit()
    assert (result.attached, result.created) == (1, 1)

    eu_story = first_batch[0].story
    assert eu_story is not None and second_batch[0].story is eu_story
    assert eu_story.first_seen_at == NOW - timedelta(hours=6)  # earliest article
    assert eu_story.updated_at == NOW - timedelta(hours=3)  # attaching doesn't bump it
    assert eu_story.status == "new"
    assert session.query(Story).count() == 3


def test_assign_to_stories_ignores_stories_outside_window(
    session: Session, settings: Settings
) -> None:
    settings.grouping.threshold = 64
    old = _article(EU_CANADA[0], NOW - timedelta(hours=50), "https://example.com/old")
    session.add(old)
    session.flush()
    assign_to_stories(session, [old], settings, NOW - timedelta(hours=50))
    new = _article(EU_CANADA[1], NOW, "https://example.com/new")
    session.add(new)
    session.flush()
    result = assign_to_stories(session, [new], settings, NOW)
    assert result.created == 1
    assert new.story is not old.story
