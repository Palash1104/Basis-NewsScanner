"""Remove the old news Google News searches stored before the intake rule (user, 2026-10-19).

Until 2026-10-19 the scanner stored any search result naming a watchlist stock, however old;
the intake rule (`Watcher._store_articles`) now skips one published more than
`watch.catch_up_max_days` before BASIS first sees it. This removes what the rule would have
kept out, so the database looks as if the rule had always been there and nothing written
later has to know about the exception (user's decision, 2026-10-19):

  stories made only of old pages   #194, #196, #204 and #195, with their calls (140, 141, 151)
                                   and the score rows on them (123 and 124, on call 140)
  old pages in current stories     593 (#101), 588 and 586 (#131), 618 (#147)

Call 149 (#147) stays. Run it with the scanner stopped:

  uv run python scripts/cleanup_stale_news.py           # dry run: what it would delete
  uv run python scripts/cleanup_stale_news.py --apply   # back up, delete, check

With --apply it first copies the database into data/backups/ with SQLite's backup API (a
file copy could miss what is still in the WAL), deletes everything in one transaction -
dependents first, with foreign keys enforced - and then runs PRAGMA integrity_check and
foreign_key_check. Every row is checked against what was decided before anything is deleted:
a story must hold nothing but old pages, an article must be old news, a call must belong to its
story. Anything else and it stops without deleting. Rows already gone are reported, not an
error, so a second run does nothing.
"""

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402

STORIES = {194: (140,), 196: (141,), 204: (151,), 195: ()}  # story -> the calls it holds
SCORES = (123, 124)  # on call 140
OLD_PAGES = {593: 101, 588: 131, 586: 131, 618: 147}  # article -> the current story it is in
KEEP_CALLS = (149,)


@dataclass
class Plan:
    deletes: dict[str, list[int]] = field(default_factory=dict)  # table -> ids, in order
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def add(self, table: str, ids: list[int]) -> None:
        current = self.deletes.setdefault(table, [])
        current.extend(i for i in ids if i not in current)


