"""Compare grouping scorers and thresholds on real fetched articles.

Fetches enabled feeds once (or reuses a saved sample), dedupes, then groups the sample with
every scorer at several thresholds. Prints a summary table and writes a detailed markdown
report with the largest groups, borderline merges, and near misses.

Usage:
    uv run python scripts/grouping_report.py                  # reuse sample if present
    uv run python scripts/grouping_report.py --refresh --lookback-hours 24
    uv run python scripts/grouping_report.py --detail title_token_set:64 --limit 20
"""

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import get_args

from app.config import ROOT_DIR, GroupingScorer, load_feeds, load_settings
from app.models import utcnow
from app.pipeline.cluster import Grouper
from app.pipeline.dedupe import count_independent_sources, dedupe_articles, normalize_source
from app.pipeline.fetch import FetchedArticle, SourceResolver, fetch_all, filter_recent

DEFAULT_SAMPLE = ROOT_DIR / "data" / "grouping_sample.json"
DEFAULT_REPORT = ROOT_DIR / "data" / "grouping_report.md"
THRESHOLDS = [50, 55, 60, 65, 70, 75, 80, 85]
BORDER = 8  # points above/below threshold that count as borderline


@dataclass
class Link:
    score: float
    article: FetchedArticle
    matched: FetchedArticle


@dataclass
class Outcome:
    scorer: str
    threshold: float
    groups: list[list[FetchedArticle]]
    merges: list[Link] = field(default_factory=list)
    near_misses: list[Link] = field(default_factory=list)


def _article_to_json(article: FetchedArticle) -> dict[str, str | int]:
    data = asdict(article)
    data["published_at"] = article.published_at.isoformat()
    data["fetched_at"] = article.fetched_at.isoformat()
    return data


def _article_from_json(data: dict[str, str | int]) -> FetchedArticle:
    values = dict(data)
    values["published_at"] = datetime.fromisoformat(str(data["published_at"]))
    values["fetched_at"] = datetime.fromisoformat(str(data["fetched_at"]))
    return FetchedArticle(**values)  # type: ignore[arg-type]


def load_or_fetch_sample(path: Path, refresh: bool, lookback_hours: float) -> list[FetchedArticle]:
    if path.exists() and not refresh:
        return [_article_from_json(item) for item in json.loads(path.read_text(encoding="utf-8"))]
    settings = load_settings()
    all_feeds = load_feeds(include_disabled=True)
    feeds = [feed for feed in all_feeds if feed.enabled]
    results = asyncio.run(fetch_all(feeds, settings, resolver=SourceResolver(all_feeds)))
    articles = [article for result in results for article in result.articles]
    articles = filter_recent(articles, lookback_hours, utcnow())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([_article_to_json(a) for a in articles], indent=1), encoding="utf-8")
    return articles


def run_grouping(
    articles: list[FetchedArticle], scorer: GroupingScorer, threshold: float, window: timedelta
) -> Outcome:
    grouper: Grouper[int] = Grouper(scorer, threshold, window)
    groups: list[list[FetchedArticle]] = []
    outcome = Outcome(scorer, threshold, groups)
    for article in articles:
        match = grouper.match(article.title, article.snippet, article.published_at)
        if match.key is None:
            key = len(groups)
            groups.append([article])
            if match.best_score is not None and match.best_score >= threshold - BORDER:
                outcome.near_misses.append(Link(match.best_score, article, match.best_ref))  # type: ignore[arg-type]
        else:
            key = match.key
            groups[key].append(article)
            if match.best_score is not None and match.best_score < threshold + BORDER:
                outcome.merges.append(Link(match.best_score, article, match.best_ref))  # type: ignore[arg-type]
        grouper.add(key, article.title, article.snippet, article.published_at, ref=article)
    return outcome


def _sources(group: list[FetchedArticle]) -> int:
    return len({normalize_source(article.source_name) for article in group})


def _line(article: FetchedArticle) -> str:
    return f"{article.title} *({article.source_name})*"


