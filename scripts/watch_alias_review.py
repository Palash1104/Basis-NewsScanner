"""Review the watchlist's aliases against real headlines: every keep, mention and drop.

Run it after any edit to config/watchlist.yaml. It reads three sources of headlines:
  - every article BASIS has stored (general news, weeks of it)
  - the current entries of config/watch_feeds.yaml (the business feeds the fast scan reads)
  - a 30-day Google News search for every alias AND every exclusion of every stock, so the
    corpus holds the lookalikes too ("HAL" the name, "Tejas Networks", "Aequs Foundation")

and writes data/watch_alias_review.md: per stock, every headline the matcher touched, with its
verdict and why. The Google News results are cached in data/watch_corpus.json, so a re-run
after editing aliases costs nothing; pass --refresh to fetch again (one request per alias).

Usage:
    uv run python scripts/watch_alias_review.py [--refresh]
"""

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import FeedConfig, WatchItem, load_feeds, load_settings, load_watchlist
from app.db import make_engine, make_session_factory
from app.models import Article
from app.net import make_client
from app.pipeline.fetch import SourceResolver, fetch_all
from app.watch.match import Matcher

sys.stdout.reconfigure(encoding="utf-8")

WATCH_FEEDS = Path("config/watch_feeds.yaml")
VERDICTS = ("keep", "mention", "drop")


def _queries(items: list[WatchItem]) -> list[FeedConfig]:
    """One Google News search per alias and per exclusion, last 30 days."""
    terms: dict[str, None] = {}
    for item in items:
        if item.type != "stock":
            continue
        for term in [*item.aliases.strong, *item.aliases.weak, *item.exclude]:
            terms.setdefault(term, None)
    return [
        FeedConfig(
            name=f"Google News: {term}",
            url=f"https://news.google.com/rss/search?q=%22{quote_plus(term)}%22+when:30d"
            "&hl=en-IN&gl=IN&ceid=IN:en",
            region="IN",
            weight=1,
        )
        for term in terms
    ]


async def _fetch(feeds: list[FeedConfig], settings, resolver: SourceResolver) -> list[dict]:
    async with make_client(settings.http) as client:
        results = await fetch_all(feeds, settings, resolver=resolver, client=client)
    rows = []
    for result in results:
        if not result.ok:
            print(f"  {result.feed.name}: {result.error}")
        for a in result.articles:
            rows.append(
                {
                    "title": a.title,
                    "snippet": a.snippet or "",
                    "source": a.source_name,
                    "published_at": a.published_at.isoformat(),
                    "from": result.feed.name,
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true", help="fetch Google News again")
    args = parser.parse_args()

    settings = load_settings()
    items = load_watchlist()
    tz = ZoneInfo(settings.timezone)
    cache = settings.resolve_path("data/watch_corpus.json")
    report = settings.resolve_path("data/watch_alias_review.md")

    rows: list[dict] = []
    engine = make_engine(settings.resolve_path(settings.paths.database))
    with make_session_factory(engine)() as session:
        for title, snippet, source, published in session.execute(
            select(Article.title, Article.snippet, Article.source_name, Article.published_at)
        ):
            rows.append(
                {
                    "title": title or "",
                    "snippet": snippet or "",
                    "source": source,
                    "published_at": published.isoformat(),
                    "from": "stored",
                }
            )
    stored = len(rows)

    watch_feeds = load_feeds(WATCH_FEEDS)
    resolver = SourceResolver([*load_feeds(include_disabled=True), *watch_feeds])
    print("fetching the watch feeds...")
    rows += asyncio.run(_fetch(watch_feeds, settings, resolver))
    from_feeds = len(rows) - stored

    if args.refresh or not cache.exists():
        queries = _queries(items)
        print(f"searching Google News for {len(queries)} aliases and exclusions...")
        found = asyncio.run(_fetch(queries, settings, resolver))
        cache.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    found = json.loads(cache.read_text(encoding="utf-8"))
    rows += found

    # One verdict per distinct headline: the same story syndicated ten times is one row.
    distinct: dict[str, dict] = {}
    for row in rows:
        key = re.sub(r"\W+", " ", row["title"]).strip().lower()
        distinct.setdefault(key, row)
    print(
        f"{len(distinct)} distinct headlines ({stored} stored, {from_feeds} from watch feeds, "
        f"{len(found)} from Google News, before de-duplicating)"
    )

    matcher = Matcher(items)
    by_stock: dict[str, dict[str, list[tuple[str, dict]]]] = {}
    for row in distinct.values():
        for match in matcher.match(row["title"], row["snippet"]):
            why = f"{match.reason}" + (f" [{match.alias}]" if match.alias else "")
            by_stock.setdefault(match.symbol, {}).setdefault(match.verdict, []).append((why, row))

    stocks = [item for item in items if item.type == "stock"]
    lines = [
        "# Watchlist alias review",
        "",
        f"Generated {datetime.now(tz):%d %b %Y %H:%M %Z} from {len(distinct)} distinct "
        f"headlines: {stored} stored by BASIS, {from_feeds} from the watch feeds, "
        f"{len(found)} from 30 days of Google News searches for every alias and exclusion.",
        "",
        "| Stock | keep | mention | drop |",
        "|---|---|---|---|",
    ]
    for item in stocks:
        v = by_stock.get(item.symbol, {})
        lines.append(
            f"| {item.name} | {len(v.get('keep', []))} | {len(v.get('mention', []))} "
            f"| {len(v.get('drop', []))} |"
        )
    for item in stocks:
        v = by_stock.get(item.symbol, {})
        lines += ["", f"## {item.name} ({item.symbol})", ""]
        for verdict in VERDICTS:
            entries = sorted(v.get(verdict, []), key=lambda e: e[1]["published_at"], reverse=True)
            reasons = Counter(why.split(" [")[0] for why, _ in entries)
            lines.append(f"### {verdict} ({len(entries)})")
            if reasons and verdict != "keep":
                lines.append("")
                lines.append("; ".join(f"{n}× {r}" for r, n in reasons.most_common()))
            lines.append("")
            for why, row in entries:
                when = datetime.fromisoformat(row["published_at"]).astimezone(tz)
                title = row["title"].replace("|", "/")
                lines.append(f"- {when:%d %b} · {row['source']} · {title} — *{why}*")
            lines.append("")
    report.write_text("\n".join(lines), encoding="utf-8")

    print()
    for item in stocks:
        v = by_stock.get(item.symbol, {})
        print(
            f"{item.name:<22} keep {len(v.get('keep', [])):3}  mention "
            f"{len(v.get('mention', [])):3}  drop {len(v.get('drop', [])):3}"
        )
    print(f"\nevery verdict: {report}")


if __name__ == "__main__":
    main()
