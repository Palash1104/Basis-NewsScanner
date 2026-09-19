"""Known grouping misses from the title-matcher era, re-run with the real embedding model and
the configured threshold. Real articles: tests/fixtures/grouping_regressions.json. Skipped if
the model can't be loaded (e.g. first run with no network)."""

import json
from datetime import datetime, timedelta

import pytest

from app.config import Settings, load_settings
from app.pipeline.classify import is_non_news
from app.pipeline.cluster import Decision, group_embeddings
from app.pipeline.dedupe import count_independent_sources
from app.pipeline.embed import Embedder, article_text, load_embedder
from tests.conftest import FIXTURES

CASES = json.loads((FIXTURES / "grouping_regressions.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def embedder() -> Embedder:
    loaded, reason = load_embedder(load_settings().grouping.embedding_model)
    if loaded is None:
        pytest.skip(f"embedding model unavailable: {reason}")
    return loaded


def _group(case: str, embedder: Embedder, settings: Settings) -> tuple[list[dict], list[Decision]]:
    articles = CASES[case]["articles"]
    decisions = group_embeddings(
        [datetime.fromisoformat(a["published_at"]) for a in articles],
        embedder.embed([article_text(a["title"], a["snippet"]) for a in articles]),
        [is_non_news(a["title"]) for a in articles],
        settings.grouping.embedding_threshold,
        timedelta(hours=settings.pipeline.story_attach_window_hours),
    )
    return articles, decisions


def test_a_split_event_groups_together(embedder: Embedder, settings: Settings) -> None:
    articles, decisions = _group("same_event_split", embedder, settings)
    case = CASES["same_event_split"]
    group_of = {a["db_id"]: d.group for a, d in zip(articles, decisions, strict=True)}
    lead_groups = {group_of[db_id] for db_id in case["must_group_together"]}
    assert len(lead_groups) == 1, "the House vote and India's reaction ended up apart"
    (lead_group,) = lead_groups
    share = sum(d.group == lead_group for d in decisions) / len(decisions)
    assert share >= case["min_share_in_one_story"], f"only {share:.0%} in one story"


def test_b_rape_case_does_not_join_ice_story(embedder: Embedder, settings: Settings) -> None:
    articles, decisions = _group("ice_vs_rape", embedder, settings)
    assert [a["title"] for a in articles][1] == "Two arrested on charges of rape"
    assert decisions[0].group != decisions[1].group


def test_c_explainer_is_non_news_and_not_a_source() -> None:
    (explainer,) = CASES["iran_sanctions_explainer"]["articles"]
    assert is_non_news(explainer["title"])

    class Row:
        def __init__(self, data: dict) -> None:
            self.source_name = data["source_name"]
            self.title = data["title"]
            self.published_at = datetime.fromisoformat(data["published_at"])
            self.non_news = is_non_news(data["title"])

    news = [Row(a) for a in CASES["same_event_split"]["articles"]]
    with_explainer = [*news, Row(explainer)]
    assert count_independent_sources(with_explainer, 90) == count_independent_sources(news, 90)


def test_c_explainer_never_starts_a_story(embedder: Embedder, settings: Settings) -> None:
    articles, decisions = _group("iran_sanctions_explainer", embedder, settings)
    assert decisions[0].group is None and not decisions[0].created


def test_d_story3_sources_stay_apart(embedder: Embedder, settings: Settings) -> None:
    articles, decisions = _group("rates_story", embedder, settings)
    nyt, aljazeera = decisions
    assert aljazeera.group is None or aljazeera.group != nyt.group