def summary_row(outcome: Outcome) -> str:
    groups = outcome.groups
    multi = [g for g in groups if len(g) > 1]
    multi_source = [g for g in groups if _sources(g) > 1]
    largest = max((len(g) for g in groups), default=0)
    in_multi = sum(len(g) for g in multi)
    total = sum(len(g) for g in groups)
    return (
        f"| {outcome.scorer} | {outcome.threshold:g} | {len(groups)} | {len(multi)} | "
        f"{len(multi_source)} | {largest} | {100 * in_multi / max(total, 1):.0f}% |"
    )


def detail_section(outcome: Outcome, syndication_similarity: float, limit: int) -> list[str]:
    lines = [f"\n## {outcome.scorer} @ {outcome.threshold:g}\n"]
    ranked = sorted(outcome.groups, key=lambda g: (_sources(g), len(g)), reverse=True)
    lines.append(f"### Largest multi-source groups (top {limit})\n")
    for index, group in enumerate(ranked[:limit], start=1):
        independent = count_independent_sources(group, syndication_similarity)
        lines.append(
            f"**{index}. {len(group)} articles, {_sources(group)} sources "
            f"({independent} independent)**"
        )
        for article in group[:12]:
            lines.append(f"- {_line(article)}")
        if len(group) > 12:
            lines.append(f"- ... {len(group) - 12} more")
        lines.append("")

    lines.append(f"### Borderline merges (joined with score < {outcome.threshold + BORDER:g})\n")
    for link in sorted(outcome.merges, key=lambda item: item.score)[:limit]:
        lines.append(f"- **{link.score:.0f}** {_line(link.article)}  \n  → {_line(link.matched)}")
    lines.append(f"\n### Near misses (kept apart with score ≥ {outcome.threshold - BORDER:g})\n")
    for link in sorted(outcome.near_misses, key=lambda item: -item.score)[:limit]:
        lines.append(f"- **{link.score:.0f}** {_line(link.article)}  \n  ≠ {_line(link.matched)}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--refresh", action="store_true", help="fetch a new sample")
    parser.add_argument("--lookback-hours", type=float, default=24)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--detail", action="append", default=[], metavar="SCORER:THRESHOLD")
    parser.add_argument("--limit", type=int, default=15, help="examples per section")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    window = timedelta(hours=settings.pipeline.story_attach_window_hours)

    raw = load_or_fetch_sample(args.sample, args.refresh, args.lookback_hours)
    articles, dropped = dedupe_articles(raw, [], settings.dedupe)
    articles.sort(key=lambda item: item.published_at)

    lines = [
        "# Grouping report\n",
        f"Sample: {args.sample.name}, {len(raw)} fetched, {len(articles)} after dedupe.\n",
    ]
    drop_reasons = Counter(item.reason for item in dropped)
    lines.append("Dropped: " + (", ".join(f"{r} {n}" for r, n in drop_reasons.items()) or "none"))
    same_source = [item for item in dropped if item.reason == "same_source_similar_title"]
    if same_source:
        lines.append("\nSame-source title drops (dropped → kept):\n")
        for item in same_source[: args.limit]:
            lines.append(f"- {_line(item.article)}  \n  → {item.matched_title}")

    per_source = Counter(article.source_name for article in articles)
    lines.append(
        "\nArticles per source: " + ", ".join(f"{s} {n}" for s, n in per_source.most_common())
    )

    header = [
        "\n| scorer | threshold | stories | multi-article | multi-source | largest | grouped |",
        "|---|---|---|---|---|---|---|",
    ]
    rows: list[str] = []
    outcomes: dict[tuple[str, float], Outcome] = {}
    for scorer in get_args(GroupingScorer):
        for threshold in THRESHOLDS:
            outcome = run_grouping(articles, scorer, threshold, window)
            outcomes[(scorer, threshold)] = outcome
            rows.append(summary_row(outcome))
    lines += header + rows

    details: dict[str, list[float]] = defaultdict(list)
    for item in args.detail:
        scorer, _, threshold = item.partition(":")
        details[scorer].append(float(threshold))
    for scorer, thresholds in details.items():
        for threshold in thresholds:
            outcome = outcomes.get((scorer, threshold)) or run_grouping(
                articles,
                scorer,
                threshold,
                window,  # type: ignore[arg-type]
            )
            lines += detail_section(
                outcome, settings.dedupe.syndication_title_similarity, args.limit
            )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:4]))
    print("\n".join(header + rows))
    print(f"\nFull report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
