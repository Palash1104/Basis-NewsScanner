"""Tune the embedding grouping threshold on real fetched articles.

Groups three real datasets (a saved Sept 16 sample, the articles in the database, and a fresh
fetch) at several cosine-similarity thresholds, exactly as the pipeline does (incremental,
centroid-based, non-news can't start stories). Prints a summary table, checks the grouping
regressions in tests/fixtures/grouping_regressions.json, and lists borderline decisions near a
chosen threshold. Writes the details to data/embedding_report.md.

Usage:
    uv run python scripts/embedding_report.py                 # reuse the saved fresh sample
    uv run python scripts/embedding_report.py --refresh       # fetch a new one
    uv run python scripts/embedding_report.py --candidate 0.55 --pairs 15
"""

import argparse
import asyncio
import json
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.config import ROOT_DIR, load_feeds, load_settings
from app.models import utcnow
from app.pipeline.classify import is_non_news
from app.pipeline.cluster import Decision, group_embeddings
from app.pipeline.dedupe import dedupe_articles, normalize_source
from app.pipeline.embed import article_text, load_embedder
from app.pipeline.fetch import FetchedArticle, SourceResolver, fetch_all, filter_recent

THRESHOLDS = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]
FIXTURES = ROOT_DIR / "tests" / "fixtures" / "grouping_regressions.json"
FRESH_SAMPLE = ROOT_DIR / "data" / "embedding_sample.json"
REPORT = ROOT_DIR / "data" / "embedding_report.md"


@dataclass
class Item:
    key: str  # db id or url
    title: str
    snippet: str
    source_name: str
    source_region: str
    source_weight: int
    published_at: datetime

    @property
    def non_news(self) -> bool:
        return is_non_news(self.title)


def _from_fetched(articles: list[FetchedArticle]) -> list[Item]:
    return [
        Item(
            a.url,
            a.title,
            a.snippet,
            a.source_name,
            a.source_region,
            a.source_weight,
            a.published_at,
        )
        for a in articles
    ]


