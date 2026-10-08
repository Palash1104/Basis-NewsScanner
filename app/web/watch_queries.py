"""Every read the watchlist pages make: `/watchlist`, a watchlist story, and the rail's list.

The watchlist is config/watchlist.yaml, one list for stocks and commodities (user,
2026-10-07); the browser-stored list it replaced is gone. Like every page, these only read:
prices from `price_cache` (the pipeline caches 60-minute bars for the universe and the
watchlist) and the scanner's own polls (`watch_prices`), calls from `watch_calls`, and the
playbook's impacts for anything the universe also prices.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import AssetConfig, Settings, WatchItem, WatchlistFile
from app.models import (
    Impact,
    PriceBar,
    Story,
    WatchAlert,
    WatchArticle,
    WatchCall,
    WatchPrice,
    WatchStory,
)
from app.pipeline.prices import INTRADAY, format_move, move_pct
from app.watch.alerts import MARK, current_calls, first_reported, shown, story_state
from app.watch.analyse import kept_symbols
from app.web.queries import (
    MOVER_HOURS,
    RAIL_WINDOW,
    Series,
    format_price,
    series_for,
    window_moves,
)

NEWS_DAYS = 7
ALERT_DAYS = 7
DRIVER_DAYS = 7


@dataclass(frozen=True)
class WatchCard:
    """One watchlist entry, as the design's watchlist card draws it."""

    symbol: str
    name: str
    kicker: str  # "NSE · Defence", or the exchange and currency of a commodity
    price: str | None
    change: str | None
    change_label: str  # "today" from the scanner's own poll, else "24h" from the cache
    up: bool
    series: Series
    driver: str | None  # the latest call's reason, or the playbook's mechanism
    driver_mark: str | None
    href: str | None  # the asset page, for anything the universe prices
    stock: bool


@dataclass(frozen=True)
class Hit:
    name: str
    mark: str
    followed: bool


@dataclass(frozen=True)
class NewsRow:
    href: str
    at: datetime
    headline: str
    hits: tuple[Hit, ...]
    tag: str
    lead_change: str | None
    lead_up: bool
    lead_series: Series | None


@dataclass(frozen=True)
class TimelineItem:
    at: datetime
    kind: str  # article | filing
    source: str
    detail: str  # how it arrived, or the filing's subject
    text: str
    url: str | None


@dataclass
class WatchStoryView:
    story: WatchStory
    reported: datetime
    state: str
    summary: str | None
    calls: list[WatchCall]
    passing: list[WatchCall]
    timeline: list[TimelineItem]
    prices: list[tuple[str, str, str | None, bool, Series]] = field(default_factory=list)


def _inr(close: float) -> str:
    figure = f"{close:,.0f}" if abs(close) >= 1000 else f"{close:,.2f}"
    return f"₹{figure}"


def _pct(value: float) -> str:
    return f"{value:+.1%}"


def _midnight(now: datetime, settings: Settings) -> datetime:
    return datetime.combine(now.astimezone(settings.tz).date(), time(0), tzinfo=settings.tz)


def _live(session: Session, symbols: Sequence[str], now: datetime, settings: Settings) -> dict:
    """Each symbol's latest poll today, from the scanner, with a previous close."""
    found: dict[str, WatchPrice] = {}
    for row in session.scalars(
        select(WatchPrice)
        .where(
            WatchPrice.symbol.in_(symbols),
            WatchPrice.polled_at >= _midnight(now, settings),
            WatchPrice.price.is_not(None),
            WatchPrice.previous_close.is_not(None),
        )
        .order_by(WatchPrice.polled_at)
    ):
        found[row.symbol] = row
    return found


def _poll_series(session: Session, symbol: str, now: datetime) -> Series:
    """A week of the scanner's own polls, for a stock the cache has no bars for yet."""
    values = list(
        session.scalars(
            select(WatchPrice.price)
            .where(
                WatchPrice.symbol == symbol,
                WatchPrice.polled_at >= now - timedelta(days=7),
                WatchPrice.price.is_not(None),
            )
            .order_by(WatchPrice.polled_at)
        )
    )
    return Series(
        symbol=symbol, closes=tuple(values), rose=len(values) < 2 or values[-1] >= values[0]
    )


