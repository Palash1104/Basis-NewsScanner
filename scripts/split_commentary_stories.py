"""Split the stories share-price commentary began and events then joined (user, 2026-10-19).

Until 2026-10-19 a story begun by share-price commentary took in events like any other, and
four stored stories did: #59 (a multibagger list, then Astra Microwave's Q1 results), #93 (a
"rise streak" note, then BDL's Rs 1,307 crore torpedo order and a Rs 56.8 crore tender), #101
(a Zen share-price note, then the Cogknit acquisition: NSE's filing, the press release and the
reports) and #170 (a "buzzing stocks" list, then a $9.4 billion aerospace-metals piece). The
grouper now keeps events out of such a story (app/watch/events.py `is_commentary`,
`is_event`). This moves each event the rule would have kept out to where the grouper would
have put it, so the database reads as if the rule had always been there:

  items    every filing and every article that `is_event` in those four stories, replayed
           through the scanner's own grouping (`Watcher._group`) pass by pass, as they arrived:
           each one's window is what BASIS had stored by then, with the rule in force. It
           joins an existing story or starts a new one, as the grouper decides.
  calls    each call goes with the news its reason describes, or stays (CALLS, below).
  scores   a moved call's score rows are deleted; `newsdesk score` judges it again from its
           new story, if it is that story's first call on the stock.

Run it with the scanner stopped, after scripts/cleanup_stale_news.py:

  uv run python scripts/split_commentary_stories.py           # dry run, on a copy
  uv run python scripts/split_commentary_stories.py --apply   # back up, split, check

The dry run does everything on a copy of the database (SQLite's backup API) and throws it
away. --apply first backs up into data/backups/ the same way, then splits in one transaction
(BEGIN IMMEDIATE, foreign keys enforced) and runs PRAGMA integrity_check and
foreign_key_check. Both print where each item and call goes, the score rows deleted and from
when they will be judged again, and the alerts, away summaries and digest due now - before and
after - to show the split sends nothing. A second run finds nothing to split.
"""

import argparse
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, event, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, selectinload, sessionmaker  # noqa: E402

from app.config import Settings, WatchlistFile, load_settings, load_watchlist_file  # noqa: E402
from app.models import (  # noqa: E402
    WatchAlert,
    WatchArticle,
    WatchCall,
    WatchFiling,
    WatchScore,
    WatchStory,
    utcnow,
)
from app.pipeline.embed import Embedder, article_text, load_embedder  # noqa: E402
from app.pipeline.fetch import SourceResolver  # noqa: E402
from app.pipeline.prices import INTRADAY, cached_bars, reference_point  # noqa: E402
from app.watch.alerts import away_summaries, digest_section, followups, news_alerts  # noqa: E402
from app.watch.analyse import stale_after, stories_to_call  # noqa: E402
from app.watch.events import is_commentary, is_event  # noqa: E402
from app.watch.scan import Watcher  # noqa: E402
from app.watch.scoring import judged_from, scorable_calls  # noqa: E402

STORIES = (59, 93, 101, 170)
# Every call on those stories: the item whose news its reason describes - the call moves with
# it - or None, the call stays. A call not listed stops the split.
CALLS: dict[int, tuple[str, int] | None] = {
    47: (
        "article",
        195,
    ),  # Astra, high negative: "a YoY decline in net profit ... MD stepping down"
    92: ("article", 383),  # BDL, high positive: "a major Rs 1,307 crore torpedo order"
    157: ("article", 383),  # BDL, the same, called again on 19 Oct
    153: ("filing", 37),  # Zen, high positive: "acquiring a majority 52% stake in Cogknit"
    91: None,  # Zen, low neutral: "examines ... numbers ... without announcing" - the note
    154: None,  # Astra, passing: "not mentioned in the articles or filings"
    145: None,  # BEL, passing: "included as a Buy in a list of 16 stocks"
}

Item = WatchArticle | WatchFiling


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    moved: int = 0

    def say(self, line: str = "") -> None:
        self.lines.append(line)


def sqlite_engine(path: Path) -> Engine:
    """SQLAlchemy over pysqlite, with the transaction under our control: BEGIN IMMEDIATE takes
    the write lock before the first read, so everything is one transaction (SQLAlchemy's
    documented recipe for pysqlite)."""
    engine = create_engine(f"sqlite:///{path.as_posix()}")

    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def backup(database: Path, target: Path) -> None:
    source = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    source.backup(copy)  # consistent, WAL included
    copy.close()
    source.close()


