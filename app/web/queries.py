"""Everything the pages read, in one place, so no page grows a query of its own.

Every function here is read-only and touches nothing but the database: prices come from
`price_cache`, never from Yahoo, so a page can neither race the pipeline's price step nor
spend its rate limit.
"""

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from app import health
from app.config import AssetConfig, Settings
from app.db import SEARCH_TABLE
from app.delivery.format import SourceLink, pick_sources
from app.health import SlotDay
from app.models import (
    Article,
    Event,
    Impact,
    ImpactScore,
    PriceBar,
    RuleDisagreementRow,
    Run,
    Story,
)
from app.pipeline.prices import DAILY, INTRADAY, format_move, move_labels, move_pct
from app.pipeline.scoring import TrackRow, story_track_line, track_record
from app.presentation import AssetCall, StoryCalls, call_rank, story_age, story_calls

# How far back the feed looks by default, and how many stories a page holds. Two days rather
# than one: the pipeline runs eight times a day, but a day with a long gap (a laptop asleep,
# see `newsdesk health`) would otherwise leave the page nearly empty. The window is printed
# on the page, so it is never guessed at.
FEED_HOURS = 48
# The first page holds everything that broke today, however much that is, so the home screen
# is one scroll through the day and "Earlier stories" is the deliberate step back (user,
# 2026-09-23). PAGE_SIZE is the floor, so an empty morning still shows yesterday's evening;
# FIRST_PAGE_MAX is the ceiling, so a day with 80 stories doesn't render them all at once.
PAGE_SIZE = 12
FIRST_PAGE_MAX = 60
# The digest shows at most this many assets per story; the feed follows it.
MAX_CALLS_PER_STORY = 6

SUMMARIZED = ("summarized", "analyzed")

# The right rail's "Biggest movers - 24h". The window is the last 24 hours, so an asset whose
# market has been shut for all of them simply isn't in it; nothing is stretched to fill the
# list. Five days of lead is how far back the bar that opens the window may sit: enough for a
# long weekend, short enough that a delisted symbol drops out.
MOVER_HOURS = 24
MOVER_LIMIT = 6
MOVER_REFERENCE_LEAD = timedelta(days=5)
# Both rail lists draw the same window, so the two sets of sparklines can be read against each
# other: a week (user, 2026-09-22). The moves beside them stay 24-hour, and the rail says so.
RAIL_WINDOW = "1w"


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
    day_start: datetime | None = None,
) -> tuple[list[Story], bool]:
    """Summarized stories, newest first, with one page of results.

    Newest first, not most important first (user, 2026-09-22): the page is read like a feed,
    several times a day, and the same important story sitting at the top of it for two days
    hides what has happened since. The digest is still ordered by importance - it is sent
    twice a day, and there the ranking is the whole point.

    Returns the page and whether older stories remain. A search looks through everything
    stored, not just the window: someone searching has a story in mind.

    With `day_start`, the first page grows to hold every story that broke since then - the
    home screen is meant to be one scroll through today, with "Earlier stories" as the step
    back into yesterday.
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

    statement = statement.order_by(Story.first_seen_at.desc(), Story.id.desc())
    # Regions are a JSON list, and one window holds few enough stories to filter in Python.
    stories = list(session.scalars(statement))
    if region:
        stories = [story for story in stories if region in (story.regions or [])]
    if offset == 0 and not query and day_start is not None:
        today = sum(1 for story in stories if story.first_seen_at >= day_start)
        limit = min(max(today, limit), FIRST_PAGE_MAX)
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
    early_below = settings.scoring.early_rate_below_stories
    return [
        FeedStory(
            story=story,
            calls=story_calls(story.impacts, assets, MAX_CALLS_PER_STORY, labels),
            sources=pick_sources(story.articles),
            track_record=story_track_line(story, rules, minimum, names, min_stories, early_below),
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


@dataclass(frozen=True)
class Mover:
    """One asset's move over the last 24 hours, for the mockup's right rail."""

    symbol: str
    name: str
    change: str  # "+1.8%", or points for a rate
    up: bool
    pct: float
    series: Series | None = None  # the week behind the move; None when nothing is cached


def _newest_close_in(session: Session, lower: datetime, upper: datetime) -> dict[str, float]:
    """Each symbol's newest 60-minute close in [lower, upper). One grouped query, so a page
    never reads a window's worth of bars for every asset in the universe."""
    newest = (
        select(PriceBar.symbol, func.max(PriceBar.ts).label("ts"))
        .where(PriceBar.interval == INTRADAY, PriceBar.ts >= lower, PriceBar.ts < upper)
        .group_by(PriceBar.symbol)
        .subquery()
    )
    rows = session.execute(
        select(PriceBar.symbol, PriceBar.close)
        .join(newest, (PriceBar.symbol == newest.c.symbol) & (PriceBar.ts == newest.c.ts))
        .where(PriceBar.interval == INTRADAY)
    ).all()
    return {symbol: close for symbol, close in rows}


