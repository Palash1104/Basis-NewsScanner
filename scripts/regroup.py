"""Regroup every stored article with the embedding matcher. No LLM calls.

Stories whose article set changed and had a summary are marked `needs_resummary`: they drop out
of digests until they gain a new article in a later run and are summarized again.

Usage:
    uv run python scripts/regroup.py --dry-run   # report only, change nothing
    uv run python scripts/regroup.py
"""

import argparse
import logging
import sys

from sqlalchemy import func, select

from app.config import load_settings
from app.db import init_db, make_engine, make_session_factory
from app.models import Article, Story, utcnow
from app.pipeline.embed import load_embedder
from app.pipeline.regroup import regroup_articles


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING)

    settings = load_settings()
    embedder, reason = load_embedder(settings.grouping.embedding_model)
    if embedder is None:
        print("embedding model unavailable:", reason)
        return 1
    engine = make_engine(settings.resolve_path(settings.paths.database))
    init_db(engine)
    with make_session_factory(engine)() as session:
        by_status_before = dict(
            session.execute(select(Story.status, func.count()).group_by(Story.status)).all()
        )
        articles = session.scalars(select(Article)).all()
        report = regroup_articles(session, articles, settings, embedder, utcnow())
        by_status_after = dict(
            session.execute(select(Story.status, func.count()).group_by(Story.status)).all()
        )
        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    print(f"{'DRY RUN: nothing saved' if args.dry_run else 'saved'}")
    print(
        f"articles regrouped: {report.articles} "
        f"(non-news {report.non_news}: {report.non_news_attached} attached, "
        f"{report.non_news_left_out} left out)"
    )
    print(f"stories before: {report.stories_before} {by_status_before}")
    print(f"stories after:  {report.stories_after} {by_status_after}")
    print(
        f"kept unchanged: {report.unchanged} · changed: {report.changed} · "
        f"new: {report.created} · deleted (all articles moved): {report.deleted}"
    )
    print(f"stale (needs_resummary): {report.stale} {report.stale_ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
