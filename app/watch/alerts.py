"""Watchlist alerts and the digest's watchlist section (step 4).

What reaches Telegram, and when:

  news       a story the call rates high for any watchlist company it is about: one alert
             for the story, listing every company (user, 2026-10-08), as soon as it is called.
             A story BASIS already had alerts too when news that just came out raises a stock
             to high; the alert leads with that news, not the story's old headline (`new_news`)
  followup   once, when NSE asks the company to clarify, the company replies, or a filing
             attaches to a story already alerted - after the reply has been read and the
             story called again, or after FOLLOWUP_WAIT
  price      "moved, no story yet" (app/watch/moves.py): at most once per stock per day
  sector     one "defence sector move" when the peers moved together, instead of one each
  away       after a catch-up: ONE summary of what was first reported more than an hour
             before BASIS saw it, newest first, each with its original time, and any part of
             the gap no source covered. Fresher items are alerted as usual. Filings that
             reach BASIS late outside a catch-up (the PEAD tool backfilling BSE after it was
             started) get one too, from their `late` run, and never an alert of their own.
             A late filing on a story already alerted is shown as an update to it:
             "Update: <the story> (alerted 10:23)", with the filing under it.

Feed health is not sent (user, 2026-10-16: one notification per feed after every night off
was noise, and wrong): it is a section of /runs, app/feed_health.py.

Every alert is a watch_alerts row whose key makes it happen once. It is written before it is
sent; a failed send is retried for half an hour, three times at most.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.config import Settings, WatchlistFile
from app.delivery.format import TELEGRAM_LIMIT, telegram_length
from app.models import (
    WatchAlert,
    WatchArticle,
    WatchCall,
    WatchFiling,
    WatchPrice,
    WatchRun,
    WatchStory,
)
from app.schedule import start_of_news_day
from app.watch.analyse import (
    first_reported,
    kept_articles,
    kept_symbols,
    open_since,
    reporting_articles,
    stale_after,
    trigger_for,
)
from app.watch.moves import MoveEvent, Snapshot, Typical, detect
from app.watch.render import (
    MARK,
    Sections,
    low_item,
    move_lines,
    section_for,
    story_item,
    time_range,
)

log = logging.getLogger(__name__)

RANK = {"high": 0, "medium": 1, "low": 2}
FOLLOWUP_WAIT = timedelta(minutes=15)
AWAY_WAIT = timedelta(minutes=10)
# An instant alert is for news: one first reported longer ago than this goes to the digest
# and the page instead. Running, BASIS sees a story within minutes of it breaking.
NEWS_MAX_AGE = timedelta(hours=6)
RETRY_FOR = timedelta(minutes=30)
MAX_ATTEMPTS = 3
SOURCES_SHOWN = 3
DIGEST_STORIES = 8

# Runs whose stories get a "while you were away" summary: catch-ups, and passes that read
# filings late (app/watch/scan.py `_record_late`).
SUMMARISED_JOBS = ("catchup", "late")

Sender = Callable[[list[str]], None]
# Between the messages of one alert in watch_alerts.text: never in a message itself.
MESSAGE_SEPARATOR = "\x1e"


@dataclass(frozen=True)
class Outgoing:
    kind: str
    key: str
    messages: tuple[str, ...]
    story_id: int | None = None
    symbols: tuple[str, ...] = ()


@dataclass
class AlertResult:
    sent: list[str] = field(default_factory=list)  # keys
    failed: list[tuple[str, str]] = field(default_factory=list)
    marked: list[str] = field(default_factory=list)  # summaries with nothing to say


def _t(value: str) -> str:
    return escape(value, quote=False)


def _local(moment: datetime, settings: Settings, ref: datetime | None = None) -> str:
    local = moment.astimezone(settings.tz)
    if ref is not None and local.date() == ref.astimezone(settings.tz).date():
        return f"{local:%H:%M}"
    return f"{local:%a %d %b %H:%M}"


# ---------------------------------------------------------------- what a story says


def arrived_late(filing: WatchFiling, away: timedelta) -> bool:
    """A filing BASIS first saw more than `away` after it was filed: read in a catch-up, or
    backfilled by the PEAD tool after it was started. It belongs in a "while you were away"
    summary, never an alert of its own."""
    return filing.first_seen_at - filing.filed_at > away


def story_state(story: WatchStory, settings: Settings) -> str:
    """Media report, not yet filed -> NSE asked -> the company responded / filed."""
    by_kind: dict[str, list[WatchFiling]] = {}
    for filing in sorted(story.filings, key=lambda f: f.filed_at):
        by_kind.setdefault(filing.kind, []).append(filing)
    if replies := by_kind.get("company_reply"):
        return f"the company responded to NSE at {_local(replies[-1].filed_at, settings)}"
    if asked := by_kind.get("clarification_sought"):
        return f"NSE asked the company to clarify at {_local(asked[0].filed_at, settings)}"
    if filed := by_kind.get("filing"):
        first = filed[0]
        verb = (
            f"filed with {first.exchange}" if kept_articles(story) else f"a {first.exchange} filing"
        )
        return f"{verb}: {first.subject} ({_local(first.filed_at, settings)})"
    return "media report, not yet filed"


def current_calls(session: Session, story_ids: Sequence[int]) -> dict[int, dict[str, WatchCall]]:
    """Each story's latest call per company."""
    found: dict[int, dict[str, WatchCall]] = {}
    for call in session.scalars(
        select(WatchCall)
        .where(WatchCall.story_id.in_(story_ids))
        .order_by(WatchCall.created_at, WatchCall.id)
    ):
        found.setdefault(call.story_id, {})[call.symbol] = call
    return found