def _drivers(
    session: Session, watchlist: WatchlistFile, now: datetime
) -> dict[str, tuple[str, str]]:
    """What last moved each entry: a stock's latest real call (never a passing mention), a
    commodity's latest playbook mechanism."""
    since = now - timedelta(days=DRIVER_DAYS)
    found: dict[str, tuple[str, str]] = {}
    stocks = [s.symbol for s in watchlist.stocks]
    for call in session.scalars(
        select(WatchCall)
        .where(
            WatchCall.symbol.in_(stocks),
            WatchCall.created_at >= since,
            WatchCall.relevance != "passing",
        )
        .order_by(WatchCall.created_at, WatchCall.id)
    ):
        found[call.symbol] = (call.reason, MARK[call.sentiment])
    others = [i.symbol for i in watchlist.watchlist if i.symbol not in found]
    for impact in session.scalars(
        select(Impact)
        .where(Impact.symbol.in_(others), Impact.created_at >= since)
        .order_by(Impact.created_at, Impact.id)
    ):
        found[impact.symbol] = (
            impact.mechanism,
            MARK["positive" if impact.direction == "up" else "negative"],
        )
    return found


def _kicker(item: WatchItem, watchlist: WatchlistFile, asset: AssetConfig | None) -> str:
    if item.type == "stock":
        group = watchlist.groups.get(item.group or "")
        return " · ".join(part for part in ("NSE", group.name if group else None) if part)
    if asset is None:
        return ""
    return " · ".join(part for part in (asset.exchange, asset.currency) if part)


def _name(item: WatchItem, asset: AssetConfig | None) -> str:
    return item.name or (asset.display_name if asset else item.symbol)


def watch_cards(
    session: Session,
    watchlist: WatchlistFile,
    assets: dict[str, AssetConfig],
    settings: Settings,
    now: datetime,
) -> list[WatchCard]:
    """Every watchlist entry in the file's order, priced the most current way there is: the
    scanner's own poll today for a stock, else the 24-hour move from the cache."""
    symbols = [item.symbol for item in watchlist.watchlist]
    moves = window_moves(session, now, MOVER_HOURS)
    series = series_for(session, symbols, RAIL_WINDOW, now)
    live = _live(session, symbols, now, settings)
    drivers = _drivers(session, watchlist, now)
    cards = []
    for item in watchlist.watchlist:
        asset = assets.get(item.symbol)
        price = change = None
        label, up = "24h", True
        poll = live.get(item.symbol)
        if poll is not None and item.type == "stock":
            pct = poll.price / poll.previous_close - 1  # type: ignore[operator]
            price, change, label, up = _inr(poll.price), _pct(pct), "today", pct >= 0  # type: ignore[arg-type]
        elif item.symbol in moves:
            reference, close = moves[item.symbol]
            pct = move_pct(reference, close)
            price = format_price(asset, close) if asset else _inr(close)
            change = format_move(asset, reference, pct) if asset else _pct(pct / 100)
            up = pct >= 0
        line = series.get(item.symbol, Series(item.symbol))
        if not line.points and item.type == "stock":
            line = _poll_series(session, item.symbol, now)
        driver = drivers.get(item.symbol)
        cards.append(
            WatchCard(
                symbol=item.symbol,
                name=_name(item, asset),
                kicker=_kicker(item, watchlist, asset),
                price=price,
                change=change,
                change_label=label,
                up=up,
                series=line,
                driver=driver[0] if driver else None,
                driver_mark=driver[1] if driver else None,
                href=f"/asset/{item.symbol}" if asset else None,
                stock=item.type == "stock",
            )
        )
    return cards


def _move_since(session: Session, symbol: str, since: datetime) -> float | None:
    """The move from the last cached hourly close at or before `since` to the newest one."""
    before = session.scalars(
        select(PriceBar.close)
        .where(PriceBar.symbol == symbol, PriceBar.interval == INTRADAY, PriceBar.ts <= since)
        .order_by(PriceBar.ts.desc())
        .limit(1)
    ).first()
    latest = session.scalars(
        select(PriceBar.close)
        .where(PriceBar.symbol == symbol, PriceBar.interval == INTRADAY, PriceBar.ts > since)
        .order_by(PriceBar.ts.desc())
        .limit(1)
    ).first()
    if before is None or latest is None:
        return None
    return move_pct(before, latest) / 100  # move_pct is in percent; this is a fraction


