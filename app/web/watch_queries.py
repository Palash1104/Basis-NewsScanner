"""Every read the watchlist pages make: `/watchlist`, a watchlist story, and the rail's list.

The watchlist is config/watchlist.yaml, one list for stocks and commodities (user,
2026-10-07); the browser-stored list it replaced is gone. Like every page, these only read:
prices from `price_cache` (the pipeline caches 60-minute bars for the universe and the
watchlist) and the scanner's own polls (`watch_prices`), calls from `watch_calls`, and the
playbook's impacts for anything the universe also prices.

When a story happened is when it was first reported (`analyse.first_reported`), never when
BASIS first saw it (user, 2026-10-19): every "this week" here - the news list, the card notes,
a stock's Latest and Headlines - is about when the news came out. `first_seen_at` keeps its
own jobs: what a scan found that was new, lead times, scoring's reference.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

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
    WatchFiling,
    WatchMatch,
    WatchPrice,
    WatchStory,
)
from app.pipeline.prices import INTRADAY, format_move, move_pct
from app.watch.alerts import MARK, current_calls, shown, story_state
from app.watch.analyse import first_reported, kept_symbols, stale_after
from app.watch.render import adds_information, short_headline
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
    driver: "Driver | None"  # what last moved it: a story's headline and why
    href: str | None  # the asset page, for anything the universe prices
    stock: bool


@dataclass(frozen=True)
class Driver:
    """The note under a card (the user's design, 2026-10-19): the story's headline in bold,
    the call's reason or the playbook's mechanism under it, and where the story is."""

    title: str
    detail: str | None
    tone: str  # up | down | flat
    href: str | None


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
    session: Session, watchlist: WatchlistFile, now: datetime, max_age: timedelta
) -> dict[str, Driver]:
    """What last moved each entry. For a stock: among this week's stories with a real call on
    it (never a passing mention), the one first reported most recently - not the newest call,
    which can be on old news a search has just turned up. For a commodity: its latest playbook
    mechanism. The headline is the story's, without the company's name when it opens with it;
    the reason shows under it only when it adds something."""
    since = now - timedelta(days=DRIVER_DAYS)
    found: dict[str, Driver] = {}
    names = {
        item.symbol: [n for n in (item.name, item.nse_name, *item.aliases.strong) if n]
        for item in watchlist.stocks
    }
    stories = _reported_since(session, since, max_age)
    calls = current_calls(session, [story.id for story, _ in stories])
    best: dict[str, tuple[datetime, int, WatchStory, WatchCall]] = {}
    for story, reported in stories:
        for symbol, call in calls.get(story.id, {}).items():
            if symbol not in names or call.relevance == "passing":
                continue
            key = (reported, call.id)
            if symbol not in best or key > best[symbol][:2]:
                best[symbol] = (reported, call.id, story, call)
    tones = {"positive": "up", "negative": "down"}
    for symbol, (_, _, story, call) in best.items():
        headline = story.headline or ""
        title = short_headline(headline, names.get(symbol, [])) if headline else call.reason
        detail = call.reason if headline and adds_information(call.reason, headline) else None
        found[symbol] = Driver(
            title, detail, tones.get(call.sentiment, "flat"), f"/watchlist/story/{story.id}"
        )
    others = [i.symbol for i in watchlist.watchlist if i.symbol not in found]
    impacts = list(
        session.scalars(
            select(Impact)
            .where(Impact.symbol.in_(others), Impact.created_at >= since)
            .order_by(Impact.created_at, Impact.id)
        )
    )
    titles = dict(
        session.execute(
            select(Story.id, Story.headline).where(Story.id.in_({i.story_id for i in impacts}))
        ).all()
    )
    for impact in impacts:
        title = titles.get(impact.story_id)
        found[impact.symbol] = Driver(
            title or impact.mechanism,
            impact.mechanism if title else None,
            "up" if impact.direction == "up" else "down",
            f"/story/{impact.story_id}",
        )
    return found


def exchange_of(symbol: str) -> str | None:
    """The exchange a watchlist stock's Yahoo ticker trades on."""
    if symbol.endswith(".NS"):
        return "NSE"
    if symbol.endswith(".BO"):
        return "BSE"
    return None


def _kicker(item: WatchItem, watchlist: WatchlistFile, asset: AssetConfig | None) -> str:
    if item.type == "stock":
        group = watchlist.groups.get(item.group or "")
        exchange = exchange_of(item.symbol)
        return " · ".join(part for part in (exchange, group.name if group else None) if part)
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
    scanner's own poll today for a stock, else the 24-hour move from the cache, else - with
    its market shut for longer than that, as NSE is all weekend - its last session's move
    (2026-10-17: every NSE card read "no price yet" on a Saturday evening)."""
    symbols = [item.symbol for item in watchlist.watchlist]
    moves = window_moves(session, now, MOVER_HOURS)
    series = series_for(session, symbols, RAIL_WINDOW, now)
    live = _live(session, symbols, now, settings)
    drivers = _drivers(session, watchlist, now, stale_after(settings))
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
        elif last := _last_session(session, item.symbol, _zone(asset, settings)):
            reference, close = last
            pct = move_pct(reference, close)
            price = format_price(asset, close) if asset else _inr(close)
            change = format_move(asset, reference, pct) if asset else _pct(pct / 100)
            label, up = "last", pct >= 0
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
                driver=driver,
                href=f"/asset/{item.symbol}" if asset else None,
                stock=item.type == "stock",
            )
        )
    return cards


def _zone(asset: AssetConfig | None, settings: Settings) -> ZoneInfo:
    """The exchange's time zone: the asset's own, else the app's (IST, for an NSE stock)."""
    return ZoneInfo(asset.timezone) if asset and asset.timezone else settings.tz


def _last_session(session: Session, symbol: str, zone: ZoneInfo) -> tuple[float, float] | None:
    """(previous session's last close, last close) from the cached hourly bars, sessions
    dated in the exchange's own time zone; None without two sessions of bars."""
    bars = session.execute(
        select(PriceBar.ts, PriceBar.close)
        .where(PriceBar.symbol == symbol, PriceBar.interval == INTRADAY)
        .order_by(PriceBar.ts.desc())
        .limit(60)
    ).all()
    if not bars:
        return None
    latest_at, latest = bars[0]
    day = latest_at.astimezone(zone).date()
    for at, close in bars[1:]:
        if at.astimezone(zone).date() < day:
            return close, latest
    return None


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


def _reported_since(
    session: Session, since: datetime, max_age: timedelta
) -> list[tuple[WatchStory, datetime]]:
    """Stories first *reported* since `since`, each with that time. A story is first seen no
    earlier than it is first reported, so those first seen since then are the ones to read;
    one first seen today from an August article is left out."""
    found = []
    for story in _stories(session, since):
        reported = first_reported(story, max_age)
        if reported >= since:
            found.append((story, reported))
    return found


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

    stories = _reported_since(session, since, stale_after(settings))
    calls = current_calls(session, [s.id for s, _ in stories])
    for story, reported in stories:
        visible = shown(calls.get(story.id, {}), order)
        if not visible:
            continue
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
    reported = first_reported(story, stale_after(settings))
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


# ---------------------------------------------------------------- one stock's dialog
# Opening a stock's card scans it (app/watch/ondemand.py); the dialog shows what is stored
# about it, read here like every other page.

STOCK_DAYS = 7  # the dialog's Latest and Headlines reach back this far
FILING_DAYS = 30
FILING_ROWS = 60


@dataclass(frozen=True)
class LatestRow:
    story_id: int
    headline: str
    reported: datetime
    state: str
    sources: tuple[str, ...]
    call: WatchCall | None
    new: bool


@dataclass(frozen=True)
class StockHeadline:
    verdict: str
    title: str
    source: str
    published: datetime
    where: str  # "Google News" | "Market feeds", for the source filter
    reason: str
    alias: str | None
    url: str


def stock_card(
    session: Session,
    watchlist: WatchlistFile,
    symbol: str,
    assets: dict[str, AssetConfig],
    settings: Settings,
    now: datetime,
) -> WatchCard | None:
    """One card, priced exactly as the page prices it."""
    entries = [item for item in watchlist.watchlist if item.symbol == symbol]
    if not entries:
        return None
    one = watchlist.model_copy(update={"watchlist": entries})
    return watch_cards(session, one, assets, settings, now)[0]


def stock_latest(
    session: Session,
    symbol: str,
    now: datetime,
    new_since: datetime | None,
    settings: Settings,
) -> list[LatestRow]:
    """The stories about one stock first reported this week - kept for it, never a passing
    mention - each with its call for that stock, newest report first. "New" is what was stored
    since `new_since` (its previous scan) *and* first reported within the scan's own reach
    (`watch.on_demand_days`): an older report a search has only now turned up is not news."""
    max_age = stale_after(settings)
    fresh = now - timedelta(days=settings.watch.on_demand_days)
    stories = [
        (story, reported)
        for story, reported in _reported_since(session, now - timedelta(days=STOCK_DAYS), max_age)
        if symbol in kept_symbols(story)
    ]
    calls = current_calls(session, [s.id for s, _ in stories])
    rows = []
    for story, reported in stories:
        call = calls.get(story.id, {}).get(symbol)
        if call is not None and call.relevance == "passing":
            continue
        sources = tuple(
            dict.fromkeys(
                a.source_name for a in sorted(story.articles, key=lambda a: a.first_seen_at)
            )
        ) + tuple(dict.fromkeys(f"{f.exchange} filing" for f in story.filings))
        rows.append(
            LatestRow(
                story_id=story.id,
                headline=story.headline,
                reported=reported,
                state=short_state(story),
                sources=sources,
                call=call,
                new=new_since is not None
                and story.first_seen_at >= new_since
                and reported >= fresh,
            )
        )
    rows.sort(key=lambda row: row.reported, reverse=True)
    return rows


def stock_headlines(
    session: Session, symbol: str, now: datetime
) -> tuple[list[StockHeadline], dict[str, int]]:
    """Every stored headline published this week that named the stock - keep, mention or
    drop - newest first, with how many of each. By publication, not by when BASIS stored it:
    an old page a search turned up is not this week's headline."""
    matches = session.execute(
        select(WatchMatch, WatchArticle)
        .join(WatchArticle, WatchMatch.article_id == WatchArticle.id)
        .where(
            WatchMatch.symbol == symbol,
            WatchArticle.published_at >= now - timedelta(days=STOCK_DAYS),
        )
        .options(selectinload(WatchArticle.sightings))
        .order_by(WatchArticle.published_at.desc())
    ).all()
    rows = []
    counts = {"keep": 0, "mention": 0, "drop": 0}
    for match, article in matches:
        counts[match.verdict] = counts.get(match.verdict, 0) + 1
        google = any(s.via == "google_news" for s in article.sightings)
        rows.append(
            StockHeadline(
                verdict=match.verdict,
                title=article.title,
                source=article.source_name,
                published=article.published_at,
                where="Google News" if google else "Market feeds",
                reason=match.reason,
                alias=match.alias,
                url=article.url,
            )
        )
    return rows, counts


def stock_filings(session: Session, symbol: str, now: datetime) -> list[WatchFiling]:
    """NSE and BSE filings by the company over the last month, newest first."""
    return list(
        session.scalars(
            select(WatchFiling)
            .where(
                WatchFiling.symbol == symbol,
                WatchFiling.filed_at >= now - timedelta(days=FILING_DAYS),
            )
            .order_by(WatchFiling.filed_at.desc())
            .limit(FILING_ROWS)
        )
    )
