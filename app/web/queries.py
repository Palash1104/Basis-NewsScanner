"""Everything the pages read, in one place, so no page grows a query of its own.

Every function here is read-only and touches nothing but the database: prices come from
`price_cache`, never from Yahoo, so a page can neither race the pipeline's price step nor
spend its rate limit.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from app.config import AssetConfig, Settings
from app.db import SEARCH_TABLE
from app.delivery.format import SourceLink, pick_sources
from app.models import Article, Event, Impact, ImpactScore, PriceBar, RuleDisagreementRow, Story
from app.pipeline.prices import DAILY, INTRADAY, format_move, move_labels
from app.pipeline.scoring import TrackRow, story_track_line, track_record
from app.presentation import AssetCall, StoryCalls, call_rank, story_age, story_calls

# How far back the feed looks by default, and how many stories a page holds. Two days rather
# than one: the pipeline runs eight times a day, but a day with a long gap (a laptop asleep,
# see `newsdesk health`) would otherwise leave the page nearly empty. The window is printed
# on the page, so it is never guessed at.
FEED_HOURS = 48
PAGE_SIZE = 12
# The digest shows at most this many assets per story; the feed follows it.
MAX_CALLS_PER_STORY = 6

SUMMARIZED = ("summarized", "analyzed")


@dataclass(frozen=True)
class Series:
    """One asset's recent closes, for a sparkline. Empty when nothing is cached."""

    symbol: str
    closes: tuple[float, ...] = ()
    rose: bool = True

    @property
    def points(self) -> bool:
        return len(self.closes) > 1


@dataclass
class FeedStory:
    """A story as the feed shows it."""

    story: Story
    calls: StoryCalls
    sources: list[SourceLink] = field(default_factory=list)
    track_record: str | None = None
    # "3d ago", only when the story broke well before it was summarized. It sits on the
    # timestamp's own line, which is the time it is describing.
    age: str | None = None

    @property
    def shown(self) -> list[AssetCall]:
        return self.calls.shown


# ---------------------------------------------------------------- search


def search_ready(session: Session) -> bool:
    """Whether the index exists. It is built by `newsdesk run`, never by the web app."""
    found = session.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": SEARCH_TABLE},
    ).first()
    return found is not None


def _match_expression(query: str) -> str | None:
    """Turn what someone typed into an FTS5 query.

    Each word is quoted, so punctuation and FTS5's own operators (AND, NEAR, *, ") can't
    break the query or mean something the reader didn't ask for. The last word matches as a
    prefix, which is what makes search-as-you-type feel right.
    """
    words = re.findall(r"\w+", query, flags=re.UNICODE)
    if not words:
        return None
    quoted = [f'"{word}"' for word in words[:-1]]
    quoted.append(f'"{words[-1]}"*')
    return " ".join(quoted)


def search_story_ids(session: Session, query: str, limit: int = 200) -> list[int]:
    """Story ids matching `query`, best first. Empty when nothing matches."""
    expression = _match_expression(query)
    if expression is None or not search_ready(session):
        return []
    rows = session.execute(
        text(f"SELECT rowid FROM {SEARCH_TABLE} WHERE {SEARCH_TABLE} MATCH :q ORDER BY rank"),
        {"q": expression},
    ).fetchall()
    return [row[0] for row in rows[:limit]]


# ---------------------------------------------------------------- the feed