def shown(calls: Mapping[str, WatchCall], order: Sequence[str]) -> list[WatchCall]:
    """The calls worth showing - never a passing mention - most material first."""
    position = {symbol: n for n, symbol in enumerate(order)}
    return sorted(
        (c for c in calls.values() if c.relevance != "passing"),
        key=lambda c: (RANK[c.materiality], position.get(c.symbol, 99)),
    )


def _marks(calls: Sequence[WatchCall], names: Mapping[str, str]) -> str:
    return " · ".join(
        f"{MARK[c.sentiment]} {_t(names.get(c.symbol, c.symbol))} ({c.materiality})" for c in calls
    )


def _sources(articles: Sequence[WatchArticle]) -> str:
    links, seen = [], set()
    for article in sorted(articles, key=lambda a: a.first_seen_at):
        if article.source_name in seen:
            continue
        seen.add(article.source_name)
        links.append(f'<a href="{escape(article.url, quote=True)}">{_t(article.source_name)}</a>')
        if len(links) == SOURCES_SHOWN:
            break
    return " · ".join(links)


def news_message(
    story: WatchStory,
    calls: Sequence[WatchCall],
    names: Mapping[str, str],
    settings: Settings,
    now: datetime | None = None,
    news: "NewNews | None" = None,
) -> str:
    """An instant alert: the item as everywhere else (app/watch/render.py), then the
    summary, each company's call and the sources folded underneath. `news`: the news that
    came into a story BASIS already had, which the item then leads with."""
    ref = now or story.first_seen_at
    details = [_t(calls[0].summary)] if calls else []
    details += [
        f"{MARK[c.sentiment]} {_t(names.get(c.symbol, c.symbol))} · {c.materiality} · "
        f"{c.event_type.replace('_', ' ')} — {_t(c.reason)}"
        for c in calls
    ]
    if sources := _sources(news.articles if news else kept_articles(story)):
        details.append(f"Sources: {sources}")
    level = calls[0].materiality.upper() if calls else ""
    items = (news.articles, news.filings) if news else None
    return "\n".join(
        [
            f"<b>BASIS · watchlist alert{' · ' + level if level else ''}</b>",
            story_item(story, calls, names, settings, ref, items=items),
            "<blockquote expandable>" + "\n".join(details) + "</blockquote>",
        ]
    )


# ---------------------------------------------------------------- news in a story BASIS had
# A story stays open while news keeps joining it (scan._window_members), so a call can rise
# on news that came out days after the story began: Astra Microwave's Q1 results (10 Oct, rated
# high) inside a 9 Oct multibagger list, Zen's Cogknit acquisition (17 Oct) inside a 13 Oct
# share-price note. Judged by when the story began, neither was alerted nor in a digest. The
# alert, the digest and the away summary judge a story by the news behind its call instead
# (user, 2026-10-19).


@dataclass(frozen=True)
class NewNews:
    """The news behind a call that rated a stock higher than before."""

    articles: tuple[WatchArticle, ...]  # those that came out in the period asked about
    filings: tuple[WatchFiling, ...]
    raised: tuple[WatchCall, ...]  # the calls that rose, most material first
    when: datetime  # when the earliest of them came out


def came_out(item: WatchArticle | WatchFiling) -> datetime:
    """When the publisher or the exchange put it out (never later than BASIS saw it)."""
    if isinstance(item, WatchFiling):
        return item.filed_at
    return min(item.published_at, item.first_seen_at)


def call_history(session: Session, story_ids: Sequence[int]) -> dict[int, list[WatchCall]]:
    """Every call on these stories, oldest first."""
    found: dict[int, list[WatchCall]] = {}
    if not story_ids:
        return found
    for call in session.scalars(
        select(WatchCall)
        .where(WatchCall.story_id.in_(list(story_ids)))
        .order_by(WatchCall.created_at, WatchCall.id)
    ):
        found.setdefault(call.story_id, []).append(call)
    return found


def raised_calls(history: Sequence[WatchCall], since: datetime) -> list[WatchCall]:
    """Each stock's latest call made since `since`, when it rates the stock above every call
    on it before then (a first call always does) and isn't a passing mention. Most material
    first."""
    before: dict[str, int] = {}
    latest: dict[str, WatchCall] = {}
    for call in sorted(history, key=lambda c: (c.created_at, c.id)):
        if call.created_at >= since:
            latest[call.symbol] = call
        elif call.relevance != "passing":
            before[call.symbol] = min(before.get(call.symbol, len(RANK)), RANK[call.materiality])
    raised = [
        call
        for call in latest.values()
        if call.relevance != "passing"
        and RANK[call.materiality] < before.get(call.symbol, len(RANK))
    ]
    return sorted(raised, key=lambda c: (RANK[c.materiality], c.symbol))