def window_moves(
    session: Session, now: datetime, hours: int = MOVER_HOURS
) -> dict[str, tuple[float, float]]:
    """(reference close, latest close) per symbol over the last `hours`.

    A symbol needs a bar inside the window *and* one at or before it opens; a market that has
    been shut for all of it has no move to show, and a stale last price is not one. Two
    grouped queries, so a page never reads a window's worth of bars for the whole universe.
    """
    start = now - timedelta(hours=hours)
    latest = _newest_close_in(session, start, now + timedelta(hours=1))
    before = _newest_close_in(session, start - MOVER_REFERENCE_LEAD, start)
    return {
        symbol: (before[symbol], close) for symbol, close in latest.items() if before.get(symbol)
    }


def movers(
    session: Session,
    assets: dict[str, AssetConfig],
    now: datetime,
    limit: int = MOVER_LIMIT,
    hours: int = MOVER_HOURS,
    window: str = RAIL_WINDOW,
) -> list[Mover]:
    """The biggest 24-hour moves across the whole universe, largest first, each with the week
    behind it.

    The pipeline caches 60-minute bars for every asset, not only the ones a story called, so
    this can rank the universe. The lines are fetched after the ranking, so a page reads bars
    for the handful shown and not for all 82.
    """
    rows = []
    for symbol, (reference, close) in window_moves(session, now, hours).items():
        asset = assets.get(symbol)
        if asset is None:
            continue
        pct = move_pct(reference, close)
        rows.append(
            Mover(
                symbol=symbol,
                name=asset.display_name,
                change=format_move(asset, reference, pct),
                up=pct >= 0,
                pct=pct,
            )
        )
    rows.sort(key=lambda mover: abs(mover.pct), reverse=True)
    shown = rows[:limit]
    series = series_for(session, [mover.symbol for mover in shown], window, now)
    return [replace(mover, series=series.get(mover.symbol)) for mover in shown]


# The currency symbols the universe actually uses (see config/assets.yaml). Grains quote in
# US cents, which is written after the number, the way a price page writes it.
CURRENCY_MARKS = {"USD": "$", "INR": "₹"}


def format_price(asset: AssetConfig, close: float) -> str:
    """A last price as the design writes one: "$71.40", "₹9,610", "412¢". Big numbers drop
    the paise; a currency we have no mark for keeps its code, rather than a guessed symbol."""
    figure = f"{close:,.0f}" if abs(close) >= 1000 else f"{close:,.2f}"
    if asset.currency == "USX":
        return f"{figure}¢"
    mark = CURRENCY_MARKS.get(asset.currency or "")
    return f"{mark}{figure}" if mark else f"{figure} {asset.currency}".strip()


@dataclass(frozen=True)
class WatchRow:
    """One asset on the rail's watchlist: what it costs, where it has been, how far it moved."""

    symbol: str
    name: str
    price: str | None  # the last cached close; None when nothing is cached
    change: str | None  # over the same window as the movers
    up: bool
    series: Series


def watchlist_symbols(
    wanted: Sequence[str], assets: dict[str, AssetConfig], limit: int
) -> list[str]:
    """The symbols to show, in the order asked for: known, deduplicated and capped.

    Anything not in the universe is dropped rather than shown as an empty row - the list can
    arrive from settings.yaml or from a browser, and neither is checked anywhere else.
    """
    seen: list[str] = []
    for symbol in wanted:
        cleaned = symbol.strip()
        if cleaned in assets and cleaned not in seen:
            seen.append(cleaned)
    return seen[:limit]


