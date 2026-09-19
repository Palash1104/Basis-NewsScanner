"""Group articles into stories (SPEC 7.3).

Grouping is incremental: each article, oldest first, joins the most similar story whose latest
article is within the attach window, if the similarity clears the threshold; otherwise it
starts a new story.

Two matchers:
- `EmbeddingGrouper` (default): cosine similarity between the article's embedding (headline +
  snippet, all-MiniLM-L6-v2) and each story's centroid, the normalized mean of its news
  articles' embeddings. The article must also be similar enough to the story's seed (its
  earliest news article), so a story can't drift, one loosely related article at a time, into
  a topic blob.
- `Grouper`: rapidfuzz title similarity. Only used if the embedding model can't load.

Non-news articles (explainers, roundups; see classify.py) may attach to an existing story but
never start one and never move a centroid.
"""

from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import GroupingScorer, Settings
from app.models import Article, Story
from app.pipeline.dedupe import normalize_title
from app.pipeline.embed import Embedder, article_text

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
    seed_score: float | None = None  # similarity to the seed of the best-scoring group
    seed_rejected: bool = False  # the best group cleared the centroid check but not the seed


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


class EmbeddingGrouper[K: Hashable]:
    """In-memory incremental grouper over embeddings. Keys identify groups (e.g. Story objects).

    Each group keeps the sum of its members' (normalized) vectors; its centroid is that sum
    normalized, so cosine similarity to the centroid is a dot product. It also keeps its seed,
    the first vector added (callers add members oldest first). With `seed_threshold` set, an
    article joins the best-scoring group that clears both the centroid and the seed threshold.
    """

    def __init__(
        self, threshold: float, window: timedelta, seed_threshold: float | None = None
    ) -> None:
        self.threshold = threshold
        self.seed_threshold = seed_threshold
        self.window = window
        self._keys: list[K] = []
        self._index: dict[K, int] = {}
        self._refs: list[object] = []
        self._sums = np.zeros((0, 0), dtype=np.float32)
        self._centroids = np.zeros((0, 0), dtype=np.float32)
        self._seeds = np.zeros((0, 0), dtype=np.float32)
        self._last = np.zeros(0, dtype=np.float64)

    def __len__(self) -> int:
        return len(self._keys)

    def match(self, vector: np.ndarray, published_at: datetime) -> Match[K]:
        count = len(self._keys)
        if count == 0:
            return Match(None, None, None)
        eligible = self._last[:count] >= (published_at - self.window).timestamp()
        if not eligible.any():
            return Match(None, None, None)
        scores = np.where(eligible, self._centroids[:count] @ vector, -np.inf)
        best = int(np.argmax(scores))
        score = float(scores[best])
        passes = scores >= self.threshold
        if self.seed_threshold is None:
            seed_score = None
        else:
            seed_scores = self._seeds[:count] @ vector
            seed_score = float(seed_scores[best])
            passes &= seed_scores >= self.seed_threshold
        if not passes.any():
            key = None
        else:
            key = self._keys[int(np.argmax(np.where(passes, scores, -np.inf)))]
        seed_rejected = score >= self.threshold and not bool(passes[best])
        return Match(key, score, self._refs[best], seed_score, seed_rejected)

    def add(self, key: K, vector: np.ndarray, published_at: datetime, ref: object = None) -> None:
        """Add a news article to group `key` (creating the group if needed)."""
        index = self._index.get(key)
        if index is None:
            index = self._grow(key, vector.shape[0], ref)
            self._seeds[index] = vector
        self._sums[index] += vector
        norm = float(np.linalg.norm(self._sums[index]))
        self._centroids[index] = self._sums[index] / norm if norm else self._sums[index]
        self._last[index] = max(self._last[index], published_at.timestamp())

    def _grow(self, key: K, dimension: int, ref: object) -> int:
        index = len(self._keys)
        if index >= self._sums.shape[0]:
            capacity = max(64, index * 2)
            sums = np.zeros((capacity, dimension), dtype=np.float32)
            centroids = np.zeros((capacity, dimension), dtype=np.float32)
            seeds = np.zeros((capacity, dimension), dtype=np.float32)
            last = np.full(capacity, -np.inf, dtype=np.float64)
            if index:
                sums[:index] = self._sums[:index]
                centroids[:index] = self._centroids[:index]
                seeds[:index] = self._seeds[:index]
                last[:index] = self._last[:index]
            self._sums, self._centroids, self._seeds, self._last = sums, centroids, seeds, last
        self._keys.append(key)
        self._index[key] = index
        self._refs.append(ref)
        return index


@dataclass(frozen=True)
class Decision:
    """How one article was placed by `group_embeddings` (for reports and tests)."""

    index: int  # position in the input
    group: int | None  # group joined or started; None for a non-news article left ungrouped
    created: bool
    best_group: int | None  # most similar eligible group before placement
    best_score: float | None
    seed_rejected: bool = False