def new_news(
    story: WatchStory,
    history: Sequence[WatchCall],
    called_since: datetime,
    period: tuple[datetime, datetime | None],
    max_age: timedelta,
    skip: Callable[[WatchArticle | WatchFiling], bool] | None = None,
) -> NewNews | None:
    """The news behind a call made since `called_since` that rates a stock higher than any
    call before then, when it came out in `period` ([start, end), end None for open); None
    otherwise. The news is the kept articles and filings that arrived since the story's
    previous call - all of them for its first.

    A re-call needs one of them to have come out in the period. A story's first call needs
    its earliest to: the old rule for a new story, "first reported in the last 6 hours". `skip`
    leaves items out - one read in a catch-up long after it came out, which the away summary
    has (for a first call, the story is left out when its earliest is such an item)."""
    raised = raised_calls(history, called_since)
    if not raised:
        return None
    previous = max((c.created_at for c in history if c.created_at < called_since), default=None)
    latest = max(c.created_at for c in history)
    items: list[WatchArticle | WatchFiling] = [
        item
        for item in [*reporting_articles(story, max_age), *story.filings]
        if (previous is None or item.first_seen_at > previous) and item.first_seen_at <= latest
    ]
    if not items:
        return None
    start, end = period

    def inside(item: WatchArticle | WatchFiling) -> bool:
        return start <= came_out(item) and (end is None or came_out(item) < end)

    if previous is None:
        earliest = min(items, key=came_out)
        if not inside(earliest) or (skip is not None and skip(earliest)):
            return None
    fresh = [i for i in items if inside(i) and (skip is None or not skip(i))]
    if not fresh:
        return None
    return NewNews(
        articles=tuple(i for i in fresh if isinstance(i, WatchArticle)),
        filings=tuple(i for i in fresh if isinstance(i, WatchFiling)),
        raised=tuple(raised),
        when=min(came_out(i) for i in fresh),
    )


def _called_since(session: Session, since: datetime) -> list[int]:
    return sorted(
        set(session.scalars(select(WatchCall.story_id).where(WatchCall.created_at >= since)))
    )


# ---------------------------------------------------------------- deciding what to send


def _sent_keys(session: Session, keys: Sequence[str]) -> set[str]:
    if not keys:
        return set()
    return set(session.scalars(select(WatchAlert.key).where(WatchAlert.key.in_(keys))))


def _catch_up_passes(session: Session, since: datetime) -> list[WatchRun]:
    return list(
        session.scalars(
            select(WatchRun).where(WatchRun.job.in_(SUMMARISED_JOBS), WatchRun.started_at >= since)
        )
    )


def _load_stories(
    session: Session, since: datetime | None = None, ids: Sequence[int] | None = None
) -> list[WatchStory]:
    """Stories first seen since `since`, or the stories `ids`, with what a message needs."""
    statement = select(WatchStory).options(
        selectinload(WatchStory.articles).selectinload(WatchArticle.matches),
        selectinload(WatchStory.articles).selectinload(WatchArticle.sightings),
        selectinload(WatchStory.filings),
    )
    if since is not None:
        statement = statement.where(WatchStory.first_seen_at >= since)
    if ids is not None:
        statement = statement.where(WatchStory.id.in_(list(ids)))
    return list(session.scalars(statement))


def news_alerts(
    session: Session, settings: Settings, names: Mapping[str, str], now: datetime
) -> list[Outgoing]:
    """One alert per story, when a call made in the last NEWS_MAX_AGE raises a stock to high
    on news that came out in that time (`new_news`): a new story's news first reported then,
    or news that came into a story BASIS already had. Never news read in a catch-up long after
    it came out, nor a filing that arrived late: a "while you were away" summary has those."""
    since = now - NEWS_MAX_AGE
    max_age = stale_after(settings)
    stories = _load_stories(session, ids=_called_since(session, since))
    calls = current_calls(session, [s.id for s in stories])
    history = call_history(session, [s.id for s in stories])
    already = _sent_keys(session, [f"news:{s.id}" for s in stories])
    away = timedelta(minutes=settings.watch.away_after_minutes)
    backlog = {run.started_at for run in _catch_up_passes(session, since)}

    def read_late(item: WatchArticle | WatchFiling) -> bool:
        if isinstance(item, WatchFiling):
            return arrived_late(item, away)
        return item.first_seen_at in backlog and came_out(item) < item.first_seen_at - away

    out: list[Outgoing] = []
    for story in sorted(stories, key=lambda s: s.first_seen_at):
        key = f"news:{story.id}"
        if key in already:
            continue
        news = new_news(story, history.get(story.id, []), since, (since, None), max_age, read_late)
        if news is None or news.raised[0].materiality != "high":
            continue
        visible = shown(calls.get(story.id, {}), list(names))
        # A story that began before the window is led by the news that just came out.
        older = first_reported(story, max_age) < since
        out.append(
            Outgoing(
                "news",
                key,
                (news_message(story, visible, names, settings, now, news if older else None),),
                story.id,
                tuple(c.symbol for c in visible),
            )
        )
    return out


