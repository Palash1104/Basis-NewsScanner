"""What the watchlist scanner saw: `newsdesk watch-report`.

Step 2's measurements, before any LLM call is wired in:
  - coverage: when the scanner was actually running (the laptop sleeps)
  - articles and stories per stock per day, by verdict, and filings
  - how often one story touches several watchlist stocks
  - which source saw each story first, and by how much
  - feed health: failures and staleness, as the alert would judge them
  - Yahoo's lag in market hours, and how far each stock and its peers moved
  - whether the market-hours wake ever woke the laptop
"""

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, WatchlistFile
from app.models import (
    FeedCheck,
    WatchArticle,
    WatchFiling,
    WatchPrice,
    WatchRun,
    WatchStory,
)
from app.watch.power import Resume

VERDICT_ORDER = {"keep": 0, "mention": 1, "drop": 2}
VERDICTS = ("keep", "mention", "drop")


@dataclass(frozen=True)
class _First:
    """The earliest item of a story: who had it, through what, and when BASIS saw it."""

    seen_at: datetime
    outlet: str
    channel: str  # the feed it came through, "Google News", or the exchange


def _local_day(value: datetime, settings: Settings) -> date:
    return value.astimezone(settings.tz).date()


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _p90(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]


def _fmt_minutes(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes = seconds / 60
    return f"{minutes:.0f} min" if minutes < 120 else f"{minutes / 60:.1f} h"


def _clock(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def watch_report(
    session: Session,
    settings: Settings,
    watchlist: WatchlistFile,
    now: datetime,
    days: int,
    resumes: Sequence[Resume] = (),
) -> str:
    since = now - timedelta(days=days)
    names = {stock.symbol: stock.name or stock.symbol for stock in watchlist.stocks}
    tz = settings.tz
    lines = [
        "# Watchlist scanner report",
        "",
        f"{since.astimezone(tz):%d %b %H:%M} to {now.astimezone(tz):%d %b %H:%M %Z} "
        f"({days} days). No LLM calls are made at this step.",
    ]
    runs = list(
        session.scalars(
            select(WatchRun).where(WatchRun.started_at >= since).order_by(WatchRun.started_at)
        )
    )
    lines += _coverage(runs, settings, since, now)
    articles = list(
        session.scalars(
            select(WatchArticle)
            .where(WatchArticle.first_seen_at >= since)
            .options(selectinload(WatchArticle.matches), selectinload(WatchArticle.sightings))
        )
    )
    filings = list(session.scalars(select(WatchFiling).where(WatchFiling.first_seen_at >= since)))
    stories = list(
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
    backlog = _backlog_passes(runs, settings)
    lines += _per_stock(articles, stories, filings, names, settings)
    lines += _multi_stock(stories, names, settings)
    lines += _first_sources(stories, backlog, settings)
    checks = list(
        session.scalars(
            select(FeedCheck).where(FeedCheck.checked_at >= since).order_by(FeedCheck.checked_at)
        )
    )
    lines += _feed_health(checks, settings, now)
    prices = list(
        session.scalars(
            select(WatchPrice).where(WatchPrice.polled_at >= since).order_by(WatchPrice.polled_at)
        )
    )
    lines += _price_lag(prices, settings)
    lines += _moves(prices, watchlist, settings)
    lines += wake_section(runs, resumes, settings, since, now)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- coverage


def _coverage(
    runs: Sequence[WatchRun], settings: Settings, since: datetime, now: datetime
) -> list[str]:
    lines = ["", "## Coverage", ""]
    feeds = [run for run in runs if run.job == "feeds"]
    if not feeds:
        return [*lines, "The scanner has not run in this period."]
    every = timedelta(minutes=settings.watch.feeds_every_minutes)
    by_day: dict[date, int] = Counter(_local_day(run.started_at, settings) for run in feeds)
    per_day = 24 * 60 // settings.watch.feeds_every_minutes
    lines += [
        f"Feed scans run (one every {settings.watch.feeds_every_minutes} min while the laptop is "
        f"awake; {per_day} would be a full day):",
        "",
        "| Day | scans | share of the day |",
        "|---|---|---|",
    ]
    for day in sorted(by_day):
        lines.append(f"| {day:%a %d %b} | {by_day[day]} | {by_day[day] / per_day:.0%} |")
    gaps = []
    previous = feeds[0].started_at  # before the first scan the scanner may not have existed
    lines += ["", f"First scan in this period: {previous.astimezone(settings.tz):%a %d %b %H:%M}."]
    for run in [*feeds, None]:
        moment = run.started_at if run is not None else now
        if moment - previous > 3 * every:
            gaps.append((previous, moment))
        previous = moment
    if gaps:
        lines += ["", "Gaps of more than three scans (asleep, off, or not started):", ""]
        tz = settings.tz
        for start, end in gaps:
            lines.append(
                f"- {start.astimezone(tz):%a %d %b %H:%M} to {end.astimezone(tz):%a %d %b %H:%M} "
                f"({_fmt_minutes((end - start).total_seconds())})"
            )
    errors = Counter(
        error.get("feed") or error.get("stage") or "?" for run in runs for error in run.errors
    )
    if errors:
        lines += [
            "",
            "Errors recorded by job runs: "
            + ", ".join(f"{name} {count}" for name, count in errors.most_common()),
        ]
    return lines


def _backlog_passes(runs: Sequence[WatchRun], settings: Settings) -> set[tuple[str, datetime]]:
    """Passes that read a backlog: the first after a start or a gap. What they "first saw"
    had been published while nobody was looking, so it says nothing about which source was
    first, and is left out of that table."""
    intervals = {
        "feeds": timedelta(minutes=settings.watch.feeds_every_minutes),
        "google_news": timedelta(minutes=settings.watch.google_news_quiet_every_minutes),
    }
    backlog: set[tuple[str, datetime]] = set()
    last: dict[str, datetime] = {}
    for run in runs:
        if run.job not in intervals:
            continue
        previous = last.get(run.job)
        if previous is None or run.started_at - previous > 2 * intervals[run.job]:
            backlog.add((run.job, run.started_at))
        last[run.job] = run.started_at
    return backlog


# ---------------------------------------------------------------- volumes


def _story_verdicts(story: WatchStory) -> dict[str, str]:
    """Each stock's best verdict across the story's articles; a filing counts as a keep."""
    best: dict[str, str] = {}
    for article in story.articles:
        for match in article.matches:
            current = best.get(match.symbol)
            if current is None or VERDICT_ORDER[match.verdict] < VERDICT_ORDER[current]:
                best[match.symbol] = match.verdict
    for filing in story.filings:
        best[filing.symbol] = "keep"
    return best


def _per_stock(
    articles: Sequence[WatchArticle],
    stories: Sequence[WatchStory],
    filings: Sequence[WatchFiling],
    names: dict[str, str],
    settings: Settings,
) -> list[str]:
    lines = ["", "## Articles and stories per stock per day", ""]
    lines.append(
        "Articles are distinct headlines (the same story syndicated by two outlets is two); "
        "a story's verdict for a stock is the best verdict among its articles. Days are IST, "
        "by when BASIS first saw the article. A filing is counted under filings only."
    )
    counts: dict[str, dict[date, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    for article in articles:
        day = _local_day(article.first_seen_at, settings)
        for match in article.matches:
            counts[match.symbol][day][f"a_{match.verdict}"] += 1
    for story in stories:
        day = _local_day(story.first_seen_at, settings)
        for symbol, verdict in _story_verdicts(story).items():
            if story.articles or verdict != "keep":
                counts[symbol][day][f"s_{verdict}"] += 1
    for filing in filings:
        counts[filing.symbol][_local_day(filing.first_seen_at, settings)]["filings"] += 1
    days = sorted({day for per_day in counts.values() for day in per_day})
    if not days:
        return [*lines, "", "Nothing yet."]

    lines += [
        "",
        "Totals over the period, and per day on average:",
        "",
        "| Stock | articles keep / mention / drop | stories keep / mention / drop | filings |"
        " keep stories per day |",
        "|---|---|---|---|---|",
    ]
    for symbol, name in names.items():
        total: Counter[str] = Counter()
        for per_day in counts.get(symbol, {}).values():
            total.update(per_day)
        lines.append(
            f"| {name} | {total['a_keep']} / {total['a_mention']} / {total['a_drop']} | "
            f"{total['s_keep']} / {total['s_mention']} / {total['s_drop']} | {total['filings']} | "
            f"{total['s_keep'] / len(days):.1f} |"
        )
    lines += ["", "By day (articles k/m/d · stories k/m/d · filings):", ""]
    lines.append("| Day | " + " | ".join(names.values()) + " |")
    lines.append("|---|" + "---|" * len(names))
    for day in days:
        cells = []
        for symbol in names:
            c = counts.get(symbol, {}).get(day, Counter())
            cells.append(
                f"{c['a_keep']}/{c['a_mention']}/{c['a_drop']} · "
                f"{c['s_keep']}/{c['s_mention']}/{c['s_drop']} · {c['filings']}"
            )
        lines.append(f"| {day:%a %d %b} | " + " | ".join(cells) + " |")
    return lines


def _multi_stock(
    stories: Sequence[WatchStory], names: dict[str, str], settings: Settings
) -> list[str]:
    lines = ["", "## Stories touching several watchlist stocks", ""]
    kept = [(story, _story_verdicts(story)) for story in stories]
    kept = [(story, v) for story, v in kept if "keep" in v.values()]
    multi = [(story, v) for story, v in kept if sum(1 for x in v.values() if x == "keep") >= 2]
    named = [(story, v) for story, v in kept if len(v) >= 2]
    if not kept:
        return [*lines, "No story with a keep yet."]
    lines.append(
        f"{len(multi)} of {len(kept)} stories with a keep are a keep for two or more stocks "
        f"({len(multi) / len(kept):.0%}) - each of these would be one alert listing them all. "
        f"{len(named)} name two or more at any verdict."
    )
    if multi:
        lines += ["", "| First seen | Stocks (keep) | Headline |", "|---|---|---|"]
        tz = settings.tz
        for story, verdicts in sorted(multi, key=lambda pair: pair[0].first_seen_at):
            stocks = ", ".join(names.get(s, s) for s, v in verdicts.items() if v == "keep")
            lines.append(
                f"| {story.first_seen_at.astimezone(tz):%d %b %H:%M} | {stocks} | "
                f"{story.headline.replace('|', '/')} |"
            )
    return lines


# ---------------------------------------------------------------- first sources


def _members(story: WatchStory) -> list[_First]:
    found = []
    for article in story.articles:
        first = article.sightings[0] if article.sightings else None
        channel = (
            "Google News"
            if first is not None and first.via == "google_news"
            else (first.feed_name if first is not None else "?")
        )
        found.append(_First(article.first_seen_at, article.source_name, channel))
    for filing in story.filings:
        found.append(_First(filing.first_seen_at, f"{filing.exchange} filing", filing.exchange))
    return sorted(found, key=lambda item: item.seen_at)


def _first_sources(
    stories: Sequence[WatchStory], backlog: set[tuple[str, datetime]], settings: Settings
) -> list[str]:
    lines = ["", "## Which source saw each story first", ""]
    lines.append(
        "Stories with at least one keep. Times are when BASIS first saw each item, so sources "
        "seen in the same pass tie. Stories first seen in a pass right after a gap (a start, "
        "or the laptop waking) are left out: that pass read a backlog."
    )
    backlog_times = {moment for _, moment in backlog}
    first_counts: Counter[str] = Counter()
    channel_counts: Counter[str] = Counter()
    tied = 0
    leads: list[float] = []
    lead_by_outlet: dict[str, list[float]] = defaultdict(list)
    media_first = filing_first = 0
    filing_gaps: list[float] = []
    counted = skipped = 0
    for story in stories:
        if "keep" not in _story_verdicts(story).values():
            continue
        members = _members(story)
        if not members:
            continue
        if members[0].seen_at in backlog_times:
            skipped += 1
            continue
        counted += 1
        earliest = members[0].seen_at
        leaders = [m for m in members if m.seen_at == earliest]
        outlets = {m.outlet for m in leaders}
        if len(outlets) > 1:
            tied += 1
        for outlet in outlets:
            first_counts[outlet] += 1
        for channel in {m.channel for m in leaders}:
            channel_counts[channel] += 1
        later = [m for m in members if m.outlet not in outlets]
        if later:
            lead = (later[0].seen_at - earliest).total_seconds()
            leads.append(lead)
            for outlet in outlets:
                lead_by_outlet[outlet].append(lead)
        media = [m for m in members if not m.outlet.endswith("filing")]
        exchange = [m for m in members if m.outlet.endswith("filing")]
        if media and exchange:
            gap = (exchange[0].seen_at - media[0].seen_at).total_seconds()
            filing_gaps.append(gap)
            if gap > 0:
                media_first += 1
            elif gap < 0:
                filing_first += 1
    if not counted:
        return [*lines, "", f"Nothing to count yet ({skipped} stories came from a backlog pass)."]
    lines += [
        "",
        f"{counted} {'story' if counted == 1 else 'stories'} counted, {skipped} left out as "
        f"backlog, {tied} with a tie for first.",
        "",
        "| Source | first on | median lead over the next source |",
        "|---|---|---|",
    ]
    for outlet, count in first_counts.most_common():
        lines.append(f"| {outlet} | {count} | {_fmt_minutes(_median(lead_by_outlet[outlet]))} |")
    lines += ["", "| Channel it came through | first on |", "|---|---|"]
    for channel, count in channel_counts.most_common():
        lines.append(f"| {channel} | {count} |")
    lines.append("")
    lines.append(
        f"Median lead of the first source over the next: {_fmt_minutes(_median(leads))} "
        f"(over {len(leads)} stories that a second source also carried)."
    )
    if filing_gaps:
        lines.append(
            f"Stories with both media and a filing: media first on {media_first}, the filing "
            f"first on {filing_first}; median gap media → filing "
            f"{_fmt_minutes(_median([g for g in filing_gaps if g > 0]))}."
        )
    return lines


# ---------------------------------------------------------------- feed health


def _in_window(moment: datetime, settings: Settings) -> bool:
    start, end = (_clock(value) for value in settings.watch.feed_stale_window)
    local = moment.astimezone(settings.tz).time()
    return start <= local <= end


def _feed_health(checks: Sequence[FeedCheck], settings: Settings, now: datetime) -> list[str]:
    lines = ["", "## Feed health", ""]
    if not checks:
        return [*lines, "No feed checks recorded yet."]
    failing_after = settings.watch.feed_failing_after
    stale = timedelta(hours=settings.watch.feed_stale_hours)
    start, end = settings.watch.feed_stale_window
    lines.append(
        f"A feed would be reported **failing** after {failing_after} failed checks in a row, "
        f"and **stale** when its newest entry is more than {settings.watch.feed_stale_hours:g} "
        f"hours old at a check between {start} and {end} IST. Each episode counts once. The "
        "newest entry's time is the publisher's, carried over 304 answers. Nothing is sent "
        "at this step: these are the warnings the alert would have given."
    )
    by_feed: dict[tuple[str, str], list[FeedCheck]] = defaultdict(list)
    for check in checks:
        by_feed[(check.kind, check.feed_url)].append(check)
    for kind in ("watch", "pipeline"):
        feeds = [(url, rows) for (k, url), rows in by_feed.items() if k == kind]
        if not feeds:
            continue
        title = "Watch feeds (every 10 min)" if kind == "watch" else "Pipeline feeds (every run)"
        lines += [
            "",
            f"### {title}",
            "",
            "| Feed | checks | ok / 304 / error | longest error run | new entries per day |"
            " median age of newest | would warn: failing / stale |",
            "|---|---|---|---|---|---|---|",
        ]
        days = max(1.0, (checks[-1].checked_at - checks[0].checked_at).total_seconds() / 86400)
        for url, rows in sorted(feeds, key=lambda pair: (pair[1][0].feed_name, pair[0])):
            status = Counter(row.status for row in rows)
            longest = run = 0
            failing_episodes = 0
            for row in rows:
                run = run + 1 if row.status == "error" else 0
                longest = max(longest, run)
                if run == failing_after:
                    failing_episodes += 1
            newest: datetime | None = None
            ages: list[float] = []
            stale_episodes = 0
            in_episode = False
            for row in rows:
                if row.newest_entry_at is not None:
                    newest = (
                        row.newest_entry_at if newest is None else max(newest, row.newest_entry_at)
                    )
                if newest is None or row.status == "error":
                    continue
                age = row.checked_at - newest
                ages.append(age.total_seconds())
                is_stale = age > stale and _in_window(row.checked_at, settings)
                if is_stale and not in_episode:
                    stale_episodes += 1
                in_episode = is_stale if _in_window(row.checked_at, settings) else in_episode
            known_new = [row.new_entries for row in rows if row.new_entries is not None]
            per_day = f"{sum(known_new) / days:.0f}" if known_new else "-"
            label = rows[0].feed_name
            short = url.split("//", 1)[-1][:60]
            current = " (failing now)" if run >= failing_after else ""
            # A search can't go stale: a quiet day for a stock is not a broken feed.
            stale_note = "n/a" if label == "Google News" else str(stale_episodes)
            lines.append(
                f"| {label} `{short}` | {len(rows)} | {status['ok']} / {status['not_modified']} / "
                f"{status['error']} | {longest}{current} | {per_day} | "
                f"{_fmt_minutes(_median(ages))} | {failing_episodes} / {stale_note} |"
            )
        errors = Counter(
            (rows[0].feed_name, row.error) for _, rows in feeds for row in rows if row.error
        )
        if errors:
            lines += ["", "Errors seen:", ""]
            lines += [
                f"- {name}: {error} ({count}×)" for (name, error), count in errors.most_common(12)
            ]
    return lines


# ---------------------------------------------------------------- prices


def _price_lag(prices: Sequence[WatchPrice], settings: Settings) -> list[str]:
    lines = ["", "## Yahoo's lag in market hours", ""]
    if not prices:
        return [*lines, "No price polls yet (they run Monday to Friday, 09:15-15:35 IST)."]
    lines.append(
        "Lag is the poll time minus Yahoo's own time for the last trade. Polls on a day the "
        "stock didn't trade (a holiday) are left out; so are polls in the first two minutes "
        "of the session. A 1-minute bar is stamped with the start of its minute, so an age "
        "under 60s means Yahoo already has the minute in progress."
    )
    lines += [
        "",
        "| Symbol | polls | errors | median lag | p90 | max |"
        " age of the newest 1-minute bar, median |",
        "|---|---|---|---|---|---|---|",
    ]
    by_symbol: dict[str, list[WatchPrice]] = defaultdict(list)
    for row in prices:
        by_symbol[row.symbol].append(row)
    open_at = _clock(settings.watch.market_open)
    for symbol, rows in by_symbol.items():
        lags, bars = [], []
        errors = sum(1 for row in rows if row.error)
        for row in rows:
            if row.error or row.last_trade_at is None:
                continue
            local = row.polled_at.astimezone(settings.tz)
            if row.last_trade_at.astimezone(settings.tz).date() != local.date():
                continue
            if (
                local.time()
                < (datetime.combine(local.date(), open_at) + timedelta(minutes=2)).time()
            ):
                continue
            lags.append((row.polled_at - row.last_trade_at).total_seconds())
            if row.newest_bar_at is not None:
                bars.append((row.polled_at - row.newest_bar_at).total_seconds())
        fmt = lambda v: "-" if v is None else f"{v:.0f}s"  # noqa: E731
        lines.append(
            f"| {symbol} | {len(rows)} | {errors} | {fmt(_median(lags))} | {fmt(_p90(lags))} | "
            f"{fmt(max(lags) if lags else None)} | {fmt(_median(bars))} |"
        )
    return lines


def _moves(prices: Sequence[WatchPrice], watchlist: WatchlistFile, settings: Settings) -> list[str]:
    """Each day's move from the previous close at the last poll, and the largest move seen
    during the day: what the price-move alert's thresholds will be set from."""
    lines = ["", "## Daily moves (from the polls)", ""]
    good = [row for row in prices if row.price is not None and row.previous_close]
    if not good:
        return [*lines, "No polls with prices yet."]
    last: dict[tuple[date, str], WatchPrice] = {}
    widest: dict[tuple[date, str], float] = {}
    for row in good:
        day = _local_day(row.polled_at, settings)
        move = row.price / row.previous_close - 1  # type: ignore[operator]
        last[(day, row.symbol)] = row
        widest[(day, row.symbol)] = max(widest.get((day, row.symbol), 0.0), abs(move))
    days = sorted({day for day, _ in last})
    symbols = list(dict.fromkeys(row.symbol for row in good))
    group_of = {stock.symbol: stock.group for stock in watchlist.stocks}
    lines.append(
        "Close: the last poll's price against the previous close. Widest: the largest move "
        "from the previous close at any poll that day."
    )
    lines += ["", "| Symbol | " + " | ".join(f"{d:%a %d %b} close / widest" for d in days) + " |"]
    lines.append("|---|" + "---|" * len(days))
    for symbol in symbols:
        cells = []
        for day in days:
            row = last.get((day, symbol))
            if row is None:
                cells.append("-")
                continue
            close = row.price / row.previous_close - 1  # type: ignore[operator]
            cells.append(f"{close:+.1%} / {widest[(day, symbol)]:.1%}")
        suffix = f" ({group_of[symbol]})" if group_of.get(symbol) else ""
        lines.append(f"| {symbol}{suffix} | " + " | ".join(cells) + " |")
    for key, group in watchlist.groups.items():
        members = [s for s, g in group_of.items() if g == key]
        if not group.index:
            continue
        lines.append("")
        for day in days:
            index_row = last.get((day, group.index))
            moves = [
                last[(day, s)].price / last[(day, s)].previous_close - 1  # type: ignore[operator]
                for s in members
                if (day, s) in last
            ]
            if index_row is None or not moves:
                continue
            index_move = index_row.price / index_row.previous_close - 1  # type: ignore[operator]
            same = sum(1 for m in moves if (m > 0) == (index_move > 0))
            lines.append(
                f"- {day:%a %d %b}: {group.name} index {index_move:+.1%}; {same} of "
                f"{len(moves)} {group.name.lower()} names moved the same way."
            )
    return lines


# ---------------------------------------------------------------- the wake


def wake_section(
    runs: Iterable[WatchRun],
    resumes: Sequence[Resume],
    settings: Settings,
    since: datetime,
    now: datetime,
) -> list[str]:
    """Did the market-hours wake work? For each weekday morning in the period: whether the
    timer fired, whether the network came up, whether a scan ran - and when nothing fired,
    what Windows says woke the laptop instead. Then every wake run, and every resume."""
    tz = settings.tz
    lines = ["", "## Market-hours wake", ""]
    wakes = [run for run in runs if run.job == "wake"]
    in_period = [r for r in resumes if r.woke_at >= since]
    start_at = _clock(settings.watch.wake_start)

    lines.append(
        f"The wake task runs weekdays {settings.watch.wake_start}-{settings.watch.wake_end}, "
        "every 10 min, on AC power only. A run that found the laptop already awake had "
        "nothing to fire."
    )
    lines += ["", "### Each weekday morning", ""]
    day = since.astimezone(tz).date()
    any_day = False
    while day <= now.astimezone(tz).date():
        first_slot = datetime.combine(day, start_at, tzinfo=tz)
        if day.weekday() < 5 and since <= first_slot <= now:
            any_day = True
            lines.append(
                f"- {day:%a %d %b}: " + _morning(day, first_slot, wakes, resumes, settings)
            )
        day += timedelta(days=1)
    if not any_day:
        lines.append(f"- No weekday {settings.watch.wake_start} in this period yet.")

    lines += ["", "### Wake runs", ""]
    if not wakes:
        lines.append("None: the task never ran (on battery, or the timer didn't wake the laptop).")
    else:
        lines += ["| Time | AC | fired | network | scan |", "|---|---|---|---|---|"]
        for run in wakes:
            fired, network, scan = _wake_cells(run, settings)
            on_ac = {True: "yes", False: "no", None: "?"}[run.on_ac]
            lines.append(
                f"| {run.started_at.astimezone(tz):%a %d %b %H:%M} | {on_ac} | {fired} | "
                f"{network} | {scan} |"
            )

    lines += ["", "### Every resume Windows logged", ""]
    if not in_period:
        lines.append("None in this period (or the event log couldn't be read).")
    else:
        lines += ["| Asleep from | Woke at | State | What woke it |", "|---|---|---|---|"]
        for resume in in_period:
            lines.append(
                f"| {resume.slept_at.astimezone(tz):%a %d %b %H:%M} | "
                f"{resume.woke_at.astimezone(tz):%a %d %b %H:%M} | {resume.slept_as} | "
                f"{resume.woken_by} |"
            )
    return lines


def _morning(
    day: date,
    first_slot: datetime,
    wakes: Sequence[WatchRun],
    resumes: Sequence[Resume],
    settings: Settings,
) -> str:
    tz = settings.tz
    todays = [w for w in wakes if w.started_at.astimezone(tz).date() == day and w.details]
    fired = [w for w in todays if (w.details or {}).get("fired") == "timer"]
    if fired:
        first = fired[0]
        _, network, scan = _wake_cells(first, settings)
        return (
            f"**the wake timer fired** at {first.started_at.astimezone(tz):%H:%M}; network "
            f"{network}; scan {scan}."
        )
    # Asleep across the first slot? Then the timer should have woken it, and didn't.
    asleep = next((r for r in resumes if r.slept_at <= first_slot <= r.woke_at), None)
    if asleep is not None:
        shut = (
            " A shut-down laptop can't be woken by a timer, and the tasks only run while you "
            "are logged in."
            if asleep.slept_as == "shut down"
            else ""
        )
        return (
            f"**the wake timer did not fire**: the laptop was {asleep.slept_as} from "
            f"{asleep.slept_at.astimezone(tz):%a %H:%M} until "
            f"{asleep.woke_at.astimezone(tz):%a %H:%M}, woken by {asleep.woken_by}.{shut}"
        )
    if todays:
        return (
            f"the laptop was already awake at {settings.watch.wake_start}; "
            f"{len(todays)} wake runs found it so."
        )
    return (
        f"no wake run and no sleep covering {settings.watch.wake_start} in Windows's log "
        "(on battery, shut down and not logged in, or the log has no record)."
    )


def _wake_cells(run: WatchRun, settings: Settings) -> tuple[str, str, str]:
    """fired, network and scan, in words, from a wake run's details."""
    tz = settings.tz
    details = run.details or {}
    resume = details.get("resume") or {}
    fired = {
        "timer": f"YES, {resume.get('woken_by', 'a wake timer')}",
        "resume": f"no - resumed by {resume.get('woken_by', '?')}",
        "awake": "not needed (awake)",
    }.get(details.get("fired", ""), "?")
    network_info = details.get("network") or {}
    if network_info.get("up"):
        network = f"up after {network_info.get('after_seconds') or 0:.0f} s"
    elif network_info:
        network = "DOWN"
    else:
        network = "?"
    scan_info = details.get("scan") or {}
    if scan_info.get("by") in ("resident", "this task") and scan_info.get("at"):
        at = datetime.fromisoformat(scan_info["at"]).astimezone(tz)
        failed = scan_info.get("feed_errors") or 0
        who = "the resident scanner" if scan_info["by"] == "resident" else "the wake task"
        scan = f"YES, by {who} at {at:%H:%M}, {scan_info.get('new_articles', 0)} new" + (
            f", {failed} feeds failed" if failed else ""
        )
    else:
        scan = f"NONE ({scan_info.get('note') or 'no details'})"
    return fired, network, scan
