"""Newsdesk command line (SPEC 12). Phase 1: `newsdesk run` and `newsdesk digest`."""

import asyncio
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Annotated, Any

import httpx
import typer
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import FeedConfig, Settings, get_secret, load_env, load_feeds, load_settings
from app.db import init_db, make_engine, make_session_factory
from app.delivery.format import digest_item, format_digest, telegram_length
from app.delivery.telegram import TelegramError, send_messages
from app.llm.client import LLMClient, LLMConfigError, make_llm_client
from app.models import Article, Run, Story, utcnow
from app.net import make_client
from app.pipeline.cluster import assign_to_stories
from app.pipeline.dedupe import dedupe_articles
from app.pipeline.fetch import FeedResult, SourceResolver, fetch_all, filter_recent
from app.pipeline.rank import rank_stories
from app.pipeline.summarize import summarize_stories

log = logging.getLogger("newsdesk")

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Personal news digest with plain-language summaries and market impact notes.",
)

URL_QUERY_CHUNK = 500


# ---------------------------------------------------------------- pipeline


@dataclass
class PipelineReport:
    run_id: int
    feeds_ok: int = 0
    feeds_failed: int = 0
    articles_fetched: int = 0
    articles_recent: int = 0
    articles_new: int = 0
    duplicates_dropped: int = 0
    stories_created: int = 0
    articles_attached: int = 0
    stories_ranked: int = 0
    summarized: int = 0
    skipped_unchanged: int = 0
    failed: int = 0
    llm_call_errors: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


async def _fetch(
    feeds: Sequence[FeedConfig],
    settings: Settings,
    resolver: SourceResolver,
    transport: httpx.AsyncBaseTransport | None,
) -> list[FeedResult]:
    async with make_client(settings.http, transport=transport) as client:
        return await fetch_all(feeds, settings, resolver=resolver, client=client)


def _existing_articles(session: Session, urls: set[str], since: datetime) -> list[Article]:
    """Stored articles the dedupe step must see: recent ones, plus any sharing a candidate URL."""
    found: dict[int, Article] = {
        article.id: article
        for article in session.scalars(select(Article).where(Article.published_at >= since))
    }
    url_list = sorted(urls)
    for start in range(0, len(url_list), URL_QUERY_CHUNK):
        chunk = url_list[start : start + URL_QUERY_CHUNK]
        for article in session.scalars(select(Article).where(Article.url.in_(chunk))):
            found[article.id] = article
    return list(found.values())