def load_datasets(refresh: bool) -> dict[str, list[Item]]:
    settings = load_settings()
    datasets: dict[str, list[Item]] = {}

    sample = []
    for data in json.loads((ROOT_DIR / "data" / "grouping_sample.json").read_text("utf-8")):
        values = dict(data)
        values["published_at"] = datetime.fromisoformat(values["published_at"])
        values["fetched_at"] = datetime.fromisoformat(values["fetched_at"])
        values.pop("non_news", None)
        sample.append(FetchedArticle(**values))
    datasets["Sep 16 sample"] = _from_fetched(dedupe_articles(sample, [], settings.dedupe)[0])

    rows = sqlite3.connect(ROOT_DIR / "data" / "newsdesk.db").execute(
        "select id, title, snippet, source_name, source_region, source_weight, published_at "
        "from articles"
    )
    datasets["Sep 17 DB"] = [
        Item(
            str(r[0]),
            r[1],
            r[2],
            r[3],
            r[4],
            r[5],
            datetime.fromisoformat(r[6]).replace(tzinfo=UTC),
        )
        for r in rows
    ]

    if refresh or not FRESH_SAMPLE.exists():
        feeds = load_feeds(include_disabled=True)
        results = asyncio.run(
            fetch_all([f for f in feeds if f.enabled], settings, SourceResolver(feeds))
        )
        fetched = filter_recent([a for r in results for a in r.articles], 36, utcnow())
        kept = dedupe_articles(fetched, [], settings.dedupe)[0]
        FRESH_SAMPLE.write_text(
            json.dumps(
                [
                    {
                        **a.__dict__,
                        "published_at": a.published_at.isoformat(),
                        "fetched_at": a.fetched_at.isoformat(),
                    }
                    for a in kept
                ],
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    fresh = []
    for data in json.loads(FRESH_SAMPLE.read_text("utf-8")):
        values = dict(data)
        values["published_at"] = datetime.fromisoformat(values["published_at"])
        values["fetched_at"] = datetime.fromisoformat(values["fetched_at"])
        fresh.append(FetchedArticle(**values))
    stamp = max(a.fetched_at for a in fresh).strftime("%b %d")
    datasets[f"{stamp} fresh fetch"] = _from_fetched(fresh)
    return datasets


def fixture_checks(
    embed, threshold: float, window: timedelta, seed_threshold: float | None = None
) -> dict[str, bool]:
    """The regression cases, each grouped on its own (as the tests do)."""
    fixtures = json.loads(FIXTURES.read_text("utf-8"))

    def group(case: str) -> tuple[list[dict], list[Decision]]:
        articles = fixtures[case]["articles"]
        vectors = embed([article_text(a["title"], a["snippet"]) for a in articles])
        decisions = group_embeddings(
            [datetime.fromisoformat(a["published_at"]) for a in articles],
            vectors,
            [is_non_news(a["title"]) for a in articles],
            threshold,
            window,
            seed_threshold,
        )
        return articles, decisions

    articles, decisions = group("same_event_split")
    case = fixtures["same_event_split"]
    group_of = {a["db_id"]: d.group for a, d in zip(articles, decisions, strict=True)}
    leads = {group_of[db_id] for db_id in case["must_group_together"]}
    sizes: dict[int | None, int] = {}
    for d in decisions:
        sizes[d.group] = sizes.get(d.group, 0) + 1
    lead_group = next(iter(leads))
    share = sizes[lead_group] / len(articles)
    checks = {
        "a) split event groups together": len(leads) == 1
        and share >= case["min_share_in_one_story"]
    }
    articles, decisions = group("ice_vs_rape")
    checks["b) rape case stays out of ICE story"] = decisions[0].group != decisions[1].group
    (explainer,) = fixtures["iran_sanctions_explainer"]["articles"]
    checks["c) Iran sanctions explainer flagged"] = is_non_news(explainer["title"])
    articles, decisions = group("rates_story")
    checks["d) story 3 sources stay apart"] = decisions[0].group is None or (
        decisions[0].group != decisions[1].group
    )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--candidate", type=float, default=None)
    parser.add_argument("--pairs", type=int, default=15)
    parser.add_argument("--band", type=float, default=0.06)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING)

    settings = load_settings()
    window = timedelta(hours=settings.pipeline.story_attach_window_hours)
    embedder, reason = load_embedder(settings.grouping.embedding_model)
    if embedder is None:
        print("embedding model unavailable:", reason)
        return 1

    datasets = load_datasets(args.refresh)
    vectors = {
        name: embedder.embed([article_text(i.title, i.snippet) for i in items])
        for name, items in datasets.items()
    }
    lines = ["# Embedding grouping report\n", f"Model: {embedder.name}\n"]
    for name, items in datasets.items():
        non_news = sum(i.non_news for i in items)
        lines.append(f"- {name}: {len(items)} articles ({non_news} non-news)")

    header = (
        "\n| threshold | stories | multi-source | largest | non-news attached / left out "
        "| a | b | c | d |\n|---|---|---|---|---|---|---|---|---|"
    )
    rows = []
    results: dict[float, dict[str, list[Decision]]] = {}
    for threshold in THRESHOLDS:
        per_set = {}
        stories = multi = largest = attached = left_out = 0
        for name, items in datasets.items():
            decisions = group_embeddings(
                [i.published_at for i in items],
                vectors[name],
                [i.non_news for i in items],
                threshold,
                window,
                settings.grouping.seed_threshold,
            )
            per_set[name] = decisions
            members: dict[int, list[Item]] = {}
            for decision in decisions:
                if decision.group is not None and not items[decision.index].non_news:
                    members.setdefault(decision.group, []).append(items[decision.index])
            stories += len(members)
            multi += sum(
                len({normalize_source(i.source_name) for i in group}) > 1
                for group in members.values()
            )
            largest = max(largest, max((len(g) for g in members.values()), default=0))
            attached += sum(1 for d in decisions if items[d.index].non_news and d.group is not None)
            left_out += sum(1 for d in decisions if items[d.index].non_news and d.group is None)
        results[threshold] = per_set
        checks = fixture_checks(embedder.embed, threshold, window, settings.grouping.seed_threshold)
        marks = " | ".join("✅" if ok else "❌" for ok in checks.values())
        rows.append(
            f"| {threshold:.2f} | {stories} | {multi} | {largest} | {attached} / {left_out} "
            f"| {marks} |"
        )
    lines += [header, *rows]
    lines.append(
        "\nChecks: "
        + "; ".join(fixture_checks(embedder.embed, 0.5, window, settings.grouping.seed_threshold))
    )

    if args.candidate is not None:
        threshold = args.candidate
        per_set = results.get(threshold) or {
            name: group_embeddings(
                [i.published_at for i in items],
                vectors[name],
                [i.non_news for i in items],
                threshold,
                window,
                settings.grouping.seed_threshold,
            )
            for name, items in datasets.items()
        }
        borderline = []
        for name, items in datasets.items():
            decisions = per_set[name]
            first_member = {d.group: d.index for d in decisions if d.created}
            sizes: dict[int, int] = {}
            for d in decisions:
                if d.group is not None and not items[d.index].non_news:
                    sizes[d.group] = sizes.get(d.group, 0) + 1
            for d in decisions:
                if d.best_score is None or abs(d.best_score - threshold) > args.band:
                    continue
                if d.best_group is None or d.best_group not in first_member:
                    continue
                rep = items[first_member[d.best_group]]
                borderline.append(
                    (
                        d.best_score,
                        name,
                        items[d.index],
                        rep,
                        d.group == d.best_group,
                        sizes.get(d.best_group, 0),
                    )
                )
        rng = random.Random(19)
        above = [b for b in borderline if b[4]]
        below = [b for b in borderline if not b[4]]
        rng.shuffle(above)
        rng.shuffle(below)
        half = args.pairs // 2
        chosen = sorted(above[: args.pairs - half] + below[:half], key=lambda b: b[0])
        lines.append(
            f"\n## Borderline decisions at {threshold:.2f} ({len(borderline)} within "
            f"±{args.band}; {len(chosen)} shown, fixed random sample)\n"
        )
        for n, (score, name, item, rep, joined, size) in enumerate(chosen, 1):
            kind = " [non-news]" if item.non_news else ""
            lines.append(
                f"{n}. **{score:.3f}** · {'JOINS' if joined else 'separate'} · {name}\n"
                f"   - article: {item.title} *({item.source_name})*{kind}\n"
                f"   - story:   {rep.title} *({rep.source_name}; {size} articles)*"
            )
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