def _stories(session: Session, since: datetime) -> list[WatchStory]:
    return list(
        session.scalars(
            select(WatchStory)
            .where(WatchStory.first_seen_at >= since)
            .options(
                selectinload(WatchStory.articles).selectinload(WatchArticle.matches),
                selectinload(WatchStory.articles).selectinload(WatchArticle.sightings),
                selectinload(WatchStory.filings),
            )
        )
    )


def short_state(story: WatchStory) -> str:
    kinds = {f.kind for f in story.filings}
    if "company_reply" in kinds:
        return "company responded"
    if "clarification_sought" in kinds:
        return "NSE asked"
    if "filing" in kinds:
        return "filed"
    return "media report"


def watch_news(
    session: Session,
    watchlist: WatchlistFile,
    assets: dict[str, AssetConfig],
    settings: Settings,
    now: datetime,
) -> list[NewsRow]:
    """News touching the watchlist this week, newest first: the scanner's stories with a real
    call (never a passing mention), and the pipeline's stories whose playbook impacts touch a
    watched symbol (e.g. Brent from an oil story)."""
    names = {item.symbol: _name(item, assets.get(item.symbol)) for item in watchlist.watchlist}
    order = [item.symbol for item in watchlist.watchlist]
    since = now - timedelta(days=NEWS_DAYS)
    rows: list[NewsRow] = []
    leads: dict[int, str] = {}  # id(row) -> the symbol its line is drawn for

    stories = _stories(session, since)
    calls = current_calls(session, [s.id for s in stories])
    for story in stories:
        visible = shown(calls.get(story.id, {}), order)
        if not visible:
            continue
        reported = first_reported(story)
        lead = visible[0].symbol
        move = _move_since(session, lead, reported)
        row = NewsRow(
            href=f"/watchlist/story/{story.id}",
            at=reported,
            headline=story.headline,
            hits=tuple(
                Hit(names.get(c.symbol, c.symbol), MARK[c.sentiment], True) for c in visible
            ),
            tag=short_state(story),
            lead_change=_pct(move) if move is not None else None,
            lead_up=move is None or move >= 0,
            lead_series=None,
        )
        rows.append(row)
        leads[id(row)] = lead

    watched = set(order)
    impacts = session.scalars(
        select(Impact)
        .where(Impact.symbol.in_(watched), Impact.created_at >= since)
        .order_by(Impact.created_at)
    ).all()
    story_ids = sorted({impact.story_id for impact in impacts})
    if story_ids:
        main = session.scalars(
            select(Story).where(Story.id.in_(story_ids)).options(selectinload(Story.impacts))
        ).all()
        for story in main:
            touched = [i for i in story.impacts if i.symbol in watched]
            if not touched:
                continue
            everything: dict[str, Impact] = {}
            for impact in story.impacts:
                everything.setdefault(impact.symbol, impact)
            hits = tuple(
                Hit(
                    names.get(s) or (assets[s].display_name if s in assets else s),
                    MARK["positive" if i.direction == "up" else "negative"],
                    s in watched,
                )
                for s, i in everything.items()
            )
            lead = touched[0]
            asset = assets.get(lead.symbol)
            change = (
                format_move(asset, lead.reference_price, lead.move_at_detection_pct)
                if asset and lead.reference_price and lead.move_at_detection_pct is not None
                else None
            )
            row = NewsRow(
                href=f"/story/{story.id}",
                at=story.first_seen_at,
                headline=story.headline,
                hits=hits,
                tag="playbook",
                lead_change=change,
                lead_up=(lead.move_at_detection_pct or 0) >= 0,
                lead_series=None,
            )
            rows.append(row)
            leads[id(row)] = lead.symbol
    rows.sort(key=lambda row: row.at, reverse=True)
    # The week behind each row's lead asset, in one read for the whole list.
    lines = series_for(session, [lead for lead in leads.values() if lead], RAIL_WINDOW, now)
    return [replace(row, lead_series=lines.get(leads.get(id(row)) or "")) for row in rows]


