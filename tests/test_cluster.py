from datetime import datetime, timedelta

import numpy as np
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Article, Story
from app.pipeline.cluster import (
    EmbeddingGrouper,
    Grouper,
    assign_to_stories,
    group_embeddings,
    make_text_key,
)
from tests.conftest import NOW
from tests.fakes import FakeEmbedder

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


def _unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def test_embedding_grouper_matches_the_closest_centroid() -> None:
    grouper: EmbeddingGrouper[str] = EmbeddingGrouper(0.8, WINDOW)
    grouper.add("x", _unit(1, 0, 0), NOW)
    grouper.add("y", _unit(0, 1, 0), NOW)
    match = grouper.match(_unit(0.9, 0.1, 0), NOW)
    assert match.key == "x" and match.best_score is not None and match.best_score > 0.99
    assert grouper.match(_unit(0.6, 0.6, 0.5), NOW).key is None  # nothing close enough


def test_centroid_moves_as_articles_join() -> None:
    grouper: EmbeddingGrouper[str] = EmbeddingGrouper(0.9, WINDOW)
    grouper.add("x", _unit(1, 0, 0), NOW)
    assert grouper.match(_unit(1, 1, 0), NOW).key is None  # cos 0.71 to (1,0,0)
    grouper.add("x", _unit(0, 1, 0), NOW)  # centroid now (1,1,0)/sqrt(2)
    assert grouper.match(_unit(1, 1, 0), NOW).key == "x"


def test_embedding_groups_outside_window_are_not_candidates() -> None:
    grouper: EmbeddingGrouper[str] = EmbeddingGrouper(0.5, WINDOW)
    grouper.add("x", _unit(1, 0), NOW - timedelta(hours=40))
    assert grouper.match(_unit(1, 0), NOW).best_score is None


def test_seed_check_stops_a_story_drifting() -> None:
    # (1,1,0) joins the (1,0,0) story, pulling its centroid over; (0.3,1,0) then clears the
    # centroid threshold but is far from the seed (1,0,0).
    drifter = _unit(0.3, 1, 0)
    for seed_threshold, joins in ((None, True), (0.5, False)):
        grouper: EmbeddingGrouper[str] = EmbeddingGrouper(0.6, WINDOW, seed_threshold)
        grouper.add("x", _unit(1, 0, 0), NOW)
        grouper.add("x", _unit(1, 1, 0), NOW)
        match = grouper.match(drifter, NOW)
        assert match.best_score is not None and match.best_score > 0.6
        assert (match.key == "x") is joins
        assert match.seed_rejected is not joins
    assert match.seed_score is not None and match.seed_score < 0.5


def test_seed_check_falls_back_to_the_next_story_that_passes_both() -> None:
    grouper: EmbeddingGrouper[str] = EmbeddingGrouper(0.6, WINDOW, 0.5)
    grouper.add("x", _unit(1, 0, 0), NOW, ref="x")
    grouper.add("x", _unit(1, 1, 0), NOW, ref="x")
    grouper.add("y", _unit(0, 1, 1.2), NOW, ref="y")  # cos 0.61 to the article: passes both
    match = grouper.match(_unit(0.3, 1, 0), NOW)
    assert match.best_ref == "x" and match.seed_rejected  # x scored best (0.63) but failed its seed
    assert match.key == "y"


def test_group_embeddings_non_news_never_starts_a_group() -> None:
    published = [NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)]
    vectors = np.stack([_unit(1, 0), _unit(0, 1), _unit(0.95, 0.05)])
    decisions = group_embeddings(published, vectors, [False, True, True], 0.8, WINDOW)
    assert decisions[0].created
    assert decisions[1].group is None  # non-news, nothing similar: left out
    assert decisions[2].group == decisions[0].group and not decisions[2].created  # attaches


def test_assign_with_embeddings(session: Session, settings: Settings) -> None:
    settings.grouping.embedding_threshold = 0.4  # bag-of-words scores run lower than the model
    settings.grouping.seed_threshold = 0.4
    articles = [
        _article(EU_CANADA[0], NOW - timedelta(hours=3), "https://example.com/e1"),
        _article(EU_CANADA[1], NOW - timedelta(hours=2), "https://example.com/e2"),
        _article(GAZA, NOW - timedelta(hours=1), "https://example.com/g1"),
        _article("What is an EU associate member for Canada?", NOW, "https://example.com/x1"),
    ]
    articles[3].non_news = True
    session.add_all(articles)
    session.flush()
    embedder = FakeEmbedder()

    result = assign_to_stories(session, articles, settings, NOW, embedder)

    assert result.method == "embedding" and embedder.calls
    assert (result.created, result.attached) == (2, 1)
    assert articles[0].story is articles[1].story is not articles[2].story
    assert result.non_news_attached + result.non_news_ungrouped == 1
    assert session.query(Story).count() == 2  # the explainer started nothing


def test_assign_falls_back_to_title_matcher_without_embedder(
    session: Session, settings: Settings
) -> None:
    articles = [_article(EU_CANADA[0], NOW, "https://example.com/t1")]
    session.add_all(articles)
    session.flush()
    assert assign_to_stories(session, articles, settings, NOW, None).method == "title"