def feed_page(
    session: Session,
    *,
    now: datetime,
    hours: int = FEED_HOURS,
    region: str | None = None,
    category: str | None = None,
    query: str = "",
    offset: int = 0,
    limit: int = PAGE_SIZE,
) -> tuple[list[Story], bool]:
    """Summarized stories, most important first (SPEC 7.4), with one page of results.

    Returns the page and whether older stories remain. A search looks through everything
    stored, not just the window: someone searching has a story in mind.
    """
    statement = (
        select(Story)
        .where(Story.status.in_(SUMMARIZED))
        .options(selectinload(Story.articles), selectinload(Story.impacts))
    )
    if query:
        matches = search_story_ids(session, query)
        if not matches:
            return [], False
        statement = statement.where(Story.id.in_(matches))
    else:
        # Either the story broke inside the window, or its summary was written inside it.
        # The reserved slots often summarize Indian stories days after they broke, and
        # windowing on first_seen_at alone would hide exactly those.
        cutoff = now - timedelta(hours=hours)
        statement = statement.where((Story.first_seen_at >= cutoff) | (Story.updated_at >= cutoff))
    if category:
        statement = statement.where(Story.category == category)

    statement = statement.order_by(Story.importance_score.desc(), Story.first_seen_at.desc())
    # Regions are a JSON list, and one window holds few enough stories to filter in Python.
    stories = list(session.scalars(statement))
    if region:
        stories = [story for story in stories if region in (story.regions or [])]
    page = stories[offset : offset + limit]
    return page, len(stories) > offset + limit


def categories_in_use(session: Session) -> list[str]:
    """The categories that actually appear, so the filter can't offer an empty result."""
    rows = session.scalars(
        select(Story.category)
        .where(Story.status.in_(SUMMARIZED), Story.category.is_not(None))
        .distinct()
        .order_by(Story.category)
    )
    return [row for row in rows if row]


def feed_stories(
    session: Session,
    stories: Sequence[Story],
    assets: dict[str, AssetConfig],
    settings: Settings,
    now: datetime,
    rules: Sequence[TrackRow],
) -> list[FeedStory]:
    """Attach to each story what the digest would say about it, decided the same way."""
    impacts = [impact for story in stories for impact in story.impacts]
    labels = move_labels(session, impacts, assets, settings, now)
    names = {asset.symbol: asset.display_name for asset in assets.values()}
    minimum = settings.scoring.min_samples_to_show_rate
    min_stories = settings.scoring.min_stories_to_show_rate
    return [
        FeedStory(
            story=story,
            calls=story_calls(story.impacts, assets, MAX_CALLS_PER_STORY, labels),
            sources=pick_sources(story.articles),
            track_record=story_track_line(story, rules, minimum, names, min_stories),
            age=story_age(story, now),
        )
        for story in stories
    ]


def news_article_count(story: Story) -> int:
    return sum(1 for article in story.articles if not article.non_news)


# ---------------------------------------------------------------- sparklines


# What each window of the 1D/1W/1M control reads. Hourly bars only exist from the day an
# asset was first called, and daily history is about two months, so 1M is the longest
# window the cache can honestly fill.
WINDOWS: dict[str, tuple[str, timedelta]] = {
    "1d": (INTRADAY, timedelta(days=1)),
    "1w": (INTRADAY, timedelta(days=7)),
    "1m": (DAILY, timedelta(days=31)),
}
DEFAULT_WINDOW = "1w"


def series_for(
    session: Session, symbols: Sequence[str], window: str, now: datetime
) -> dict[str, Series]:
    """Closes per symbol for the chosen window, from the cache alone.

    An asset that has never been called has no bars, and gets an empty series: the chip then
    shows no sparkline rather than a made-up one.
    """
    interval, span = WINDOWS.get(window, WINDOWS[DEFAULT_WINDOW])
    wanted = sorted(set(symbols))
    if not wanted:
        return {}
    rows = session.execute(
        select(PriceBar.symbol, PriceBar.close, PriceBar.volume)
        .where(
            PriceBar.symbol.in_(wanted),
            PriceBar.interval == interval,
            PriceBar.ts >= now - span,
        )
        .order_by(PriceBar.symbol, PriceBar.ts)
    ).all()

    closes: dict[str, list[float]] = {symbol: [] for symbol in wanted}
    for symbol, close, volume in rows:
        # Exchange holidays leave a zero-volume filler bar on .NS stocks (CLAUDE.md); it is
        # not a session, and it would flatten the line.
        if interval == DAILY and volume == 0 and closes[symbol] and close == closes[symbol][-1]:
            continue
        closes[symbol].append(close)
    return {
        symbol: Series(
            symbol=symbol,
            closes=tuple(values),
            rose=len(values) < 2 or values[-1] >= values[0],
        )
        for symbol, values in closes.items()
    }