def followups(
    session: Session, settings: Settings, names: Mapping[str, str], now: datetime
) -> list[Outgoing]:
    """One update per alerted story, when a filing arrives after the alert."""
    alerted = {
        alert.story_id: alert
        for alert in session.scalars(
            select(WatchAlert).where(WatchAlert.kind == "news", WatchAlert.sent_at.is_not(None))
        )
        if alert.story_id is not None
    }
    if not alerted:
        return []
    done = _sent_keys(session, [f"followup:{sid}" for sid in alerted])
    stories = session.scalars(
        select(WatchStory)
        .where(WatchStory.id.in_(list(alerted)))
        .options(
            selectinload(WatchStory.articles).selectinload(WatchArticle.matches),
            selectinload(WatchStory.filings),
        )
    ).all()
    calls = current_calls(session, [s.id for s in stories])
    away = timedelta(minutes=settings.watch.away_after_minutes)
    out: list[Outgoing] = []
    for story in stories:
        alert = alerted[story.id]
        key = f"followup:{story.id}"
        # A filing that arrived late is in a "while you were away" summary instead.
        new = [
            f
            for f in story.filings
            if f.first_seen_at > alert.created_at and not arrived_late(f, away)
        ]
        if key in done or not new:
            continue
        latest_filing = max(new, key=lambda f: f.first_seen_at)
        waited = now - latest_filing.first_seen_at >= FOLLOWUP_WAIT
        reply_unread = any(
            f.kind == "company_reply" and f.link and f.reply_text is None for f in new
        )
        story_calls = calls.get(story.id, {})
        recalled = any(c.created_at >= latest_filing.first_seen_at for c in story_calls.values())
        if not waited and (reply_unread or not recalled):
            continue  # wait for the reply to be read and the story called again
        visible = shown(story_calls, list(names))
        lines = [
            "<b>BASIS · watchlist update</b>",
            story_item(story, visible if recalled else [], names, settings, now, reason=False),
        ]
        lines += [
            _filing_line(filing, settings, now) for filing in sorted(new, key=lambda f: f.filed_at)
        ]
        if visible and recalled:
            lines.append(f"{_marks(visible, names)} — {_t(visible[0].reason)}")
        out.append(
            Outgoing(
                "followup", key, ("\n".join(lines),), story.id, tuple(c.symbol for c in visible)
            )
        )
    return out


def move_message(
    event: MoveEvent, names: Mapping[str, str], settings: Settings, now: datetime | None = None
) -> str:
    """ "Moved, no story yet": a header, then the move as it reads everywhere else. The
    digest's watchlist section reuses the lines under the header."""
    title = (
        f"BASIS · {(event.group or 'sector').lower()} sector move"
        if event.kind == "sector"
        else "BASIS · moved, no story yet"
    )
    return "\n".join([f"<b>{title}</b>", *move_lines(event, names, settings, now or event.at)])


def move_alerts(
    events: Sequence[MoveEvent], session: Session, names: Mapping[str, str], settings: Settings
) -> list[Outgoing]:
    already = _sent_keys(session, [e.key for e in events])
    seen: set[str] = set()
    out = []
    for event in events:
        if event.key in already or event.key in seen:
            continue
        seen.add(event.key)
        symbols = (event.symbol,) if event.symbol else ()
        out.append(
            Outgoing(event.kind, event.key, (move_message(event, names, settings),), None, symbols)
        )
    return out


# ---------------------------------------------------------------- prices -> moves


def latest_snapshots(
    session: Session, symbols: Sequence[str], now: datetime, settings: Settings
) -> dict[str, Snapshot]:
    """Each symbol's latest poll today (this session), if it has a previous close."""
    midnight = datetime.combine(now.astimezone(settings.tz).date(), time(0), tzinfo=settings.tz)
    found: dict[str, Snapshot] = {}
    for row in session.scalars(
        select(WatchPrice)
        .where(
            WatchPrice.symbol.in_(symbols),
            WatchPrice.polled_at >= midnight,
            WatchPrice.price.is_not(None),
        )
        .order_by(WatchPrice.polled_at)
    ):
        if row.previous_close:
            found[row.symbol] = Snapshot(
                row.symbol, row.polled_at, row.price / row.previous_close - 1
            )  # type: ignore[operator]
    return found


