"""Feed health: which feeds are failing or have gone stale, shown on /runs.

It used to be a Telegram alert, and it was wrong in the way that matters most: it measured a
feed's age against the clock, so after a night with the laptop off every feed looked a day
stale, and the user got one notification per feed on waking (2026-10-15, 22 of them). Feed
health is now a page section only (user, 2026-10-16), and a feed's age is measured at its
own last fetch - the newest entry against the moment BASIS last asked - so time the laptop
spent off is never counted against a feed. A feed is only judged once BASIS has fetched it
again; until then the page says when it was last checked.

  failing  the last `feed_failing_after` fetches all failed
  stale    at its last fetch, made inside the daytime window, its newest entry was older than
           `feed_stale_hours` AND twice what is usual for that feed in the daytime. A feed
           needs STALE_HISTORY daytime fetches before it can be stale at all: ET's curated
           company feed is hours old all day, and a US outlet is quiet through India's day.

Google News feeds are never stale: a quiet day for a stock is not a feed fault.
"""

import contextlib
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import CONFIG_DIR, FeedConfig, Settings, load_feeds
from app.models import FeedCheck

STALE_HISTORY = 12  # daytime fetches of a feed before it can be called stale
HISTORY_DAYS = 14
WATCH_FEEDS_FILE = CONFIG_DIR / "watch_feeds.yaml"


@dataclass(frozen=True)
class FeedStatus:
    url: str
    label: str
    kind: str  # pipeline | watch (whichever fetched it last)
    state: str  # failing | stale | ok | new (too little history to judge)
    last_checked: datetime
    newest_entry: datetime | None
    age_hours: float | None  # the newest entry's age at the last fetch
    usual_hours: float | None  # the median daytime age, once there is history
    detail: str

    @property
    def problem(self) -> bool:
        return self.state in ("failing", "stale")


def _clock(value: str) -> time:
    hours, minutes = (int(part) for part in value.split(":"))
    return time(hours, minutes)


def configured_labels() -> dict[str, str]:
    """Every configured feed's label by URL, so old checks recorded under a shared outlet
    name ("CNBC") still show which feed they were. Never raises: a broken feeds file must
    not take the page down."""
    labels: dict[str, str] = {}
    feeds: list[FeedConfig] = []
    with contextlib.suppress(Exception):
        feeds += load_feeds(include_disabled=True)
    with contextlib.suppress(Exception):
        feeds += load_feeds(WATCH_FEEDS_FILE, include_disabled=True)
    for feed in feeds:
        labels.setdefault(feed.url, feed.label)
    return labels


def _is_google_news(url: str, label: str) -> bool:
    return urlsplit(url).hostname == "news.google.com" or label.startswith("Google News")


def feed_health(
    session: Session,
    settings: Settings,
    now: datetime,
    labels: Mapping[str, str] | None = None,
) -> list[FeedStatus]:
    """One status per feed fetched in the last week, problems first, then by label."""
    watch = settings.watch
    labels = configured_labels() if labels is None else labels
    rows = session.scalars(
        select(FeedCheck)
        .where(FeedCheck.checked_at >= now - timedelta(days=HISTORY_DAYS))
        .order_by(FeedCheck.checked_at)
    ).all()
    by_feed: dict[str, list[FeedCheck]] = {}
    for row in rows:
        by_feed.setdefault(row.feed_url, []).append(row)
    start, end = (_clock(value) for value in watch.feed_stale_window)
    tz = settings.tz

    def daytime(moment: datetime) -> bool:
        return start <= moment.astimezone(tz).time() <= end

    found: list[FeedStatus] = []
    for url, checks in by_feed.items():
        last = checks[-1]
        label = labels.get(url, last.feed_name)
        newest: datetime | None = None
        ages: list[float] = []
        for check in checks:
            if check.newest_entry_at is not None:
                newest = (
                    check.newest_entry_at if newest is None else max(newest, check.newest_entry_at)
                )
            if newest is not None and check.status != "error" and daytime(check.checked_at):
                ages.append((check.checked_at - newest).total_seconds() / 3600)
        age = (last.checked_at - newest).total_seconds() / 3600 if newest is not None else None
        usual = statistics.median(ages) if ages else None

        recent = checks[-watch.feed_failing_after :]
        if len(recent) == watch.feed_failing_after and all(c.status == "error" for c in recent):
            since = recent[0].checked_at
            for check in reversed(checks[: -watch.feed_failing_after]):
                if check.status != "error":
                    break
                since = check.checked_at
            state = "failing"
            detail = f"failing since {_local(since, settings, now)}: {last.error or 'error'}"
        elif _is_google_news(url, label):
            state, detail = "ok", "searches are never judged stale"
        elif age is None or len(ages) < STALE_HISTORY:
            state, detail = "new", "too little history to judge yet"
        elif (
            last.status != "error"
            and daytime(last.checked_at)
            and age > max(watch.feed_stale_hours, 2 * (usual or 0))
        ):
            state = "stale"
            detail = (
                f"nothing new for {age:.1f} h at its last fetch "
                f"(usually {usual:.1f} h in the daytime)"
            )
        else:
            state, detail = "ok", ""
        found.append(
            FeedStatus(
                url=url,
                label=label,
                kind=last.kind,
                state=state,
                last_checked=last.checked_at,
                newest_entry=newest,
                age_hours=age,
                usual_hours=usual,
                detail=detail,
            )
        )
    found.sort(key=lambda s: (not s.problem, s.state != "failing", s.label.lower(), s.url))
    return found


def problems(statuses: Sequence[FeedStatus]) -> list[FeedStatus]:
    return [status for status in statuses if status.problem]


def _local(moment: datetime, settings: Settings, ref: datetime) -> str:
    local = moment.astimezone(settings.tz)
    if local.date() == ref.astimezone(settings.tz).date():
        return f"{local:%H:%M}"
    return f"{local:%a %d %b %H:%M}"