def label(item: Item) -> str:
    return f"{'article' if isinstance(item, WatchArticle) else 'filing'} {item.id}"


def text_of(item: Item) -> str:
    if isinstance(item, WatchArticle):
        return item.title
    return f"{item.subject}: {item.description}"


def to_move(story: WatchStory) -> list[Item]:
    """What the rule keeps out of a story begun by commentary: every filing, and every article
    that is an event."""
    return [*(a for a in story.articles if is_event(a.title)), *story.filings]


def load(session: Session, ids: list[int] | None = None) -> list[WatchStory]:
    query = select(WatchStory).options(
        selectinload(WatchStory.articles).selectinload(WatchArticle.matches),
        selectinload(WatchStory.filings),
    )
    if ids is not None:
        query = query.where(WatchStory.id.in_(ids))
    return list(session.scalars(query))


def pending(session: Session) -> int:
    """How many items the split would move: 0 once it has run."""
    return sum(
        len(to_move(story))
        for story in load(session, list(STORIES))
        if is_commentary(story.headline)
    )


def what_is_due(
    session: Session, watchlist: WatchlistFile, settings: Settings, now: datetime
) -> dict[str, Any]:
    """Everything the scanner would send, or call, if it ran now. Nothing is written."""
    names = {s.symbol: s.name or s.symbol for s in watchlist.stocks}
    away, _ = away_summaries(session, watchlist, None, settings, now)
    return {
        "alerts": sorted(o.key for o in news_alerts(session, settings, names, now)),
        "follow-ups": sorted(o.key for o in followups(session, settings, names, now)),
        "away summaries": sorted(o.key for o in away),
        "digest section": digest_section(session, watchlist, settings, now),
        "calls analyse would make": sorted(
            given.story_id
            for given in stories_to_call(
                session, {s.symbol: s for s in watchlist.stocks}, settings, now
            )
        ),
    }