def news_since(
    session: Session, since: datetime, max_age: timedelta
) -> list[tuple[str, datetime, datetime]]:
    """(stock, when it came out, when BASIS first saw it) for every kept article and filing
    first reported since `since`, in any story: one begun today, or one BASIS already had that
    the news joined (user, 2026-10-19). The story counts for all its stocks. An old page
    stored today is never news, so it can't silence a real move."""
    found: list[tuple[str, datetime, datetime]] = []
    stories = session.scalars(
        select(WatchStory)
        .where(open_since(since))  # an item reported since then was first seen since then
        .options(
            selectinload(WatchStory.articles).selectinload(WatchArticle.matches),
            selectinload(WatchStory.filings),
        )
    )
    for story in stories:
        symbols = kept_symbols(story)
        for item in [*reporting_articles(story, max_age), *story.filings]:
            if came_out(item) >= since:
                found += [(symbol, came_out(item), item.first_seen_at) for symbol in symbols]
    return found


def stories_since(
    session: Session, symbols: Sequence[str], since: datetime, max_age: timedelta
) -> dict[str, bool]:
    """Which stocks have a story since `since`: one with any item (a kept article, a filing)
    first reported since then - not only a story that began then. News that joins an older
    story silences "moved, no story yet" as much as a new story does."""
    found = dict.fromkeys(symbols, False)
    for symbol, _, _ in news_since(session, since, max_age):
        if symbol in found:
            found[symbol] = True
    return found


def last_session_close(now: datetime, settings: Settings) -> datetime:
    """The most recent weekday market close before now - "since the last close"."""
    local = now.astimezone(settings.tz)
    hours, minutes = (int(x) for x in settings.watch.market_close.split(":"))
    day = local.date()
    close = datetime.combine(day, time(hours, minutes), tzinfo=settings.tz)
    while close > local or close.weekday() >= 5:
        day -= timedelta(days=1)
        close = datetime.combine(day, time(hours, minutes), tzinfo=settings.tz)
    return close


def detect_now(
    session: Session,
    watchlist: WatchlistFile,
    typical: Mapping[str, Typical],
    settings: Settings,
    now: datetime,
) -> list[MoveEvent]:
    groups, ungrouped = group_layout(watchlist)
    symbols = [s.symbol for s in watchlist.stocks] + [i for i, _ in groups.values() if i]
    snaps = latest_snapshots(session, symbols, now, settings)
    fresh = {s: snap for s, snap in snaps.items() if now - snap.at <= timedelta(minutes=15)}
    has_story = stories_since(
        session,
        [s.symbol for s in watchlist.stocks],
        last_session_close(now, settings),
        stale_after(settings),
    )
    return detect(fresh, typical, groups, ungrouped, has_story, now.astimezone(settings.tz).date())


def group_layout(
    watchlist: WatchlistFile,
) -> tuple[dict[str, tuple[str | None, list[str]]], list[str]]:
    groups: dict[str, tuple[str | None, list[str]]] = {}
    ungrouped: list[str] = []
    for stock in watchlist.stocks:
        if stock.group and stock.group in watchlist.groups:
            index = watchlist.groups[stock.group].index
            groups.setdefault(stock.group, (index, []))[1].append(stock.symbol)
        else:
            ungrouped.append(stock.symbol)
    return groups, ungrouped


def moves_in_period(
    session: Session,
    watchlist: WatchlistFile,
    typical: Mapping[str, Typical],
    settings: Settings,
    start: datetime,
    end: datetime,
) -> list[MoveEvent]:
    """Replay the price rows of a period (the catch-up's backfill) through the same rules,
    moment by moment, with only the news BASIS had by each moment (`news_since`: an item
    reported since that moment's last close and first seen by then); the first time each key
    fires is its event."""
    groups, ungrouped = group_layout(watchlist)
    stocks = [s.symbol for s in watchlist.stocks]
    symbols = stocks + [i for i, _ in groups.values() if i]
    rows = session.scalars(
        select(WatchPrice)
        .where(
            WatchPrice.symbol.in_(symbols),
            WatchPrice.polled_at >= start,
            WatchPrice.polled_at <= end,
            WatchPrice.price.is_not(None),
        )
        .order_by(WatchPrice.polled_at)
    ).all()
    by_moment: dict[datetime, dict[str, Snapshot]] = {}
    for row in rows:
        if row.previous_close:
            moment = row.polled_at.replace(second=0, microsecond=0)
            by_moment.setdefault(moment, {})[row.symbol] = Snapshot(
                row.symbol,
                row.polled_at,
                row.price / row.previous_close - 1,  # type: ignore[operator]
            )
    if not by_moment:
        return []
    earliest = last_session_close(min(by_moment), settings)
    news = news_since(session, earliest, stale_after(settings))
    events: dict[str, MoveEvent] = {}
    latest: dict[str, Snapshot] = {}
    for moment in sorted(by_moment):
        latest.update(by_moment[moment])
        day = moment.astimezone(settings.tz).date()
        current = {
            s: snap for s, snap in latest.items() if snap.at.astimezone(settings.tz).date() == day
        }
        close = last_session_close(moment, settings)
        has_story = {
            symbol: any(s == symbol and out >= close and seen <= moment for s, out, seen in news)
            for symbol in stocks
        }
        for event in detect(current, typical, groups, ungrouped, has_story, day):
            events.setdefault(event.key, event)
    return sorted(events.values(), key=lambda e: e.at)


# ---------------------------------------------------------------- while you were away


