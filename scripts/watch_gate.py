"""The watchlist call's quality gate: run it live on real stored stories, before it is wired
in, and write every input and answer for review.

What fails the gate (user, 2026-10-08): false relevance (a story about something else
called primary), confident calls on junk (high materiality on a routine or off-topic item),
and invented facts (a reason or summary saying what the articles don't).

Writes data/watch_gate.md (for reading) and tests/fixtures/watch_calls.json (inputs and
outputs, for the offline tests). Nothing is stored in watch_calls. The calls spend the
watch lane of the quota.

Usage:
    uv run python scripts/watch_gate.py [--limit 20] [story_id ...]
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import load_env, load_settings, load_watchlist_file
from app.db import init_db, make_engine, make_session_factory
from app.llm.client import LLMError, make_llm_client
from app.models import WatchArticle, WatchStory
from app.watch.analyse import matched_assessments, request_watch_call, story_input

sys.stdout.reconfigure(encoding="utf-8")
FIXTURES = Path("tests/fixtures/watch_calls.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("story_ids", nargs="*", type=int)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    load_env()
    settings = load_settings()
    engine = make_engine(settings.resolve_path(settings.paths.database))
    init_db(engine)
    factory = make_session_factory(engine)
    llm = make_llm_client(settings.llm, factory, settings.tz)
    stocks = {item.symbol: item for item in load_watchlist_file().stocks}
    tz = settings.tz

    with factory() as session:
        query = (
            select(WatchStory)
            .options(
                selectinload(WatchStory.articles).selectinload(WatchArticle.matches),
                selectinload(WatchStory.filings),
            )
            .order_by(WatchStory.first_seen_at)
        )
        if args.story_ids:
            query = query.where(WatchStory.id.in_(args.story_ids))
        inputs = [
            given
            for story in session.scalars(query)
            if (given := story_input(story, stocks, "gate")) is not None
        ][: args.limit]

    report = ["# Watchlist call: quality gate", ""]
    fixtures = []
    counts: Counter[str] = Counter()
    for given in inputs:
        report += [f"## Story {given.story_id}", ""]
        report.append("Companies: " + ", ".join(f"{label} [{s}]" for s, label in given.companies))
        report.append("")
        for article in given.articles:
            when = article.published_at.astimezone(tz).strftime("%d %b %H:%M")
            report.append(f"- {when} · {article.source_name} · {article.title}")
            if article.snippet:
                report.append(f"  > {article.snippet[:300]}")
        for filing in given.filings:
            when = filing.filed_at.astimezone(tz).strftime("%d %b %H:%M")
            report.append(f"- FILING {when} · {filing.subject} · {filing.description[:300]}")
        report.append("")
        try:
            output = request_watch_call(llm, given, settings)
        except LLMError as exc:
            report += [f"**ERROR**: {exc}", ""]
            counts["error"] += 1
            continue
        assessed, notes = matched_assessments(output.value, [s for s, _ in given.companies])
        report.append(f"**Summary:** {output.value.summary}")
        report.append("")
        for item in assessed:
            report.append(
                f"- **{item.symbol}**: {item.relevance} · {item.sentiment} · {item.materiality} "
                f"· {item.event_type} — {item.reason}"
            )
            counts[f"relevance {item.relevance}"] += 1
            counts[f"sentiment {item.sentiment}"] += 1
            counts[f"materiality {item.materiality}"] += 1
        report += [f"- note: {note}" for note in notes]
        report.append("")
        fixtures.append(
            {
                "story_id": given.story_id,
                "companies": given.companies,
                "articles": [
                    {
                        "source_name": a.source_name,
                        "title": a.title,
                        "snippet": a.snippet,
                        "published_at": a.published_at.isoformat(),
                    }
                    for a in given.articles
                ],
                "filings": [
                    {
                        "exchange": f.exchange,
                        "subject": f.subject,
                        "description": f.description,
                        "filed_at": f.filed_at.isoformat(),
                    }
                    for f in given.filings
                ],
                "reply": given.reply,
                "output": output.value.model_dump(),
                "model": output.model,
            }
        )

    usage = llm.usage
    summary = (
        f"{len(inputs)} stories, {usage.calls} calls, {usage.input_tokens} input / "
        f"{usage.output_tokens} output tokens. "
        + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    )
    report[1:1] = ["", summary, ""]
    out = settings.resolve_path("data/watch_gate.md")
    out.write_text("\n".join(report), encoding="utf-8")
    FIXTURES.write_text(json.dumps(fixtures, ensure_ascii=False, indent=1), encoding="utf-8")
    print(summary)
    print(f"report: {out}\nfixtures: {FIXTURES}")


if __name__ == "__main__":
    main()