def watchlist_rows(
    session: Session,
    symbols: Sequence[str],
    assets: dict[str, AssetConfig],
    now: datetime,
    window: str = RAIL_WINDOW,
    hours: int = MOVER_HOURS,
) -> list[WatchRow]:
    """The watchlist, in the order the symbols were given - it is a list someone chose, so it
    is not re-sorted by size the way the movers are."""
    if not symbols:
        return []
    moves = window_moves(session, now, hours)
    series = series_for(session, symbols, window, now)
    rows = []
    for symbol in symbols:
        asset = assets[symbol]
        move = moves.get(symbol)
        pct = move_pct(*move) if move else None
        rows.append(
            WatchRow(
                symbol=symbol,
                name=asset.display_name,
                price=format_price(asset, move[1]) if move else None,
                change=format_move(asset, move[0], pct) if move and pct is not None else None,
                up=pct is None or pct >= 0,
                series=series.get(symbol, Series(symbol)),
            )
        )
    return rows


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
            settings.scoring.early_rate_below_stories,
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


# ---------------------------------------------------------------- assets


@dataclass(frozen=True)
class AssetRow:
    """One asset on the index: how often it is called, and how those calls have gone."""

    asset: AssetConfig
    calls: int
    stories: int
    last_called: datetime | None
    hits: int
    misses: int
    no_move: int

    @property
    def judged(self) -> int:
        return self.hits + self.misses

    @property
    def rate(self) -> float | None:
        return self.hits / self.judged if self.judged else None

    def shows_rate(self, minimum: int, min_stories: int) -> bool:
        return self.judged >= minimum and self.stories >= min_stories


@dataclass(frozen=True)
class AssetStory:
    """One story that called this asset, with what it said and how it turned out."""

    story: Story
    impacts: list[ScoredImpact]


@dataclass(frozen=True)
class AssetDetail:
    """What keeps moving one asset, and whether those calls were right."""

    asset: AssetConfig
    row: AssetRow
    stories: list[AssetStory]
    channels: list[tuple[str, int]]
    rules: list[tuple[str, int]]
    series: Series
    latest_close: float | None


def asset_rows(
    session: Session, assets: dict[str, AssetConfig], settings: Settings
) -> list[AssetRow]:
    """Every universe asset that has been called, most recently called first.

    Assets nobody has called are left out rather than listed with zeroes: the universe is 82
    symbols and an empty row says nothing.
    """
    counts = session.execute(
        select(
            Impact.symbol,
            func.count(Impact.id),
            func.count(func.distinct(Impact.story_id)),
            func.max(Impact.created_at),
        ).group_by(Impact.symbol)
    ).all()
    outcomes: dict[str, Counter[str]] = {}
    for symbol, outcome in session.execute(
        select(Impact.symbol, ImpactScore.outcome).join(
            ImpactScore, ImpactScore.impact_id == Impact.id
        )
    ):
        outcomes.setdefault(symbol, Counter())[outcome] += 1

    rows = []
    for symbol, calls, stories, last in counts:
        asset = assets.get(symbol)
        if asset is None:
            continue  # dropped from the universe since the call was made
        tally = outcomes.get(symbol, Counter())
        rows.append(
            AssetRow(
                asset=asset,
                calls=calls,
                stories=stories,
                last_called=last,
                hits=tally["hit"],
                misses=tally["miss"],
                no_move=tally["no_move"],
            )
        )
    rows.sort(key=lambda row: row.last_called or datetime.min.replace(tzinfo=UTC), reverse=True)
    return rows


def asset_detail(
    session: Session,
    asset: AssetConfig,
    settings: Settings,
    now: datetime,
    window: str = DEFAULT_WINDOW,
    limit: int = 25,
) -> AssetDetail:
    """One asset: the stories that called it, what drove them, and the outcomes.

    The channel and rule counts are the design's "exposure" panel, made from data we have:
    what keeps moving this asset, counted, not scored.
    """
    impacts = list(
        session.scalars(
            select(Impact)
            .where(Impact.symbol == asset.symbol)
            .options(
                selectinload(Impact.scores),
                selectinload(Impact.story).selectinload(Story.articles),
                selectinload(Impact.event),
            )
            .order_by(Impact.created_at.desc())
        )
    )
    labels = move_labels(session, impacts, {asset.symbol: asset}, settings, now)

    by_story: dict[int, list[Impact]] = {}
    for impact in impacts:
        by_story.setdefault(impact.story_id, []).append(impact)

    stories = []
    for group in list(by_story.values())[:limit]:
        story = group[0].story
        rows = []
        for impact in sorted(group, key=call_rank):
            move = up = None
            priced = impact.reference_price is not None and impact.move_at_detection_pct is not None
            if priced:
                move = format_move(asset, impact.reference_price, impact.move_at_detection_pct)
                up = impact.move_at_detection_pct >= 0
            rows.append(
                ScoredImpact(
                    impact=impact,
                    name=asset.display_name,
                    unit=" · ".join(part for part in (asset.exchange, asset.currency) if part),
                    move=move,
                    move_up=up,
                    label=labels.get(impact.id),
                    scores=sorted(impact.scores, key=lambda score: score.horizon_days),
                )
            )
        stories.append(AssetStory(story=story, impacts=rows))

    channels = Counter(
        channel for impact in impacts if impact.event for channel in (impact.event.channels or [])
    )
    rules = Counter(impact.rule_id for impact in impacts if impact.rule_id)
    series = series_for(session, [asset.symbol], window, now).get(
        asset.symbol, Series(asset.symbol)
    )
    row = next(
        (row for row in asset_rows(session, {asset.symbol: asset}, settings)),
        AssetRow(asset=asset, calls=0, stories=0, last_called=None, hits=0, misses=0, no_move=0),
    )
    return AssetDetail(
        asset=asset,
        row=row,
        stories=stories,
        channels=channels.most_common(),
        rules=rules.most_common(),
        series=series,
        latest_close=series.closes[-1] if series.closes else None,
    )