def parse(stamp: str) -> datetime:
    moment = datetime.fromisoformat(stamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def plan(conn: sqlite3.Connection, max_age: timedelta) -> Plan:
    """What to delete, every row checked first; nothing is changed here."""
    out = Plan()

    def ids(sql: str, args: tuple = ()) -> list[int]:
        return [row[0] for row in conn.execute(sql, args)]

    def old_page(article_id: int) -> bool:
        row = conn.execute(
            "SELECT published_at, first_seen_at FROM watch_articles WHERE id = ?", (article_id,)
        ).fetchone()
        return row is not None and parse(row[0]) < parse(row[1]) - max_age

    # Stories made only of old pages: the story, its articles' matches and sightings, the
    # articles, its calls with their scores, and any alert on it.
    for story_id, expected_calls in STORIES.items():
        headline = conn.execute(
            "SELECT headline FROM watch_stories WHERE id = ?", (story_id,)
        ).fetchone()
        if headline is None:
            out.notes.append(f"story #{story_id}: already gone")
            continue
        articles = ids("SELECT id FROM watch_articles WHERE story_id = ?", (story_id,))
        current = [a for a in articles if not old_page(a)]
        if current:
            out.problems.append(f"story #{story_id} holds articles that aren't old news: {current}")
        filings = ids("SELECT id FROM watch_filings WHERE story_id = ?", (story_id,))
        if filings:
            out.problems.append(f"story #{story_id} holds filings {filings}: not deleting a filing")
        calls = ids("SELECT id FROM watch_calls WHERE story_id = ?", (story_id,))
        if sorted(calls) != sorted(expected_calls):
            out.problems.append(
                f"story #{story_id} holds calls {calls}, expected {list(expected_calls)}"
            )
        if set(calls) & set(KEEP_CALLS):
            out.problems.append(f"story #{story_id} holds a call that stays: {KEEP_CALLS}")
        marks = ",".join("?" * len(calls)) or "NULL"
        out.add(
            "watch_scores",
            ids(f"SELECT id FROM watch_scores WHERE call_id IN ({marks})", tuple(calls)),
        )
        out.add("watch_calls", calls)
        out.add("watch_alerts", ids("SELECT id FROM watch_alerts WHERE story_id = ?", (story_id,)))
        out.add("watch_articles", articles)
        out.add("watch_stories", [story_id])
        out.notes.append(
            f"story #{story_id} ({headline[0][:70]}): {len(articles)} articles, calls {calls}"
        )

    # The two score rows named: on call 140, which the story above takes anyway.
    for score_id in SCORES:
        row = conn.execute("SELECT call_id FROM watch_scores WHERE id = ?", (score_id,)).fetchone()
        if row is None:
            out.notes.append(f"score {score_id}: already gone")
        elif row[0] != 140:
            out.problems.append(f"score {score_id} is on call {row[0]}, expected 140")
        else:
            out.add("watch_scores", [score_id])

    # Old pages inside current stories: only the article and what hangs off it.
    for article_id, story_id in OLD_PAGES.items():
        row = conn.execute(
            "SELECT story_id, title FROM watch_articles WHERE id = ?", (article_id,)
        ).fetchone()
        if row is None:
            out.notes.append(f"article {article_id}: already gone")
            continue
        if row[0] != story_id:
            out.problems.append(f"article {article_id} is in story #{row[0]}, expected #{story_id}")
        if not old_page(article_id):
            out.problems.append(f"article {article_id} isn't old news")
        out.add("watch_articles", [article_id])
        out.notes.append(f"article {article_id} in #{story_id}: {row[1][:70]}")

    articles = out.deletes.get("watch_articles", [])
    marks = ",".join("?" * len(articles)) or "NULL"
    out.add(
        "watch_matches",
        ids(f"SELECT id FROM watch_matches WHERE article_id IN ({marks})", tuple(articles)),
    )
    out.add(
        "watch_sightings",
        ids(f"SELECT id FROM watch_sightings WHERE article_id IN ({marks})", tuple(articles)),
    )

    for call_id in KEEP_CALLS:
        if call_id in out.deletes.get("watch_calls", []):
            out.problems.append(f"call {call_id} would be deleted, and it stays")
    return out


# Dependents first: nothing is deleted while something still points at it.
ORDER = (
    "watch_scores",
    "watch_alerts",
    "watch_calls",
    "watch_matches",
    "watch_sightings",
    "watch_articles",
    "watch_stories",
)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="back up, then delete")
    parser.add_argument(
        "--database",
        type=Path,
        help="another database (a copy, to try it on); default the live one",
    )
    args = parser.parse_args()
    settings = load_settings()
    database = args.database or settings.resolve_path(settings.paths.database)
    max_age = timedelta(days=settings.watch.catch_up_max_days)

    source = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    planned = plan(source, max_age)
    for note in planned.notes:
        print("  " + note)
    total = 0
    for table in ORDER:
        rows = planned.deletes.get(table, [])
        total += len(rows)
        print(f"{table}: {len(rows)} to delete {rows}")
    if planned.problems:
        print("\nNot deleting anything:")
        for problem in planned.problems:
            print("  - " + problem)
        return 1
    if not args.apply:
        print(f"\nDry run: {total} rows would be deleted. Run with --apply, the scanner stopped.")
        return 0
    if not total:
        print("\nNothing to delete.")
        return 0

    backups = database.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / f"newsdesk.{datetime.now():%Y%m%d-%H%M%S}.before-stale-cleanup.db"
    copy = sqlite3.connect(backup)
    source.backup(copy)  # consistent, WAL included
    copy.close()
    source.close()
    print(f"\nBacked up to {backup}")

    conn = sqlite3.connect(database, timeout=10, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=ON")  # a missed dependent fails the transaction
        conn.execute("BEGIN IMMEDIATE")
        for table in ORDER:
            rows = planned.deletes.get(table, [])
            if rows:
                marks = ",".join("?" * len(rows))
                deleted = conn.execute(f"DELETE FROM {table} WHERE id IN ({marks})", rows).rowcount
                if deleted != len(rows):
                    raise RuntimeError(f"{table}: deleted {deleted}, expected {len(rows)}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    print(f"Deleted {total} rows in one transaction.")
    integrity = conn.execute("PRAGMA integrity_check").fetchall()
    dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    print(f"integrity_check: {', '.join(row[0] for row in integrity)}")
    print(f"foreign_key_check: {dangling if dangling else 'no problems'}")
    return 0 if integrity == [("ok",)] and not dangling else 1


if __name__ == "__main__":
    sys.exit(main())