# The mockup draws 16 points in a 68px box. A week of hourly bars is over a hundred, which
# turns the same box into noise, so a long series is sampled down to about that many.
SPARK_MAX_POINTS = 24


def _sampled(values: Sequence[float], limit: int = SPARK_MAX_POINTS) -> list[float]:
    """Evenly spaced samples, always keeping the first and last close."""
    if len(values) <= limit:
        return list(values)
    last = len(values) - 1
    picks = {round(index * last / (limit - 1)) for index in range(limit)}
    return [values[index] for index in sorted(picks)]


def sparkline_points(series: Series, width: int, height: int, pad: int) -> str:
    """An SVG polyline, scaled to the box, as the mockup's `poly()` does."""
    values = _sampled(series.closes)
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1
    last = len(values) - 1
    points = []
    for index, value in enumerate(values):
        x = (index / last) * width
        y = height - pad - ((value - low) / span) * (height - pad * 2)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


# ---------------------------------------------------------------- the strip and the footer


def last_run_finish(session: Session, kind: str = "pipeline") -> datetime | None:
    from app.models import Run

    return session.scalars(
        select(Run.finished_at)
        .where(Run.kind == kind, Run.finished_at.is_not(None))
        .order_by(Run.finished_at.desc())
        .limit(1)
    ).first()


def story_count(session: Session, now: datetime, hours: int = FEED_HOURS) -> int:
    cutoff = now - timedelta(hours=hours)
    return (
        session.scalar(
            select(func.count())
            .select_from(Story)
            .where(
                Story.status.in_(SUMMARIZED),
                (Story.first_seen_at >= cutoff) | (Story.updated_at >= cutoff),
            )
        )
        or 0
    )


def article_sources(story: Story) -> list[Article]:
    return [article for article in story.articles if not article.non_news]


# ---------------------------------------------------------------- one story


@dataclass
class ScoredImpact:
    """One call and how it turned out, for the story page."""

    impact: Impact
    name: str
    unit: str
    move: str | None
    move_up: bool | None
    label: str | None
    scores: list[ImpactScore]


@dataclass
class StoryDetail:
    """Everything /story/{id} shows: what the story says, what it called, and how those
    calls are doing."""

    story: Story
    calls: StoryCalls
    impacts: list[ScoredImpact]
    articles: list[Article]
    event: Event | None
    disagreements: list[RuleDisagreementRow]
    track_record: str | None = None

    @property
    def priced(self) -> int:
        return sum(1 for row in self.impacts if row.move is not None)

    @property
    def judged(self) -> int:
        return sum(1 for row in self.impacts if row.scores)


def load_story(session: Session, story_id: int) -> Story | None:
    return session.scalars(
        select(Story)
        .where(Story.id == story_id)
        .options(
            selectinload(Story.articles),
            selectinload(Story.events),
            selectinload(Story.impacts).selectinload(Impact.scores),
        )
    ).first()