def group_embeddings(
    published: Sequence[datetime],
    vectors: np.ndarray,
    non_news: Sequence[bool],
    threshold: float,
    window: timedelta,
    seed_threshold: float | None = None,
) -> list[Decision]:
    """Group items from scratch, oldest first, exactly as the pipeline does. Group ids are ints;
    a group's first member is its seed. Returns one Decision per item, in input order.
    """
    grouper: EmbeddingGrouper[int] = EmbeddingGrouper(threshold, window, seed_threshold)
    decisions: dict[int, Decision] = {}
    next_group = 0
    for index in sorted(range(len(published)), key=lambda i: published[i]):
        match = grouper.match(vectors[index], published[index])
        best_group = match.best_ref if isinstance(match.best_ref, int) else None
        if non_news[index]:
            decisions[index] = Decision(
                index, match.key, False, best_group, match.best_score, match.seed_rejected
            )
            continue
        if match.key is None:
            group, created = next_group, True
            next_group += 1
        else:
            group, created = match.key, False
        grouper.add(group, vectors[index], published[index], ref=group)
        decisions[index] = Decision(
            index, group, created, best_group, match.best_score, match.seed_rejected
        )
    return [decisions[index] for index in range(len(published))]


@dataclass(frozen=True)
class Placement:
    """Where one new article went, and how close the nearest story was."""

    article: Article
    story: Story | None  # the story it joined or started; None if left out
    best_story: Story | None  # most similar eligible story before placement
    score: float | None  # similarity to best_story (cosine, or 0-100 for the title matcher)
    decision: str  # "joined" | "new story" | "non-news attached" | "non-news left out"
    seed_score: float | None = None  # similarity to best_story's seed (embedding grouping only)
    seed_rejected: bool = False  # best_story cleared the centroid check but not the seed check


@dataclass(frozen=True)
class GroupingResult:
    attached: int
    created: int
    non_news_attached: int = 0
    non_news_ungrouped: int = 0
    method: str = "title"
    placements: tuple[Placement, ...] = ()


def assign_to_stories(
    session: Session,
    articles: Sequence[Article],
    settings: Settings,
    now: datetime,
    embedder: Embedder | None = None,
) -> GroupingResult:
    """Attach unassigned `articles` to recent stories or create new stories for them.

    Uses embeddings when `settings.grouping.method` is "embedding" and an embedder is given;
    otherwise the title matcher.
    """
    use_embeddings = settings.grouping.method == "embedding" and embedder is not None
    method = "embedding" if use_embeddings else "title"
    if not articles:
        return GroupingResult(0, 0, method=method)
    window = timedelta(hours=settings.pipeline.story_attach_window_hours)
    earliest = min(article.published_at for article in articles)
    new_ids = {article.id for article in articles if article.id is not None}
    # Every news article of each story still inside the attach window, oldest first, so that
    # centroids cover whole stories and each story's first member is its seed.
    recent_stories = (
        select(Article.story_id)
        .where(Article.story_id.is_not(None), Article.published_at >= earliest - window)
        .distinct()
    )
    existing = [
        article
        for article in session.scalars(
            select(Article)
            .where(Article.story_id.in_(recent_stories))
            .order_by(Article.published_at, Article.id)
        )
        if article.id not in new_ids and article.story is not None and not article.non_news
    ]
    ordered = sorted(articles, key=lambda item: item.published_at)

    match: Callable[[int, Article], Match[Story]]
    add: Callable[[Story, int, Article], None]
    if use_embeddings:
        assert embedder is not None
        vectors = embedder.embed(
            [article_text(item.title, item.snippet) for item in [*existing, *ordered]]
        )
        grouper: EmbeddingGrouper[Story] = EmbeddingGrouper(
            settings.grouping.embedding_threshold, window, settings.grouping.seed_threshold
        )
        for item, vector in zip(existing, vectors, strict=False):
            grouper.add(item.story, vector, item.published_at, ref=item.story)  # type: ignore[arg-type]
        new_vectors = vectors[len(existing) :]

        def match(index: int, item: Article) -> Match[Story]:
            return grouper.match(new_vectors[index], item.published_at)

        def add(story: Story, index: int, item: Article) -> None:
            grouper.add(story, new_vectors[index], item.published_at, ref=story)
    else:
        title_grouper: Grouper[Story] = Grouper(
            settings.grouping.scorer, settings.grouping.threshold, window
        )
        for item in existing:
            title_grouper.add(  # type: ignore[arg-type]
                item.story, item.title, item.snippet, item.published_at, ref=item.story
            )

        def match(index: int, item: Article) -> Match[Story]:
            return title_grouper.match(item.title, item.snippet, item.published_at)

        def add(story: Story, index: int, item: Article) -> None:
            title_grouper.add(story, item.title, item.snippet, item.published_at, ref=story)

    attached = created = non_news_attached = non_news_ungrouped = 0
    placements: list[Placement] = []
    for index, article in enumerate(ordered):
        found = match(index, article)
        story = found.key
        best = found.best_ref if isinstance(found.best_ref, Story) else None
        seed = {"seed_score": found.seed_score, "seed_rejected": found.seed_rejected}
        if article.non_news:
            article.story = story
            if story is None:
                non_news_ungrouped += 1
                decision = "non-news left out"
            else:
                non_news_attached += 1
                decision = "non-news attached"
            placements.append(Placement(article, story, best, found.best_score, decision, **seed))
            continue
        decision = "joined" if story is not None else "new story"
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
            attached += 1
        article.story = story
        add(story, index, article)
        placements.append(Placement(article, story, best, found.best_score, decision, **seed))
    return GroupingResult(
        attached, created, non_news_attached, non_news_ungrouped, method, tuple(placements)
    )