def split(
    session: Session,
    settings: Settings,
    watchlist: WatchlistFile,
    embedder: Embedder,
    now: datetime,
) -> Report:
    """Move, in the session's transaction; the caller commits or rolls back."""
    out = Report()
    max_age = stale_after(settings)
    local = settings.tz

    def when(moment: datetime) -> str:
        return f"{moment.astimezone(local):%a %d %b %H:%M} IST"

    stories = {story.id: story for story in load(session, list(STORIES))}
    for story_id in STORIES:
        if story_id not in stories:
            out.problems.append(f"story #{story_id} is gone")
        elif not is_commentary(stories[story_id].headline):
            out.problems.append(
                f"#{story_id} didn't begin as commentary: {stories[story_id].headline!r}"
            )
    others = [
        story.id
        for story in load(session)
        if story.id not in STORIES and is_commentary(story.headline) and to_move(story)
    ]
    if others:
        out.say(f"Also begun by commentary and holding an event, not split here: {others}")
    moving = {story_id: to_move(story) for story_id, story in stories.items()}
    out.moved = sum(len(items) for items in moving.values())
    if not out.moved:
        out.say("Nothing to split: no story begun by commentary holds an event.")
        return out

    calls = list(session.scalars(select(WatchCall).where(WatchCall.story_id.in_(STORIES))))
    anchors: dict[int, Item] = {}
    for call in calls:
        if call.id not in CALLS:
            out.problems.append(f"call {call.id} on #{call.story_id} isn't listed in CALLS")
            continue
        anchor = CALLS[call.id]
        if anchor is None:
            continue
        kind, item_id = anchor
        found = next(
            (i for i in moving.get(call.story_id, []) if label(i) == f"{kind} {item_id}"), None
        )
        if found is None:
            out.problems.append(
                f"call {call.id} goes with {kind} {item_id}, not moving from #{call.story_id}"
            )
        else:
            anchors[call.id] = found
    if out.problems:
        return out

    # Before anything moves: what the moving calls are judged from, and what is due now.
    history: dict[int, list[WatchCall]] = defaultdict(list)
    for call in session.scalars(select(WatchCall).where(WatchCall.story_id.in_(STORIES))):
        history[call.story_id].append(call)
    was_judged = {
        call_id: judged_from(call, stories[call.story_id], history[call.story_id])
        for call_id, call in ((c.id, c) for c in calls if c.id in anchors)
    }
    due_before = what_is_due(session, watchlist, settings, now)
    last_story = session.scalar(select(WatchStory.id).order_by(WatchStory.id.desc()).limit(1)) or 0

    # Take the events out, then replay them pass by pass through the scanner's grouping.
    items = [item for story_items in moving.values() for item in story_items]
    came_from = {label(item): item.story_id for item in items}
    for item in items:
        item.story = None
    session.flush()
    watcher = Watcher(
        settings,
        sessionmaker(bind=session.get_bind()),
        watchlist,
        [],
        SourceResolver([]),
        embedder,
        None,
    )
    passes: dict[datetime, list[Item]] = defaultdict(list)
    for item in items:
        passes[item.first_seen_at].append(item)
    for moment in sorted(passes):
        batch = passes[moment]
        articles = [i for i in batch if isinstance(i, WatchArticle)]
        filings = [i for i in batch if isinstance(i, WatchFiling)]
        window = watcher._window_members(session, moment)
        watcher._warm(
            [member.text for member in window]
            + [article_text(a.title, a.snippet) for a in articles]
            + [article_text(f.subject, f.description) for f in filings]
        )
        watcher._group(session, window, articles, filings, moment)
        session.flush()

    # The calls go with their news; a moved call's score rows go.
    deleted_scores: dict[int, list[int]] = {}
    for call in calls:
        if call.id in anchors:
            call.story_id = anchors[call.id].story_id  # type: ignore[assignment]
            rows = list(session.scalars(select(WatchScore).where(WatchScore.call_id == call.id)))
            deleted_scores[call.id] = [row.id for row in rows]
            for row in rows:
                session.delete(row)
    session.flush()
    session.expire_all()

    # The report, story by story.
    after = {story.id: story for story in load(session)}
    new_ids = {story_id for story_id in after if story_id > last_story}
    scored_after = {c.id for c in scorable_calls(session, max_age)}
    history_after: dict[int, list[WatchCall]] = defaultdict(list)
    for call in session.scalars(select(WatchCall)):
        history_after[call.story_id].append(call)
    for story_id in STORIES:
        story = after[story_id]
        out.say(f"#{story_id} {story.headline[:80]!r} (first seen {when(story.first_seen_at)})")
        for article in sorted(story.articles, key=lambda a: a.first_seen_at):
            out.say(f"    stays   article {article.id} {article.title[:80]!r}")
        for item in sorted(
            (i for i in items if came_from[label(i)] == story_id), key=lambda i: i.first_seen_at
        ):
            target = after[item.story_id]  # type: ignore[index]
            where = "NEW" if target.id in new_ids else "existing"
            out.say(
                f"    moves   {label(item)} {when(item.first_seen_at)} {text_of(item)[:70]!r}\n"
                f"            -> #{target.id} ({where}) {target.headline[:80]!r}"
            )
        for call in sorted(
            (c for c in calls if came_from_call(c, anchors, came_from) == story_id),
            key=lambda c: c.id,
        ):
            head = (
                f"    call {call.id} {call.symbol} {call.materiality} {call.sentiment}"
                f" {call.event_type}, {when(call.created_at)}: {call.reason[:90]!r}"
            )
            if call.id not in anchors:
                out.say(f"{head}\n            stays")
                continue
            target = after[call.story_id]
            out.say(f"{head}\n            -> with {label(anchors[call.id])} to #{target.id}")
            if call.id in scored_after:
                judged = judged_from(call, target, history_after[call.story_id])
                note = f"judged from {when(judged)} (was {when(was_judged[call.id])})"
                new_point = reference_point(
                    cached_bars(session, call.symbol, INTRADAY, judged - timedelta(days=10)),
                    judged,
                )
                old_point = reference_point(
                    cached_bars(
                        session, call.symbol, INTRADAY, was_judged[call.id] - timedelta(days=10)
                    ),
                    was_judged[call.id],
                )
                if new_point and old_point:
                    same = new_point[0] == old_point[0]
                    note += f"; first bar {when(new_point[0])}" + (
                        ", the same bar as before" if same else f" (was {when(old_point[0])})"
                    )
            else:
                first = next(
                    c.id
                    for c in history_after[call.story_id]
                    if c.id in scored_after and c.symbol == call.symbol
                )
                note = f"not judged again: #{target.id} is judged on call {first}, its first"
            out.say(
                f"            score rows deleted: {deleted_scores.get(call.id) or 'none'}; {note}"
            )
        out.say()

    # Nothing may still be mixed, and the split must send nothing.
    for story_id in STORIES:
        left = to_move(after[story_id])
        if left:
            out.problems.append(f"#{story_id} still holds events: {[label(i) for i in left]}")
    due_after = what_is_due(session, watchlist, settings, now)
    out.say(f"Due at {when(now)}, before and after the split:")
    for key in due_before:
        before, later = due_before[key], due_after[key]
        if key == "digest section":
            # Today's section as a digest sent now would have it: by item, not by markup.
            gone = [line for line in items_of(before) if line not in items_of(later)]
            added = [line for line in items_of(later) if line not in items_of(before)]
            out.say(f"  {key}: {'unchanged' if not gone and not added else 'changed'}")
            out.lines += [f"    drops {line}" for line in gone] + [
                f"    adds  {line}" for line in added
            ]
            continue
        out.say(f"  {key}: before {before}, after {later}")
        if key == "calls analyse would make":
            if later != before:
                out.say("    (the calls the split leaves the scanner to make)")
        elif set(later) - set(before):
            out.problems.append(f"the split would send {key}: {sorted(set(later) - set(before))}")
    alerts = session.scalars(select(WatchAlert.id).where(WatchAlert.story_id.in_(STORIES))).all()
    out.say(f"  alerts already sent on these stories: {list(alerts) or 'none'}")
    return out