def run_pipeline(
    session_factory: sessionmaker[Session],
    settings: Settings,
    feeds: Sequence[FeedConfig],
    llm: LLMClient | None,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    llm_unavailable: str | None = None,
) -> PipelineReport:
    """One pass: fetch → dedupe → group → rank → summarize. `feeds` includes disabled feeds
    (their names still resolve Google News outlets); only enabled feeds are fetched.
    With `llm` None, summaries are skipped and `llm_unavailable` is recorded as the reason."""
    now = now or utcnow()
    with session_factory() as session:
        run = Run(kind="pipeline", started_at=utcnow(), errors=[])
        session.add(run)
        session.commit()
        report = PipelineReport(run_id=run.id)
        stage = "fetch"
        try:
            enabled = [feed for feed in feeds if feed.enabled]
            results = asyncio.run(_fetch(enabled, settings, SourceResolver(feeds), transport))
            for result in results:
                if result.ok:
                    report.feeds_ok += 1
                else:
                    report.feeds_failed += 1
                    report.errors.append(
                        {
                            "stage": "fetch",
                            "feed": result.feed.name,
                            "url": result.feed.url,
                            "error": result.error,
                        }
                    )
            fetched = [article for result in results for article in result.articles]
            recent = filter_recent(fetched, settings.pipeline.lookback_hours, now)
            report.articles_fetched = len(fetched)
            report.articles_recent = len(recent)

            stage = "dedupe"
            since = now - timedelta(
                hours=settings.pipeline.lookback_hours + settings.dedupe.same_source_window_hours
            )
            existing = _existing_articles(session, {article.url for article in recent}, since)
            kept, dropped = dedupe_articles(recent, existing, settings.dedupe)
            report.duplicates_dropped = len(dropped)
            new_articles = [
                Article(
                    url=article.url,
                    source_name=article.source_name,
                    source_region=article.source_region,
                    source_weight=article.source_weight,
                    title=article.title,
                    snippet=article.snippet,
                    published_at=article.published_at,
                    fetched_at=article.fetched_at,
                )
                for article in kept
            ]
            session.add_all(new_articles)
            session.flush()
            report.articles_new = len(new_articles)

            stage = "group"
            grouping = assign_to_stories(session, new_articles, settings, now)
            report.stories_created = grouping.created
            report.articles_attached = grouping.attached
            session.commit()

            stage = "rank"
            top = rank_stories(session, settings, now)
            report.stories_ranked = len(top)
            session.commit()

            stage = "summarize"
            if llm is None:
                reason = llm_unavailable or "no LLM client configured"
                report.errors.append({"stage": "summarize", "error": f"skipped: {reason}"})
            else:
                summary = summarize_stories(session, top, llm, settings, now)
                report.summarized = len(summary.summarized)
                report.skipped_unchanged = summary.skipped_unchanged
                report.failed = len(summary.failed)
                report.llm_call_errors = len(summary.call_errors)
                for story_id, error in summary.failed:
                    report.errors.append(
                        {
                            "stage": "summarize",
                            "story_id": story_id,
                            "status": "failed",
                            "error": error,
                        }
                    )
                for story_id, error in summary.call_errors:
                    report.errors.append(
                        {"stage": "summarize", "story_id": story_id, "error": error}
                    )
                if summary.stopped:
                    report.errors.append(
                        {"stage": "summarize", "error": f"stopped early: {summary.stopped}"}
                    )
        except Exception as exc:
            session.rollback()
            report.errors.append({"stage": stage, "error": f"{type(exc).__name__}: {exc}"})
            log.exception("pipeline run %d crashed during %s", run.id, stage)
            raise
        finally:
            if llm is not None:
                report.llm_calls = llm.usage.calls
                report.input_tokens = llm.usage.input_tokens
                report.output_tokens = llm.usage.output_tokens
            run.finished_at = utcnow()
            run.articles_fetched = report.articles_fetched
            run.stories_processed = report.summarized
            run.input_tokens = report.input_tokens
            run.output_tokens = report.output_tokens
            run.errors = report.errors
            session.commit()
    return report


# ---------------------------------------------------------------- digest


@dataclass
class DigestReport:
    stories: int
    messages: list[str]
    sent: bool = False
    since: datetime | None = None


def last_digest_sent_at(session: Session) -> datetime | None:
    """Start time of the most recent digest that was delivered without errors."""
    runs = session.scalars(
        select(Run).where(Run.kind == "digest").order_by(Run.started_at.desc()).limit(50)
    )
    return next((run.started_at for run in runs if not run.errors), None)


def select_digest_stories(
    session: Session, settings: Settings, now: datetime
) -> tuple[list[Story], datetime]:
    """Summarized stories whose summary was written since the last sent digest (or within the
    lookback window if none was sent yet), most important first."""
    since = last_digest_sent_at(session) or now - timedelta(hours=settings.pipeline.lookback_hours)
    stories = session.scalars(
        select(Story)
        .where(Story.status == "summarized", Story.updated_at > since)
        .order_by(Story.importance_score.desc())
        .limit(settings.delivery.max_stories_per_digest)
    ).all()
    return list(stories), since