def story_detail(
    session: Session,
    story: Story,
    assets: dict[str, AssetConfig],
    settings: Settings,
    now: datetime,
    rules: Sequence[TrackRow],
) -> StoryDetail:
    """The story page's data. The calls are grouped exactly as the digest groups them, and
    the table below them keeps every row, because that is what the scores hang off."""
    labels = move_labels(session, story.impacts, assets, settings, now)
    ranked = sorted(story.impacts, key=lambda impact: (*call_rank(impact), impact.symbol))
    impacts = []
    for impact in ranked:
        asset = assets.get(impact.symbol)
        move = up = None
        priced = impact.reference_price is not None and impact.move_at_detection_pct is not None
        if asset and priced:
            move = format_move(asset, impact.reference_price, impact.move_at_detection_pct)
            up = impact.move_at_detection_pct >= 0
        impacts.append(
            ScoredImpact(
                impact=impact,
                name=asset.display_name if asset else impact.symbol,
                unit=" · ".join(
                    part for part in ((asset.exchange, asset.currency) if asset else ()) if part
                ),
                move=move,
                move_up=up,
                label=labels.get(impact.id),
                scores=sorted(impact.scores, key=lambda score: score.horizon_days),
            )
        )

    names = {asset.symbol: asset.display_name for asset in assets.values()}
    disagreements = list(
        session.scalars(
            select(RuleDisagreementRow)
            .where(RuleDisagreementRow.story_id == story.id)
            .order_by(RuleDisagreementRow.created_at.desc())
        )
    )
    return StoryDetail(
        story=story,
        calls=story_calls(story.impacts, assets, len(story.impacts) or 1, labels),
        impacts=impacts,
        articles=sorted(story.articles, key=lambda article: article.published_at),
        event=story.latest_event,
        disagreements=disagreements,
        track_record=story_track_line(
            story,
            rules,
            settings.scoring.min_samples_to_show_rate,
            names,
            settings.scoring.min_stories_to_show_rate,
        ),
    )


# ---------------------------------------------------------------- the track record


# The groups SPEC 11 asks for, in the order they answer questions: which rule, on what kind
# of event, from which layer, how sure it was, and over how long.
TRACK_GROUPS: tuple[tuple[str, str], ...] = (
    ("rule_id", "By rule"),
    ("event_type", "By event type"),
    ("origin", "By origin"),
    ("confidence", "By stated confidence"),
    ("horizon_days", "By horizon"),
)
# Settings that are fixed today. A table per value is noise until one of them changes, so
# they appear only once more than one value has been judged.
TRACK_GROUPS_IF_VARIED: tuple[tuple[str, str], ...] = (
    ("prompt_version", "By extraction prompt"),
    ("temperature", "By temperature"),
    ("seed", "By seed"),
)


@dataclass(frozen=True)
class TrackTable:
    """One grouping of the track record, ready to render."""

    group: str
    title: str
    rows: list[TrackRow]


@dataclass(frozen=True)
class TrackSummary:
    """The whole record at a glance: what has been judged, and what is still open."""

    judged: int
    hits: int
    misses: int
    no_move: int
    unscorable: int
    stories: int
    minimum: int
    min_stories: int
    early_below_stories: int

    @property
    def rate(self) -> float | None:
        return self.hits / self.judged if self.judged else None

    @property
    def shows_rate(self) -> bool:
        return self.judged >= self.minimum and self.stories >= self.min_stories

    @property
    def early(self) -> bool:
        """Shown, but on too few stories to be a measurement yet."""
        return self.stories < self.early_below_stories


def track_tables(
    session: Session, settings: Settings, horizon: int | None = None
) -> tuple[TrackSummary, list[TrackTable]]:
    """Every table SPEC 11 asks for, at one horizon or across all of them."""
    tables = [
        TrackTable(group, title, track_record(session, group, horizon))
        for group, title in TRACK_GROUPS
    ]
    for group, title in TRACK_GROUPS_IF_VARIED:
        rows = track_record(session, group, horizon)
        if len({row.key for row in rows}) > 1:
            tables.append(TrackTable(group, title, rows))

    # Summing any one grouping counts every judged call exactly once.
    counted = next((table.rows for table in tables if table.group == "rule_id"), [])
    summary = TrackSummary(
        judged=sum(row.judged for row in counted),
        hits=sum(row.hits for row in counted),
        misses=sum(row.misses for row in counted),
        no_move=sum(row.no_move for row in counted),
        unscorable=sum(row.unscorable for row in counted),
        stories=len({story for row in counted for story in row.stories}),
        minimum=settings.scoring.min_samples_to_show_rate,
        min_stories=settings.scoring.min_stories_to_show_rate,
        early_below_stories=settings.scoring.early_rate_below_stories,
    )
    return summary, [table for table in tables if table.rows]
