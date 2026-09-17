"""Group articles into stories by text similarity (Phase 1: rapidfuzz).

Grouping is incremental: each article, oldest first, joins the most similar story whose latest
article is within the attach window, if the similarity clears the threshold; otherwise it
starts a new story.
"""

from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import GroupingScorer, Settings
from app.models import Article, Story
from app.pipeline.dedupe import normalize_title

STOPWORDS = frozenset(
    """
    a an the and or but if of at by for with about against between into through during before
    after above below to from up down in out on off over under again further then once here
    there when where why how all any both each few more most other some such no nor not only
    own same so than too very can will just should now is are was were be been being have has
    had having do does did doing i me my we our you your he him his she her it its they them
    their what which who whom this that these those am as until while s t would could may might
    must shall amid via vs per also says said say new news latest live updates update today
    report reports video watch explained
    """.split()  # noqa: SIM905
)


@dataclass(frozen=True)
class TextKey:
    title: str  # normalized title, stopwords removed
    title_snippet: str  # normalized title + snippet, stopwords removed


def make_text_key(title: str, snippet: str) -> TextKey:
    def content_words(text: str) -> str:
        return " ".join(word for word in normalize_title(text).split() if word not in STOPWORDS)

    title_key = content_words(title)
    return TextKey(title=title_key, title_snippet=f"{title_key} {content_words(snippet)}".strip())


def _all_scores(query: str, choices: Sequence[str], scorer: Callable[..., float]) -> list[float]:
    scores = [0.0] * len(choices)
    for _choice, score, index in process.extract_iter(
        query, choices, scorer=scorer, processor=None
    ):
        scores[index] = score
    return scores


def score_against(
    scorer: GroupingScorer, key: TextKey, titles: Sequence[str], title_snippets: Sequence[str]
) -> list[float]:
    """Similarity (0-100) of `key` against every member."""
    match scorer:
        case "title_token_set":
            return _all_scores(key.title, titles, fuzz.token_set_ratio)
        case "title_token_sort":
            return _all_scores(key.title, titles, fuzz.token_sort_ratio)
        case "title_snippet_blend":
            title_scores = _all_scores(key.title, titles, fuzz.token_sort_ratio)
            text_scores = _all_scores(key.title_snippet, title_snippets, fuzz.token_set_ratio)
            return [0.6 * t + 0.4 * s for t, s in zip(title_scores, text_scores, strict=True)]


@dataclass(frozen=True)
class Match[K]:
    key: K | None  # group to join, or None to start a new one
    best_score: float | None  # best score among eligible groups, even if below threshold
    best_ref: object | None  # member that produced best_score


class Grouper[K: Hashable]:
    """In-memory incremental grouper. Keys identify groups (e.g. Story objects)."""

    def __init__(self, scorer: GroupingScorer, threshold: float, window: timedelta) -> None:
        self.scorer: GroupingScorer = scorer
        self.threshold = threshold
        self.window = window
        self._titles: list[str] = []
        self._title_snippets: list[str] = []
        self._member_group: list[K] = []
        self._member_ref: list[object] = []
        self._group_last: dict[K, datetime] = {}

    def match(self, title: str, snippet: str, published_at: datetime) -> Match[K]:
        if not self._titles:
            return Match(None, None, None)
        scores = score_against(
            self.scorer, make_text_key(title, snippet), self._titles, self._title_snippets
        )
        cutoff = published_at - self.window
        best_key: K | None = None
        best_score: float | None = None
        best_ref: object | None = None
        for index, score in enumerate(scores):
            group = self._member_group[index]
            if self._group_last[group] < cutoff:
                continue
            if best_score is None or score > best_score:
                best_key, best_score, best_ref = group, score, self._member_ref[index]
        if best_score is None or best_score < self.threshold:
            return Match(None, best_score, best_ref)
        return Match(best_key, best_score, best_ref)

    def add(
        self, key: K, title: str, snippet: str, published_at: datetime, ref: object = None
    ) -> None:
        text = make_text_key(title, snippet)
        self._titles.append(text.title)
        self._title_snippets.append(text.title_snippet)
        self._member_group.append(key)
        self._member_ref.append(ref)
        last = self._group_last.get(key)
        self._group_last[key] = published_at if last is None else max(last, published_at)


@dataclass(frozen=True)
class GroupingResult:
    attached: int
    created: int


def assign_to_stories(
    session: Session, articles: Sequence[Article], settings: Settings, now: datetime
) -> GroupingResult:
    """Attach unassigned `articles` to recent stories or create new stories for them."""
    if not articles:
        return GroupingResult(0, 0)
    window = timedelta(hours=settings.pipeline.story_attach_window_hours)
    earliest = min(article.published_at for article in articles)
    new_ids = {article.id for article in articles if article.id is not None}
    existing = session.scalars(
        select(Article)
        .where(Article.story_id.is_not(None), Article.published_at >= earliest - window)
        .order_by(Article.published_at)
    ).all()

    grouper: Grouper[Story] = Grouper(settings.grouping.scorer, settings.grouping.threshold, window)
    for article in existing:
        if article.id not in new_ids and article.story is not None:
            grouper.add(article.story, article.title, article.snippet, article.published_at)

    attached = created = 0
    for article in sorted(articles, key=lambda item: item.published_at):
        story = grouper.match(article.title, article.snippet, article.published_at).key
        if story is None:
            story = Story(
                first_seen_at=article.published_at,
                updated_at=now,
                headline=article.title,
                status="new",
            )
            session.add(story)
            created += 1
        else:
            story.first_seen_at = min(story.first_seen_at, article.published_at)
            story.updated_at = now
            attached += 1
        article.story = story
        grouper.add(story, article.title, article.snippet, article.published_at)
    return GroupingResult(attached=attached, created=created)
