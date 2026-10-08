"""Watchlist alerts and the digest's watchlist section (step 4).

What reaches Telegram, and when:

  news       a story the call rates high for any watchlist company it is about: one alert
             for the story, listing every company (user, 2026-10-08), as soon as it is called
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
  feed       a feed failing (errors in a row) or gone stale (nothing new for much longer
             than is usual for it, in the daytime)

Every alert is a watch_alerts row whose key makes it happen once. It is written before it is
sent; a failed send is retried for half an hour, three times at most.
"""

import logging
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.config import Settings, WatchlistFile
from app.delivery.format import TELEGRAM_LIMIT, telegram_length
from app.models import (
    FeedCheck,
    WatchAlert,
    WatchArticle,
    WatchCall,
    WatchFiling,
    WatchPrice,
    WatchRun,
    WatchStory,
)
from app.schedule import start_of_news_day
from app.watch.analyse import kept_articles, kept_symbols, trigger_for
from app.watch.moves import MoveEvent, Snapshot, Typical, detect

log = logging.getLogger(__name__)

MARK = {"positive": "▲", "negative": "▼", "neutral": "●"}
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
STALE_HISTORY = 12  # daytime checks of a feed before it can be called stale

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


def first_reported(story: WatchStory) -> datetime:
    """When the news first came out: the earliest kept article's publication or filing time,
    and never later than when BASIS saw it."""
    times = [a.published_at for a in kept_articles(story)] + [f.filed_at for f in story.filings]
    return min([*times, story.first_seen_at])


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


def _sources(story: WatchStory) -> str:
    links, seen = [], set()
    for article in sorted(kept_articles(story), key=lambda a: a.first_seen_at):
        if article.source_name in seen:
            continue
        seen.add(article.source_name)
        links.append(f'<a href="{escape(article.url, quote=True)}">{_t(article.source_name)}</a>')
        if len(links) == SOURCES_SHOWN:
            break
    return " · ".join(links)


def _first_seen_line(story: WatchStory, settings: Settings) -> str:
    articles = sorted(story.articles, key=lambda a: a.first_seen_at)
    if not articles:
        exchanges = sorted({f.exchange for f in story.filings}) or ["the exchange"]
        where = " and ".join(exchanges)
        return f"first seen {_local(story.first_seen_at, settings)} in {where}'s filings"
    first = articles[0]
    via = (
        "Google News"
        if first.sightings and first.sightings[0].via == "google_news"
        else first.source_name
    )
    return f"first seen {_local(first.first_seen_at, settings)} via {_t(via)}"


