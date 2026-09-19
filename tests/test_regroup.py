from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Article, Story
from app.pipeline.regroup import regroup_articles
from app.pipeline.summarize import STALE_STATUS
from tests.conftest import NOW
from tests.fakes import FakeEmbedder


def _article(title: str, story: Story, minutes: int) -> Article:
    return Article(
        url=f"https://example.com/{title.replace(' ', '-')}",
        source_name=f"Outlet {minutes}",
        source_region="GLOBAL",
        source_weight=2,
        title=title,
        snippet="",
        published_at=NOW + timedelta(minutes=minutes),
        fetched_at=NOW,
        story=story,
    )


def _story(headline: str, status: str = "summarized") -> Story:
    return Story(
        first_seen_at=NOW,
        updated_at=NOW,
        headline=headline,
        status=status,
        summary="old",
        processed_article_count=1,
    )


def test_regroup_merges_splits_and_marks_changed_stories_stale(
    session: Session, settings: Settings
) -> None:
    settings.grouping.embedding_threshold = 0.5
    house = _story("House passes sanctions bill")  # will absorb `reaction`
    reaction = _story("India reacts to sanctions bill")
    wrong = _story("Arrest story")  # holds an unrelated article that must split off
    same = _story("Chess olympiad", status="new")
    articles = [
        _article("House passes Russia sanctions bill tariffs India", house, 0),
        _article("India reacts Russia sanctions bill tariffs", reaction, 5),
        _article("ICE agent arrested Minneapolis assault charges", wrong, 10),
        _article("Kerala rape case two arrested", wrong, 15),
        _article("Chess olympiad opens Budapest record entries", same, 20),
        _article("What are all the sanctions Iran is under?", house, 25),  # explainer
    ]
    session.add_all(articles)
    session.flush()
    ids = {story.headline: story.id for story in (house, reaction, wrong, same)}

    report = regroup_articles(session, articles, settings, FakeEmbedder(), NOW)
    session.commit()

    assert report.articles == 6 and report.non_news == 1
    assert (report.stories_before, report.stories_after) == (4, 4)
    # house+reaction merge into the house story; reaction is deleted; the rape case splits off.
    assert articles[0].story is articles[1].story
    assert articles[0].story.id == ids["House passes sanctions bill"]
    assert session.get(Story, ids["India reacts to sanctions bill"]) is None
    assert articles[3].story is not articles[2].story and articles[3].story.status == "new"
    assert report.deleted == 1 and report.created == 1
    # Changed summarized stories are stale; the unchanged "new" story is untouched.
    house_story = articles[0].story
    assert house_story.status == STALE_STATUS and house_story.processed_article_count == 2
    assert articles[2].story.status == STALE_STATUS
    assert session.get(Story, ids["Chess olympiad"]).status == "new"
    assert report.stale == 2 and report.unchanged == 1
    # The explainer never starts a story: attached or left out, never on its own.
    explainer = articles[5]
    assert explainer.non_news
    assert explainer.story is None or explainer.story is house_story
    assert len(session.scalars(select(Story)).all()) == 4