def items_of(section: str | None) -> list[str]:
    """A digest section's items, one line each: the company and headline, links dropped."""
    if not section:
        return []
    lines = []
    for line in section.split("\n"):
        text = re.sub(r"<[^>]+>", "", line).strip()
        if text[:1] in "▲▼●·":
            lines.append(text)
    return lines


def came_from_call(
    call: WatchCall, anchors: dict[int, Item], came_from: dict[str, int | None]
) -> int:
    """The story a call was on before the split."""
    if call.id in anchors:
        return came_from[label(anchors[call.id])]  # type: ignore[return-value]
    return call.story_id


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="back up, then split")
    parser.add_argument(
        "--database",
        type=Path,
        help="another database (a copy, to try it on); default the live one",
    )
    args = parser.parse_args()
    settings = load_settings()
    database = args.database or settings.resolve_path(settings.paths.database)
    watchlist = load_watchlist_file()
    now = utcnow()

    reader = sqlite_engine(database)
    with Session(reader) as session:
        todo = pending(session)
        session.rollback()
    reader.dispose()
    if not todo:
        print("Nothing to split: no story begun by commentary holds an event.")
        return 0
    embedder, unavailable = load_embedder(settings.grouping.embedding_model)
    if embedder is None:
        print(f"The embedding model the scanner groups with is unavailable: {unavailable}")
        return 1

    if not args.apply:
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(scratch) / "dry-run.db"
            backup(database, copy)
            engine = sqlite_engine(copy)
            with Session(engine, expire_on_commit=False) as session:
                report = split(session, settings, watchlist, embedder, now)
                session.rollback()
            engine.dispose()
        print("\n".join(report.lines))
        if report.problems:
            print("\nWould not split:")
            print("\n".join(f"  - {p}" for p in report.problems))
            return 1
        print(
            f"Dry run, on a copy: {report.moved} items would move. "
            "Run with --apply, the scanner stopped."
        )
        return 0

    backups = database.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    target = backups / f"newsdesk.{datetime.now():%Y%m%d-%H%M%S}.before-commentary-split.db"
    backup(database, target)
    print(f"Backed up to {target}")
    engine = sqlite_engine(database)
    with Session(engine, expire_on_commit=False) as session:
        try:
            report = split(session, settings, watchlist, embedder, now)
            if report.problems:
                session.rollback()
            else:
                session.commit()
        except Exception:
            session.rollback()
            raise
    engine.dispose()
    print("\n".join(report.lines))
    if report.problems:
        print("\nNot split; nothing changed:")
        print("\n".join(f"  - {p}" for p in report.problems))
        return 1
    print(f"Split in one transaction: {report.moved} items moved.")
    conn = sqlite3.connect(database)
    integrity = conn.execute("PRAGMA integrity_check").fetchall()
    dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    print(f"integrity_check: {', '.join(row[0] for row in integrity)}")
    print(f"foreign_key_check: {dangling if dangling else 'no problems'}")
    return 0 if integrity == [("ok",)] and not dangling else 1


if __name__ == "__main__":
    sys.exit(main())