def run_digest(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    send: bool,
    token: str | None = None,
    chat_id: str | None = None,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DigestReport:
    """Build the digest; with `send`, deliver it and record a digest run. A dry run records
    nothing, so it never moves the "since last digest" window."""
    now = now or utcnow()
    with session_factory() as session:
        stories, since = select_digest_stories(session, settings, now)
        items = [digest_item(story) for story in stories]
        messages = format_digest(items, now, settings.tz)
        report = DigestReport(stories=len(items), messages=messages, since=since)
        if not send or not items:
            return report
        if not token or not chat_id:
            raise TelegramError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set to send")

        run = Run(kind="digest", started_at=now, errors=[])
        try:
            asyncio.run(send_messages(messages, token, chat_id, settings.http, transport=transport))
            run.stories_processed = len(items)
            report.sent = True
        except TelegramError as exc:
            run.errors = [{"stage": "telegram", "error": str(exc)}]
            raise
        finally:
            run.finished_at = utcnow()
            session.add(run)
            session.commit()
    return report


# ---------------------------------------------------------------- commands


def setup_logging(settings: Settings) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # the Windows console codepage can't print ₹
    log_dir = settings.resolve_path(settings.paths.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        log_dir / "newsdesk.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [console, file_handler]
    root.setLevel(logging.INFO)
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _bootstrap() -> tuple[Settings, sessionmaker[Session]]:
    load_env()
    settings = load_settings()
    setup_logging(settings)
    engine = make_engine(settings.resolve_path(settings.paths.database))
    init_db(engine)
    return settings, make_session_factory(engine)


@app.command()
def run() -> None:
    """One full pipeline pass: fetch, dedupe, group, rank, summarize."""
    settings, session_factory = _bootstrap()
    feeds = load_feeds(include_disabled=True)
    llm: LLMClient | None = None
    llm_unavailable: str | None = None
    try:
        llm = make_llm_client(settings.llm, session_factory)
    except LLMConfigError as exc:
        llm_unavailable = str(exc)
    report = run_pipeline(session_factory, settings, feeds, llm, llm_unavailable=llm_unavailable)

    typer.echo(
        f"run {report.run_id}: feeds {report.feeds_ok} ok / {report.feeds_failed} failed · "
        f"articles fetched {report.articles_fetched}, in last "
        f"{settings.pipeline.lookback_hours}h {report.articles_recent}, "
        f"new {report.articles_new} ({report.duplicates_dropped} duplicates dropped)"
    )
    typer.echo(
        f"stories: {report.stories_created} new, {report.articles_attached} articles attached "
        f"to existing · top {report.stories_ranked} ranked · summarized {report.summarized}, "
        f"unchanged {report.skipped_unchanged}, failed {report.failed}, "
        f"LLM errors {report.llm_call_errors}"
    )
    typer.echo(
        f"LLM ({settings.llm.provider}, {settings.llm.summary_model}): {report.llm_calls} calls, "
        f"tokens {report.input_tokens} input, {report.output_tokens} output"
    )
    for error in report.errors:
        typer.echo(f"  error: {error}")


@app.command()
def digest(
    send: Annotated[bool, typer.Option("--send", help="Deliver to Telegram.")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the digest instead of sending (default).")
    ] = False,
) -> None:
    """Build the digest of stories summarized since the last sent digest."""
    if send and dry_run:
        raise typer.BadParameter("use either --send or --dry-run, not both")
    settings, session_factory = _bootstrap()
    try:
        report = run_digest(
            session_factory,
            settings,
            send=send,
            token=get_secret("TELEGRAM_BOT_TOKEN"),
            chat_id=get_secret("TELEGRAM_CHAT_ID"),
        )
    except TelegramError as exc:
        typer.echo(f"digest not sent: {exc}", err=True)
        raise typer.Exit(code=1) from None

    since = report.since.astimezone(settings.tz).strftime("%d %b %H:%M %Z") if report.since else "-"
    if report.stories == 0:
        typer.echo(f"No summarized stories since {since}; nothing to {'send' if send else 'show'}.")
        return
    if send:
        typer.echo(f"Sent {report.stories} stories in {len(report.messages)} message(s).")
        return
    for index, message in enumerate(report.messages, start=1):
        typer.echo(
            f"----- message {index}/{len(report.messages)} "
            f"({telegram_length(message)} chars, since {since}) -----"
        )
        typer.echo(message)