def away_summaries(
    session: Session,
    watchlist: WatchlistFile,
    typical: Mapping[str, Typical] | None,
    settings: Settings,
    now: datetime,
) -> tuple[list[Outgoing], list[WatchRun]]:
    """Summaries for finished catch-ups, once their stories have been called (or after
    AWAY_WAIT). Returns (summaries, catch-ups with nothing to report)."""
    names = {s.symbol: s.name or s.symbol for s in watchlist.stocks}
    out: list[Outgoing] = []
    empty: list[WatchRun] = []
    away = timedelta(minutes=settings.watch.away_after_minutes)
    for run in session.scalars(
        select(WatchRun).where(WatchRun.job.in_(SUMMARISED_JOBS)).order_by(WatchRun.started_at)
    ):
        details = run.details or {}
        if details.get("summary_sent"):
            continue
        gap_start = datetime.fromisoformat(details["gap"]["start"])
        gap_end = datetime.fromisoformat(details["gap"]["end"])
        cutoff = run.started_at - away
        # What this pass read: the stories it started, and any story that gained a filing
        # it read late - whenever that was filed, even before the gap began (the PEAD tool
        # backfilling BSE after a restart).
        late_story_ids = set(
            session.scalars(
                select(WatchFiling.story_id).where(
                    WatchFiling.first_seen_at == run.started_at,
                    WatchFiling.filed_at < cutoff,
                    WatchFiling.story_id.is_not(None),
                )
            )
        )
        stories = [
            s
            for s in _load_stories(session, run.started_at - timedelta(seconds=1))
            if s.first_seen_at == run.started_at and kept_symbols(s)
        ]
        # And stories BASIS already had that this pass added articles to: listed when a call
        # made on them rates a stock higher than before (`new_news`, user, 2026-10-19).
        grown_ids = set(
            session.scalars(
                select(WatchArticle.story_id).where(
                    WatchArticle.first_seen_at == run.started_at,
                    WatchArticle.story_id.is_not(None),
                )
            )
        )
        if missing := (late_story_ids | grown_ids) - {s.id for s in stories}:
            stories += _load_stories(session, ids=sorted(missing))
        calls = current_calls(session, [s.id for s in stories])
        history = call_history(session, [s.id for s in stories])
        alerted = news_alert_times(session, [s.id for s in stories], run.started_at)
        unfinished = [s for s in stories if trigger_for(s, list(calls.get(s.id, {}).values()))]
        finished_at = run.finished_at or run.started_at
        if unfinished and now - finished_at < AWAY_WAIT:
            continue  # its stories are still being called
        sections = Sections.empty()
        for story in stories:
            reported = first_reported(story, stale_after(settings))
            late = [
                f
                for f in story.filings
                if f.first_seen_at == run.started_at and f.filed_at < cutoff
            ]
            story_calls = calls.get(story.id, {})
            if late and story.id in alerted:
                # The reader has this story already: the filing is an update to it.
                when = max(f.filed_at for f in late)
                text = update_item(
                    story, late, story_calls, alerted[story.id], names, settings, gap_end
                )
                sections.add("high", when, text)  # only a high story was alerted
                continue
            if story.first_seen_at < run.started_at and not late:
                # News this pass read into a story BASIS already had, under its own headline.
                started = run.started_at
                news = new_news(
                    story,
                    history.get(story.id, []),
                    started,
                    (gap_start, cutoff),
                    stale_after(settings),
                    lambda item, started=started: item.first_seen_at != started,  # type: ignore[misc]
                )
                if news is None:
                    continue
                visible = shown(story_calls, list(names))
                items = (news.articles, news.filings)
                section = section_for(list(news.raised))
                if section == "low":
                    text = low_item(
                        story, visible, names, settings, gap_end, news.when, items=items
                    )
                else:
                    text = story_item(story, visible, names, settings, gap_end, items=items)
                sections.add(section, news.when, text)
                continue
            if story.first_seen_at == run.started_at and (gap_start <= reported < cutoff or late):
                when = reported
            elif late:  # a story BASIS already had: the item is the late filing
                when = max(f.filed_at for f in late)
            else:
                continue
            visible = shown(story_calls, list(names))
            if story.id in calls and not visible:
                continue  # every company only named in passing
            section = section_for(visible)
            if section == "low":
                sections.add("low", when, low_item(story, visible, names, settings, gap_end, when))
            else:
                sections.add(section, when, story_item(story, visible, names, settings, gap_end))
        if typical and run.job == "catchup":  # through a late run the live check was running
            for event in moves_in_period(
                session, watchlist, typical, settings, gap_start, run.started_at
            ):
                if event.at >= run.started_at - away:
                    continue  # fresh: the live check alerts it
                sections.add(
                    "moves", event.at, "\n".join(move_lines(event, names, settings, gap_end))
                )
        gaps = details.get("possible_gaps") or []
        if not len(sections) and not gaps:
            empty.append(run)
            continue
        header = ["<b>BASIS · while you were away</b>"]
        if run.job == "late":
            seen = _local(run.started_at, settings)
            header.append(
                f"<i>BSE filings the PEAD tool caught up on, first seen at {seen}</i>"
                if details.get("exchanges") == ["BSE"]
                else f"<i>Filings that reached BASIS late, first seen at {seen}</i>"
            )
        count = len(sections)
        header.append(
            f"<i>{time_range(gap_start, gap_end, settings)} · "
            f"{count} {'update' if count == 1 else 'updates'}</i>"
        )
        body = sections.blocks()
        if not body:
            body = ["", "Nothing about your watchlist was reported while BASIS was off."]
        footer = [
            f"<i>Possible gap: {_local(datetime.fromisoformat(g['from']), settings)} to "
            f"{_local(datetime.fromisoformat(g['to']), settings)} - {_t(g['what'])} "
            f"({_t(g['why'])})</i>"
            for g in gaps
        ]
        out.append(Outgoing("away", f"away:{run.id}", tuple(split(header, body, footer))))
    return out, empty