def recent_alerts(session: Session, now: datetime) -> list[WatchAlert]:
    """What the watchlist sent to Telegram this week, newest first."""
    return list(
        session.scalars(
            select(WatchAlert)
            .where(
                WatchAlert.created_at >= now - timedelta(days=ALERT_DAYS),
                WatchAlert.kind.in_(["news", "followup", "price", "sector", "away"]),
            )
            .order_by(WatchAlert.created_at.desc())
        )
    )


def watch_story(
    session: Session,
    story_id: int,
    watchlist: WatchlistFile,
    assets: dict[str, AssetConfig],
    settings: Settings,
    now: datetime,
) -> WatchStoryView | None:
    story = session.scalars(
        select(WatchStory)
        .where(WatchStory.id == story_id)
        .options(
            selectinload(WatchStory.articles).selectinload(WatchArticle.matches),
            selectinload(WatchStory.articles).selectinload(WatchArticle.sightings),
            selectinload(WatchStory.filings),
        )
    ).first()
    if story is None:
        return None
    order = [item.symbol for item in watchlist.watchlist]
    calls = current_calls(session, [story.id]).get(story.id, {})
    visible = shown(calls, order)
    timeline = [
        TimelineItem(
            at=article.first_seen_at,
            kind="article",
            source=article.source_name,
            detail=(
                "via Google News"
                if article.sightings and article.sightings[0].via == "google_news"
                else f"via {article.sightings[0].feed_name}"
                if article.sightings
                else ""
            ),
            text=article.title,
            url=article.url,
        )
        for article in story.articles
    ] + [
        TimelineItem(
            at=filing.filed_at,
            kind="filing",
            source=f"{filing.exchange} filing",
            detail=filing.subject,
            text=filing.description,
            url=filing.link or None,
        )
        for filing in story.filings
    ]
    timeline.sort(key=lambda item: item.at)
    symbols = [s for s in order if s in kept_symbols(story)]
    series = series_for(session, symbols, RAIL_WINDOW, now)
    reported = first_reported(story)
    prices = []
    for symbol in symbols:
        move = _move_since(session, symbol, reported)
        item = next(i for i in watchlist.watchlist if i.symbol == symbol)
        line = series.get(symbol, Series(symbol))
        if not line.points:
            line = _poll_series(session, symbol, now)
        prices.append(
            (
                symbol,
                _name(item, assets.get(symbol)),
                _pct(move) if move is not None else None,
                move is None or move >= 0,
                line,
            )
        )
    return WatchStoryView(
        story=story,
        reported=reported,
        state=story_state(story, settings),
        summary=visible[0].summary if visible else None,
        calls=visible,
        passing=[c for c in calls.values() if c.relevance == "passing"],
        timeline=timeline,
        prices=prices,
    )


# ---------------------------------------------------------------- the track record


@dataclass(frozen=True)
class WatchTable:
    title: str
    group: str
    rows: list


WATCH_GROUP_TITLES = (
    ("materiality", "Watchlist calls · by materiality"),
    ("event_type", "Watchlist calls · by event type"),
    ("first_source", "Watchlist calls · by first source"),
    ("lead", "Watchlist calls · by lead over the filing"),
)


def watch_track_tables(session: Session, horizon: int | None = None) -> list[WatchTable]:
    """The watchlist's track record, one table per grouping; each row is one benchmark, so
    the Nifty and the defence index sit side by side."""
    from app.watch.scoring import watch_track_record

    tables = []
    for group, title in WATCH_GROUP_TITLES:
        rows = watch_track_record(session, group, horizon)
        if rows:
            tables.append(WatchTable(title, group, rows))
    return tables


def benchmark_names(
    watchlist: WatchlistFile, assets: dict[str, AssetConfig], settings: Settings
) -> dict[str, str]:
    benchmark = assets.get(settings.watch.benchmark)
    names = {
        settings.watch.benchmark: benchmark.display_name if benchmark else settings.watch.benchmark
    }
    for group in watchlist.groups.values():
        if group.index:
            names[group.index] = f"{group.name} index"
    return names