# ---------------------------------------------------------------- runs


@dataclass(frozen=True)
class RunRow:
    """One run, as the page shows it."""

    run: Run
    minutes: float | None

    @property
    def errors(self) -> list[dict[str, object]]:
        return list(self.run.errors or [])


@dataclass(frozen=True)
class UsageRow:
    """One quota day's requests per model, against the budget and the hard cap."""

    day: str
    used: dict[str, int]
    budget: dict[str, int | None]
    cap: dict[str, int | None]

    def over_budget(self, model: str) -> bool:
        budget = self.budget.get(model)
        return budget is not None and self.used.get(model, 0) > budget


@dataclass(frozen=True)
class RunsView:
    """What `/runs` shows: whether the schedule is being kept, what it cost, what broke."""

    days: int
    runs: list[RunRow]
    slots: list[SlotDay]
    slots_ran: int
    slots_missed: int
    usage: list[UsageRow]
    models: list[str]
    rerank_fallbacks: int
    pipeline_runs: int
    layer_b_calls: int
    layer_b_declines: int
    input_tokens: int
    output_tokens: int

    @property
    def decline_rate(self) -> float | None:
        return self.layer_b_declines / self.layer_b_calls if self.layer_b_calls else None


def runs_view(session: Session, settings: Settings, now: datetime, days: int) -> RunsView:
    """The health picture, assembled from the same functions `newsdesk health` prints, so the
    page and the command can never disagree."""
    tz = settings.tz
    first = datetime.combine(
        now.astimezone(tz).date() - timedelta(days=days - 1), time(0, 0), tzinfo=tz
    )
    runs = sorted(
        (
            run
            for kind in ("pipeline", "digest", "score")
            for run in health.runs_since(session, kind, first)
        ),
        key=lambda run: run.started_at,
        reverse=True,
    )
    rows = [
        RunRow(
            run=run,
            minutes=(
                (run.finished_at - run.started_at).total_seconds() / 60 if run.finished_at else None
            ),
        )
        for run in runs
    ]
    slots = health.slot_days(session, settings, now, days)
    pipeline = [run for run in runs if run.kind == "pipeline"]
    calls, declines = health.layer_b_totals(pipeline)

    models = list(dict.fromkeys([settings.llm.summary_model, settings.llm.reasoning_model]))
    quota_days = health.quota_days(settings, now, days)
    counts = health.requests_per_day(session, models, quota_days)
    limits = settings.llm.rate_limits
    usage = [
        UsageRow(
            day=day,
            used={model: counts.get((day, model), 0) for model in models},
            budget={
                model: limits[model].daily_budget if model in limits else None for model in models
            },
            cap={
                model: limits[model].requests_per_day if model in limits else None
                for model in models
            },
        )
        for day in quota_days
    ]
    return RunsView(
        days=days,
        runs=rows,
        slots=slots,
        slots_ran=sum(len(day.ran) for day in slots),
        slots_missed=sum(len(day.missed) for day in slots),
        usage=usage,
        models=models,
        rerank_fallbacks=health.rerank_fallbacks(pipeline),
        pipeline_runs=len(pipeline),
        layer_b_calls=calls,
        layer_b_declines=declines,
        input_tokens=sum(run.input_tokens for run in runs),
        output_tokens=sum(run.output_tokens for run in runs),
    )