def rebuild_away(
    session: Session,
    watchlist: WatchlistFile,
    typical: Mapping[str, Typical] | None,
    settings: Settings,
    run: WatchRun,
    now: datetime,
) -> list[str] | None:
    """A past "while you were away" summary rebuilt in the current format, headed TEST, for
    `newsdesk watch-test-message`. Nothing is written: the run is only treated as unsent in
    memory, so the session must be rolled back (or closed) afterwards, never committed."""
    with session.no_autoflush:
        run.details = {**(run.details or {}), "summary_sent": False}
        out, _ = away_summaries(session, watchlist, typical, settings, now)
    for item in out:
        if item.key == f"away:{run.id}":
            messages = list(item.messages)
            messages[0] = messages[0].replace(
                "<b>BASIS · while you were away</b>",
                "<b>TEST · BASIS · while you were away</b>",
                1,
            )
            return messages
    return None


def news_alert_times(
    session: Session, story_ids: Sequence[int], before: datetime
) -> dict[int, datetime]:
    """When each of these stories had its instant alert sent, if it had one by `before`."""
    if not story_ids:
        return {}
    return {
        story_id: sent_at
        for story_id, sent_at in session.execute(
            select(WatchAlert.story_id, WatchAlert.sent_at).where(
                WatchAlert.kind == "news",
                WatchAlert.story_id.in_(list(story_ids)),
                WatchAlert.sent_at.is_not(None),
                WatchAlert.sent_at <= before,
            )
        )
        if story_id is not None and sent_at is not None
    }


UPDATE_DESCRIPTION_CHARS = 200


def update_item(
    story: WatchStory,
    late: Sequence[WatchFiling],
    calls: Mapping[str, WatchCall],
    alerted_at: datetime,
    names: Mapping[str, str],
    settings: Settings,
    ref: datetime,
) -> str:
    """A late filing on a story already alerted, as an update to that story (user,
    2026-10-08): the story as the alert named it and when it was alerted, each late filing
    at its own time, and the story's call again if the filing changed it."""
    first_late_seen = min(f.first_seen_at for f in late)
    recalled = [c for c in shown(calls, list(names)) if c.created_at >= first_late_seen]
    item = story_item(story, recalled, names, settings, ref, reason=False).split("\n")
    head, rest = item[0].split(": ", 1)
    lines = [f"{head}: Update: {rest} (alerted {_local(alerted_at, settings, ref)})", *item[1:]]
    lines += [
        _filing_line(filing, settings, ref, UPDATE_DESCRIPTION_CHARS)
        for filing in sorted(late, key=lambda f: f.filed_at)
    ]
    if recalled:
        lines.append(_t(recalled[0].reason))
    return "\n".join(lines)


def _filing_line(
    filing: WatchFiling, settings: Settings, ref: datetime, description_chars: int = 0
) -> str:
    """ "08:00 · BSE · Company Update: <description> · the filing" """
    text = _t(filing.subject)
    description = filing.description.strip()
    if description_chars and description:
        if len(description) > description_chars:
            description = description[: description_chars - 1].rstrip() + "…"
        text += f": {_t(description)}"
    link = f' · <a href="{escape(filing.link, quote=True)}">the filing</a>' if filing.link else ""
    return f"{_local(filing.filed_at, settings, ref)} · {filing.exchange} · {text}{link}"


def split(header: Sequence[str], body: Sequence[str], footer: Sequence[str]) -> list[str]:
    """Messages under Telegram's limit, never splitting a line."""
    messages: list[str] = []
    current = "\n".join(header)
    for block in [*body, *(["", *footer] if footer else [])]:
        candidate = f"{current}\n{block}" if current else block
        if telegram_length(candidate) > TELEGRAM_LIMIT and current:
            messages.append(current)
            current = block.lstrip("\n")
        else:
            current = candidate
    messages.append(current)
    return messages


# ---------------------------------------------------------------- sending


