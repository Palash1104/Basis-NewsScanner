"""Check that feeds respond, parse, and have recent entries.

Usage:
    uv run python scripts/verify_feeds.py                     # config/feeds.yaml (enabled)
    uv run python scripts/verify_feeds.py --include-disabled
    uv run python scripts/verify_feeds.py --file candidates.yaml
"""

import argparse
import asyncio
import logging
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

from app.config import load_feeds, load_settings
from app.models import utcnow
from app.pipeline.fetch import FeedResult, SourceResolver, fetch_all


def _age_hours(result: FeedResult) -> float | None:
    if not result.articles:
        return None
    newest = max(article.published_at for article in result.articles)
    return (utcnow() - newest).total_seconds() / 3600


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--file", type=Path, default=None, help="feeds yaml (default config/feeds.yaml)"
    )
    parser.add_argument("--include-disabled", action="store_true")
    parser.add_argument("--lookback-hours", type=float, default=None)
    args = parser.parse_args()

    sys.stdout.reconfigure(
        encoding="utf-8"
    )  # outlet names may not fit the Windows console codepage
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    lookback = args.lookback_hours or settings.pipeline.lookback_hours
    feeds = load_feeds(args.file, include_disabled=args.include_disabled)
    resolver = SourceResolver(load_feeds(args.file, include_disabled=True))

    results = asyncio.run(fetch_all(feeds, settings, resolver=resolver))
    cutoff = utcnow() - timedelta(hours=lookback)

    print(f"\n{'feed':<28} {'status':>6} {'entries':>7} {'recent':>6} {'newest':>8}  result")
    print("-" * 100)
    failures = 0
    for result in results:
        recent = sum(1 for article in result.articles if article.published_at >= cutoff)
        age = _age_hours(result)
        age_text = f"{age:.1f}h" if age is not None else "-"
        if not result.ok:
            verdict = f"FAIL {result.error}"
        elif not result.articles:
            verdict = "FAIL no entries"
        elif recent == 0:
            verdict = f"STALE nothing in last {lookback:g}h"
        else:
            verdict = "OK"
        failures += verdict != "OK"
        status = result.status_code if result.status_code is not None else "-"
        print(
            f"{result.feed.name[:28]:<28} {status:>6} {len(result.articles):>7} {recent:>6} "
            f"{age_text:>8}  {verdict}"
        )
        print(f"{'':<28} {result.feed.url}")

    aggregator_outlets = Counter(
        article.source_name
        for result in results
        if result.feed.is_google_news
        for article in result.articles
    )
    if aggregator_outlets:
        print("\nGoogle News outlets (* = matched a configured outlet):")
        for outlet, count in aggregator_outlets.most_common():
            mark = "*" if resolver.is_known(outlet) else " "
            print(f"  {mark} {count:>3}  {outlet}")

    print(f"\n{len(results) - failures}/{len(results)} feeds OK")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