def news_message(
    story: WatchStory, calls: Sequence[WatchCall], names: Mapping[str, str], settings: Settings
) -> str:
    lines = [
        "<b>BASIS · watchlist alert</b>",
        f"<b>{_marks(calls, names)}</b>",
        f"<b>{_t(story.headline)}</b>",
        f"<i>{_t(story_state(story, settings))} · {_first_seen_line(story, settings)}</i>",
        "",
        _t(calls[0].summary),
    ]
    details = [
        f"{MARK[c.sentiment]} {_t(names.get(c.symbol, c.symbol))} · {c.materiality} · "
        f"{c.event_type.replace('_', ' ')} — {_t(c.reason)}"
        for c in calls
    ]
    if sources := _sources(story):
        details.append(f"Sources: {sources}")
    lines.append("<blockquote expandable>" + "\n".join(details) + "</blockquote>")
    return "\n".join(lines)


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
    stories = _load_stories(session, now - NEWS_MAX_AGE)
    calls = current_calls(session, [s.id for s in stories])
    already = _sent_keys(session, [f"news:{s.id}" for s in stories])
    away = timedelta(minutes=settings.watch.away_after_minutes)
    backlog = {run.started_at for run in _catch_up_passes(session, now - NEWS_MAX_AGE)}
    out: list[Outgoing] = []
    for story in sorted(stories, key=lambda s: s.first_seen_at):
        key = f"news:{story.id}"
        visible = shown(calls.get(story.id, {}), list(names))
        if key in already or not visible or visible[0].materiality != "high":
            continue
        reported = first_reported(story)
        if reported < now - NEWS_MAX_AGE:
            continue
        if story.first_seen_at in backlog and reported < story.first_seen_at - away:
            continue  # read in a catch-up, long after it broke: the away summary has it
        out.append(
            Outgoing(
                "news",
                key,
                (news_message(story, visible, names, settings),),
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
            f"<b>{_t(story.headline)}</b>",
            f"<i>Now: {_t(story_state(story, settings))}</i>",
        ]
        for filing in sorted(new, key=lambda f: f.filed_at):
            link = (
                f' · <a href="{escape(filing.link, quote=True)}">the filing</a>'
                if filing.link
                else ""
            )
            lines.append(
                f"{_local(filing.filed_at, settings, now)} · {filing.exchange} · "
                f"{_t(filing.subject)}{link}"
            )
        if visible and recalled:
            lines += ["", f"<b>{_marks(visible, names)}</b>", _t(visible[0].reason)]
        out.append(
            Outgoing(
                "followup", key, ("\n".join(lines),), story.id, tuple(c.symbol for c in visible)
            )
        )
    return out


def move_message(event: MoveEvent, names: Mapping[str, str], settings: Settings) -> str:
    when = _local(event.at, settings)
    if event.kind == "sector":
        group = (event.group or "sector").capitalize()
        head = (
            f"{MARK['positive' if event.move > 0 else 'negative']} {group} index {event.move:+.1%}"
        )
        peers = " · ".join(f"{_t(names.get(s, s))} {m:+.1%}" for s, m in event.peers)
        times = f" · {event.multiple:.1f}× its typical day" if event.multiple else ""
        return "\n".join(
            [
                f"<b>BASIS · {group.lower()} sector move</b>",
                f"<b>{_t(head)}</b> at {when}{times}",
                peers,
                "<i>The peers moved together, so no stock is alerted on its own.</i>",
            ]
        )
    name = names.get(event.symbol or "", event.symbol or "")
    head = f"{MARK['positive' if event.move > 0 else 'negative']} {name} {event.move:+.1%}"
    net = (
        f", {event.excess:+.1%} net of the {event.group} index" if event.excess is not None else ""
    )
    return "\n".join(
        [
            "<b>BASIS · moved, no story yet</b>",
            f"<b>{_t(head)}</b> since the last close, at {when}{net}",
            f"{event.multiple:.1f}× its typical day's move. No story about it yet in the feeds "
            "BASIS reads.",
        ]
    )


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


def stories_since(session: Session, symbols: Sequence[str], since: datetime) -> dict[str, bool]:
    """Which stocks have a story (a keep or a filing) first seen since `since`."""
    found = dict.fromkeys(symbols, False)
    for story in _load_stories(session, since):
        for symbol in kept_symbols(story):
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
        session, [s.symbol for s in watchlist.stocks], last_session_close(now, settings)
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
    moment by moment, with only the stories BASIS had by each moment; the first time each
    key fires is its event."""
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
    seen = [
        (symbol, story.first_seen_at)
        for story in _load_stories(session, earliest)
        for symbol in kept_symbols(story)
    ]
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
            symbol: any(s == symbol and close <= at <= moment for s, at in seen)
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
        if missing := late_story_ids - {s.id for s in stories}:
            stories += _load_stories(session, ids=sorted(missing))
        calls = current_calls(session, [s.id for s in stories])
        alerted = news_alert_times(session, [s.id for s in stories], run.started_at)
        unfinished = [s for s in stories if trigger_for(s, list(calls.get(s.id, {}).values()))]
        finished_at = run.finished_at or run.started_at
        if unfinished and now - finished_at < AWAY_WAIT:
            continue  # its stories are still being called
        items: list[tuple[datetime, str]] = []
        for story in stories:
            reported = first_reported(story)
            late = [
                f
                for f in story.filings
                if f.first_seen_at == run.started_at and f.filed_at < cutoff
            ]
            if late and story.id in alerted:
                # The reader has this story already: the filing is an update to it.
                when = max(f.filed_at for f in late)
                text = update_item(
                    story,
                    late,
                    calls.get(story.id, {}),
                    alerted[story.id],
                    names,
                    settings,
                    gap_end,
                )
                items.append((when, text))
                continue
            if story.first_seen_at == run.started_at and (gap_start <= reported < cutoff or late):
                when = reported
            elif late:  # a story BASIS already had: the item is the late filing
                when = max(f.filed_at for f in late)
            else:
                continue
            visible = shown(calls.get(story.id, {}), list(names))
            if story.id in calls and not visible:
                continue  # every company only named in passing
            head = (
                _marks(visible, names)
                if visible
                else _t(", ".join(names.get(s, s) for s in sorted(kept_symbols(story))))
            )
            line = f"• <b>{_local(when, settings, gap_end)}</b> · {head} — {_t(story.headline)}"
            detail = _t(visible[0].reason) if visible else ""
            state = _t(story_state(story, settings))
            items.append((when, f"{line}\n  <i>{detail + ' · ' if detail else ''}{state}</i>"))
        if typical and run.job == "catchup":  # through a late run the live check was running
            for event in moves_in_period(
                session, watchlist, typical, settings, gap_start, run.started_at
            ):
                if event.at >= run.started_at - away:
                    continue  # fresh: the live check alerts it
                text = move_message(event, names, settings).split("\n")[1]
                items.append(
                    (event.at, f"• <b>{_local(event.at, settings, gap_end)}</b> · moved: {text}")
                )
        gaps = details.get("possible_gaps") or []
        if not items and not gaps:
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
        header += [
            f"<i>{_local(gap_start, settings)} – {_local(gap_end, settings)} · "
            f"{len(items)} {'item' if len(items) == 1 else 'items'}, newest first</i>",
            "",
        ]
        body = [text for _, text in sorted(items, key=lambda pair: pair[0], reverse=True)]
        if not body:
            body = ["Nothing about your watchlist was reported while BASIS was off."]
        footer = [
            f"<i>Possible gap: {_local(datetime.fromisoformat(g['from']), settings)} to "
            f"{_local(datetime.fromisoformat(g['to']), settings)} - {_t(g['what'])} "
            f"({_t(g['why'])})</i>"
            for g in gaps
        ]
        out.append(Outgoing("away", f"away:{run.id}", tuple(split(header, body, footer))))
    return out, empty


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
    2026-10-08): the story as the alert named it, when it was alerted, each late filing at
    its own time, and the story's call again if the filing changed it."""
    first_late_seen = min(f.first_seen_at for f in late)
    lines = [
        f"• <b>{_local(max(f.filed_at for f in late), settings, ref)}</b> · "
        f"Update: {_t(story.headline)} (alerted {_local(alerted_at, settings, ref)})"
    ]
    for filing in sorted(late, key=lambda f: f.filed_at):
        description = filing.description.strip()
        if len(description) > UPDATE_DESCRIPTION_CHARS:
            description = description[: UPDATE_DESCRIPTION_CHARS - 1].rstrip() + "…"
        link = (
            f' · <a href="{escape(filing.link, quote=True)}">the filing</a>' if filing.link else ""
        )
        lines.append(
            f"  {_local(filing.filed_at, settings, ref)} · {filing.exchange} · "
            f"{_t(filing.subject)}: {_t(description)}{link}"
        )
    recalled = [c for c in shown(calls, list(names)) if c.created_at >= first_late_seen]
    if recalled:
        lines.append(f"  <i>{_marks(recalled, names)} · {_t(recalled[0].reason)}</i>")
    return "\n".join(lines)


def split(header: Sequence[str], body: Sequence[str], footer: Sequence[str]) -> list[str]:
    """Messages under Telegram's limit, never splitting a line."""
    messages: list[str] = []
    current = "\n".join(header)
    for block in [*body, *(["", *footer] if footer else [])]:
        candidate = f"{current}\n{block}" if current else block
        if telegram_length(candidate) > TELEGRAM_LIMIT and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    messages.append(current)
    return messages


# ---------------------------------------------------------------- feed health


def feed_warnings(session: Session, settings: Settings, now: datetime) -> list[Outgoing]:
    """One warning per feed per episode: failing (`feed_failing_after` errors in a row) or
    stale - nothing new for longer than `feed_stale_hours` AND twice what is usual for the
    feed in the daytime. A feed needs STALE_HISTORY daytime checks before it can be stale at
    all: ET's curated company feed is hours old all day, and a US outlet is quiet through
    India's daytime, and neither is a fault (both would have warned on the first day)."""
    watch = settings.watch
    since = now - timedelta(days=7)
    rows = session.scalars(
        select(FeedCheck).where(FeedCheck.checked_at >= since).order_by(FeedCheck.checked_at)
    ).all()
    by_feed: dict[str, list[FeedCheck]] = {}
    for row in rows:
        by_feed.setdefault(row.feed_url, []).append(row)
    start, end = (time(*(int(x) for x in v.split(":"))) for v in watch.feed_stale_window)
    lines: list[tuple[str, str]] = []
    for url, checks in by_feed.items():
        name = checks[-1].feed_name
        recent = checks[-watch.feed_failing_after :]
        if len(recent) == watch.feed_failing_after and all(c.status == "error" for c in recent):
            run_start = recent[0].checked_at
            for check in reversed(checks[: -watch.feed_failing_after]):
                if check.status != "error":
                    break
                run_start = check.checked_at
            lines.append(
                (
                    f"feed:{url}:failing:{run_start.isoformat()}",
                    f"• {_t(name)}: failing since {_local(run_start, settings, now)} "
                    f"({_t(recent[-1].error or 'error')})",
                )
            )
            continue
        if name == "Google News" or not start <= now.astimezone(settings.tz).time() <= end:
            continue
        newest: datetime | None = None
        ages: list[float] = []
        for check in checks:
            if check.newest_entry_at is not None:
                newest = (
                    check.newest_entry_at if newest is None else max(newest, check.newest_entry_at)
                )
            if (
                newest is not None
                and check.status != "error"
                and start <= check.checked_at.astimezone(settings.tz).time() <= end
            ):
                ages.append((check.checked_at - newest).total_seconds() / 3600)
        if newest is None or checks[-1].status == "error" or len(ages) < STALE_HISTORY:
            continue
        age = (now - newest).total_seconds() / 3600
        if age > max(watch.feed_stale_hours, 2 * statistics.median(ages)):
            day = now.astimezone(settings.tz).date().isoformat()
            lines.append(
                (
                    f"feed:{url}:stale:{day}",
                    f"• {_t(name)}: nothing new for {age:.1f} h (newest entry "
                    f"{_local(newest, settings, now)})",
                )
            )
    if not lines:
        return []
    already = _sent_keys(session, [key for key, _ in lines])
    fresh = [(key, text) for key, text in lines if key not in already]
    out = []
    for key, text in fresh:
        out.append(Outgoing("feed", key, ("<b>BASIS · feed warning</b>\n" + text,)))
    return out


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
    """Alerts recorded but not sent, still inside the retry window."""
    return [
        Outgoing(
            a.kind, a.key, tuple(a.text.split(MESSAGE_SEPARATOR)), a.story_id, tuple(a.symbols)
        )
        for a in session.scalars(
            select(WatchAlert).where(
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
    starts at 22:00 the night before, as for the digest), most material first, then today's
    price and sector alerts. None when there is nothing."""
    names = {s.symbol: s.name or s.symbol for s in watchlist.stocks}
    since = start_of_news_day(settings, now) or now - timedelta(hours=24)
    stories = _load_stories(session, since)
    calls = current_calls(session, [s.id for s in stories])
    rows: list[tuple[tuple[int, float], WatchStory, list[WatchCall]]] = []
    for story in stories:
        visible = shown(calls.get(story.id, {}), list(names))
        # Today's news, as for the rest of the digest: what broke since the day began, not
        # what BASIS happened to read today.
        if visible and first_reported(story) >= since:
            rows.append(
                ((RANK[visible[0].materiality], -story.first_seen_at.timestamp()), story, visible)
            )
    moves = session.scalars(
        select(WatchAlert)
        .where(WatchAlert.kind.in_(["price", "sector"]), WatchAlert.created_at >= since)
        .order_by(WatchAlert.created_at)
    ).all()
    if not rows and not moves:
        return None
    rows.sort(key=lambda row: row[0])
    lines = [f"<b>WATCHLIST</b> · {len(rows)} {'story' if len(rows) == 1 else 'stories'} today"]
    for _, story, visible in rows[:DIGEST_STORIES]:
        lines.append(f"<b>{_marks(visible, names)}</b> · {_t(story.headline)}")
        # The state only where it says something: a filing, or a story rated high.
        state = (
            f" · {_t(story_state(story, settings))}"
            if story.filings or visible[0].materiality == "high"
            else ""
        )
        lines.append(f"<i>{_t(visible[0].reason)}{state}</i>")
    rest = rows[DIGEST_STORIES:]
    if rest:
        lines.append(
            "<blockquote expandable>"
            + "\n".join(f"{_marks(v, names)} · {_t(s.headline)}" for _, s, v in rest)
            + "</blockquote>"
        )
    for alert in moves:
        head = alert.text.split("\n")[1] if "\n" in alert.text else alert.text
        lines.append(f"Moved: {head}")
    return "\n".join(lines)