def deliver(
    session_factory: sessionmaker[Session],
    outgoing: Sequence[Outgoing],
    send: Sender | None,
    now: datetime,
) -> AlertResult:
    """Record each alert under its key, then send it. A key already recorded is skipped,
    unless an earlier send failed and it is still worth retrying."""
    result = AlertResult()
    for item in outgoing:
        with session_factory() as session:
            alert = session.scalar(select(WatchAlert).where(WatchAlert.key == item.key))
            if alert is None:
                alert = WatchAlert(
                    key=item.key,
                    kind=item.kind,
                    story_id=item.story_id,
                    symbols=list(item.symbols),
                    text=MESSAGE_SEPARATOR.join(item.messages),
                    created_at=now,
                    attempts=0,
                )
                session.add(alert)
            elif (
                alert.sent_at is not None
                or alert.attempts >= MAX_ATTEMPTS
                or now - alert.created_at > RETRY_FOR
            ):
                continue
            alert.attempts += 1
            session.commit()
            alert_id = alert.id
        error = None
        if send is None:
            error = "no Telegram credentials (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)"
        else:
            try:
                send(list(item.messages))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        with session_factory() as session:
            alert = session.get(WatchAlert, alert_id)
            if alert is not None:
                if error is None:
                    alert.sent_at = now
                    alert.error = None
                else:
                    alert.error = error
                session.commit()
        if error is None:
            result.sent.append(item.key)
        else:
            result.failed.append((item.key, error))
            log.warning("watch alert %s not sent: %s", item.key, error)
    return result


def retry_pending(session: Session, now: datetime) -> list[Outgoing]:
    """Alerts recorded but not sent, still inside the retry window. Never a feed warning:
    those are no longer sent at all, and one recorded by an older version stays unsent."""
    return [
        Outgoing(
            a.kind, a.key, tuple(a.text.split(MESSAGE_SEPARATOR)), a.story_id, tuple(a.symbols)
        )
        for a in session.scalars(
            select(WatchAlert).where(
                WatchAlert.kind != "feed",
                WatchAlert.sent_at.is_(None),
                WatchAlert.attempts < MAX_ATTEMPTS,
                WatchAlert.created_at >= now - RETRY_FOR,
            )
        )
    ]


def mark_summarised(
    session_factory: sessionmaker[Session], runs: Sequence[WatchRun], note: str
) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    with session_factory() as session:
        for stale in runs:
            run = session.get(WatchRun, stale.id)
            if run is None:
                continue
            run.details = {**(run.details or {}), "summary_sent": True, "summary_note": note}
            flag_modified(run, "details")
        session.commit()


# ---------------------------------------------------------------- the digest


def digest_section(
    session: Session, watchlist: WatchlistFile, settings: Settings, now: datetime
) -> str | None:
    """The watchlist section at the top of the digest: today's called stories (the news day
    starts at 22:00 the night before, as for the digest) and today's price and sector
    alerts, in the same shape as everywhere else (app/watch/render.py). None when there is
    nothing."""
    names = {s.symbol: s.name or s.symbol for s in watchlist.stocks}
    since = start_of_news_day(settings, now) or now - timedelta(hours=24)
    max_age = stale_after(settings)
    stories = _load_stories(session, since)
    calls = current_calls(session, [s.id for s in stories])
    sections = Sections.empty()
    count = 0
    listed: set[int] = set()
    for story in stories:
        visible = shown(calls.get(story.id, {}), list(names))
        # Today's news, as for the rest of the digest: what broke since the day began, not
        # what BASIS happened to read today.
        reported = first_reported(story, max_age)
        if not visible or reported < since:
            continue
        count += 1
        listed.add(story.id)
        section = section_for(visible)
        if section == "low":
            sections.add("low", reported, low_item(story, visible, names, settings, now, reported))
        else:
            sections.add(section, reported, story_item(story, visible, names, settings, now))
    # News that came out today in a story BASIS already had, when a call made today rates a
    # stock higher than before: under that news's own headline, at its own time.
    older = [i for i in _called_since(session, since) if i not in listed]
    history = call_history(session, older)
    for story in _load_stories(session, ids=older):
        news = new_news(story, history.get(story.id, []), since, (since, None), max_age)
        if news is None:
            continue
        visible = shown(current_calls(session, [story.id]).get(story.id, {}), list(names))
        count += 1
        items = (news.articles, news.filings)
        section = section_for(list(news.raised))
        if section == "low":
            sections.add(
                "low",
                news.when,
                low_item(story, visible, names, settings, now, news.when, items=items),
            )
        else:
            sections.add(
                section, news.when, story_item(story, visible, names, settings, now, items=items)
            )
    moves = session.scalars(
        select(WatchAlert)
        .where(WatchAlert.kind.in_(["price", "sector"]), WatchAlert.created_at >= since)
        .order_by(WatchAlert.created_at)
    ).all()
    for alert in moves:
        lines = alert.text.split(MESSAGE_SEPARATOR)[0].split("\n")[1:]
        sections.add("moves", alert.created_at, "\n".join(lines) or alert.text)
    if not len(sections):
        return None
    head = f"<b>WATCHLIST</b> · {count} {'story' if count == 1 else 'stories'} today"
    return "\n".join([head, *(block for block in sections.blocks(spaced=False))])
