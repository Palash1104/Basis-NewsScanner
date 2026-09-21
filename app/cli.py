"""Newsdesk command line (SPEC 12): `newsdesk run`, `digest`, `scheduler` and
`validate-tickers`."""

import asyncio
import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.assets import (
    REPORT_HEADER,
    Fetch,
    TickerResult,
    latest_checks,
    markdown_report,
    record_checks,
    report_rows,
    validate_assets,
    validation_gaps,
    validation_warning,
    yahoo_fetch,
)
from app.config import (
    AssetConfig,
    FeedConfig,
    Settings,
    get_secret,
    load_assets,
    load_env,
    load_feeds,
    load_settings,
)
from app.db import init_db, make_engine, make_session_factory
from app.delivery.format import digest_item, format_digest, telegram_length
from app.delivery.telegram import TelegramError, send_messages
from app.health import DEFAULT_DAYS as HEALTH_DAYS
from app.health import health_lines
from app.llm.client import (
    LLMClient,
    LLMConfigError,
    LLMError,
    LLMQuotaError,
    make_llm_client,
)
from app.llm.prompts import IMPACT_PROMPT_VERSION
from app.locks import job_lock
from app.models import Article, RuleDisagreementRow, Run, Story, utcnow
from app.net import make_client
from app.pipeline.classify import non_news_reason
from app.pipeline.cluster import GroupingResult, assign_to_stories
from app.pipeline.dedupe import dedupe_articles
from app.pipeline.embed import Embedder, load_embedder
from app.pipeline.extract_event import extract_events, pending_event_stories
from app.pipeline.fetch import FeedResult, SourceResolver, fetch_all, filter_recent
from app.pipeline.impact_llm import (
    matched_impacts,
    request_impacts,
    validate_impacts,
)
from app.pipeline.merge_impacts import merge_impacts
from app.pipeline.playbook import (
    Rule,
    load_playbook,
    maps_to_impacts,
    matching_rules,
    store_calls,
    stories_awaiting_impacts,
)
from app.pipeline.prices import (
    DAILY,
    DAILY_LEAD,
    PriceProvider,
    PriceReport,
    YahooPrices,
    cached_bars,
    impacts_to_price,
    label_for,
    price_impacts,
)
from app.pipeline.rank import pending_stories, rank_stories, rerank_stories
from app.pipeline.scoring import (
    ScoreReport,
    mark_unreferenced,
    score_impacts,
    story_track_line,
    track_record,
    unpriced_impacts,
)
from app.pipeline.summarize import summarize_stories
from app.schedule import pipeline_hours

log = logging.getLogger("newsdesk")

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Personal news digest with plain-language summaries and market impact notes.",
)

URL_QUERY_CHUNK = 500
DIGEST_STATUSES = ("summarized", "analyzed")


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
    grouping_method: str = ""
    non_news_attached: int = 0
    non_news_ungrouped: int = 0
    # Every non-news headline this run, so false positives are visible (never silent).
    non_news: list[dict[str, Any]] = field(default_factory=list)
    borderline_logged: int = 0
    pending_carried: int = 0  # stories skipped for quota on an earlier run, done first this run
    skipped_quota: int = 0  # stories left pending because the quota ran out this run
    events_extracted: int = 0
    events_failed: int = 0  # invalid output after retry: no event until the next re-summary
    event_call_errors: int = 0  # API errors: extracted next run
    events_pending_carried: int = 0
    events_skipped_quota: int = 0
    stories_analyzed: int = 0  # event extracted and the playbook applied
    impacts_created: int = 0
    reranked: int = 0  # stories the reasoning model reordered
    llm_impact_calls: int = 0
    llm_impact_declines: int = 0  # calls where the model saw nothing worth calling
    llm_impacts_added: int = 0  # calls the playbook didn't have
    llm_disagreements: int = 0
    # Invented symbols, capped confidences and anything else layer B had thrown out.
    llm_notes: list[str] = field(default_factory=list)
    impacts_priced: int = 0  # impacts that got a reference price this run
    impacts_refreshed: int = 0  # impacts whose move so far was updated
    impacts_waiting: int = 0  # market hasn't opened since the story; priced after the open
    price_symbols: int = 0
    price_bars_stored: int = 0
    price_unusable: dict[str, int] = field(default_factory=dict)  # reason -> symbols
    impact_rules: dict[str, int] = field(default_factory=dict)  # rule id -> impacts written
    # Unmapped country names and other fixes to extracted events, shown in the run output.
    event_notes: list[str] = field(default_factory=list)
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
    rules: Sequence[Rule] | None = None,
    prices: PriceProvider | None = None,
    llm_unavailable: str | None = None,
    embedder: Embedder | None = None,
    embedder_unavailable: str | None = None,
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
                    non_news=article.non_news,
                )
                for article in kept
            ]
            session.add_all(new_articles)
            session.flush()
            report.articles_new = len(new_articles)

            stage = "group"
            grouping = assign_to_stories(session, new_articles, settings, now, embedder)
            report.grouping_method = grouping.method
            report.non_news_attached = grouping.non_news_attached
            report.non_news_ungrouped = grouping.non_news_ungrouped
            if settings.grouping.method == "embedding" and grouping.method != "embedding":
                reason = embedder_unavailable or "no embedder given"
                report.errors.append(
                    {
                        "stage": "group",
                        "error": f"embedding model unavailable ({reason}); used the title matcher",
                    }
                )
            report.stories_created = grouping.created
            report.articles_attached = grouping.attached
            session.commit()  # assigns story ids for the logs below
            report.non_news = non_news_entries(grouping)
            for entry in report.non_news:
                log.info("non-news %s", entry)
            report.borderline_logged = log_borderline(grouping, settings, report.run_id)

            stage = "rank"
            candidates_wanted = max(
                settings.pipeline.rerank_candidates, settings.pipeline.max_stories_per_run
            )
            top = rank_stories(session, settings, now, limit=candidates_wanted)
            if llm is not None and settings.pipeline.rerank_candidates:
                # SPEC 7.4: the reasoning model decides what matters most, which then decides
                # what gets summarized and which stories layer B spends a call on.
                top, note = rerank_stories(llm, top, settings, settings.pipeline.rerank_candidates)
                report.reranked = min(len(top), settings.pipeline.rerank_candidates)
                if note:
                    report.errors.append({"stage": "rank", "error": note})
            top = top[: settings.pipeline.max_stories_per_run]
            report.stories_ranked = len(top)
            # Summaries skipped for quota on an earlier run go first, even if no longer top-N.
            pending = pending_stories(session, settings, now)
            report.pending_carried = len(pending)
            candidates = pending + [story for story in top if story not in pending]
            session.commit()

            stage = "summarize"
            # Extractions still owed from earlier runs, before this run adds its own.
            carried = {story.id for story in pending_event_stories(session, settings, now)}
            if llm is None:
                reason = llm_unavailable or "no LLM client configured"
                report.errors.append({"stage": "summarize", "error": f"skipped: {reason}"})
            else:
                summary = summarize_stories(session, candidates, llm, settings, now)
                report.skipped_quota = len(summary.skipped_quota)
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
                        {
                            "stage": "summarize",
                            "error": f"stopped early: {summary.stopped}",
                            "left_for_next_run": summary.skipped_quota,
                        }
                    )

                stage = "extract"
                # Summarizing marks a story event_pending, so this covers both the stories
                # summarized just now and any extraction still owed from an earlier run.
                to_extract = pending_event_stories(session, settings, now)
                report.events_pending_carried = len(carried & {s.id for s in to_extract})
                extraction = extract_events(session, to_extract, llm, settings, now)
                report.events_extracted = len(extraction.extracted)
                report.events_failed = len(extraction.failed)
                report.event_call_errors = len(extraction.call_errors)
                report.events_skipped_quota = len(extraction.skipped_quota)
                report.event_notes = extraction.notes
                for story_id, error in extraction.failed + extraction.call_errors:
                    report.errors.append({"stage": "extract", "story_id": story_id, "error": error})
                if extraction.stopped:
                    report.errors.append(
                        {
                            "stage": "extract",
                            "error": f"stopped early: {extraction.stopped}",
                            "left_for_next_run": extraction.skipped_quota,
                        }
                    )

                stage = "impacts"
                playbook = rules
                if playbook is None:
                    try:
                        playbook = load_playbook(assets=load_assets())
                    except Exception as exc:  # a broken playbook must not stop the run
                        playbook = []
                        report.errors.append(
                            {"stage": "impacts", "error": f"playbook not loaded: {exc}"}
                        )
                assets = _assets(report.errors)
                # Layer B runs on the most significant stories of this run (after the
                # rerank), and only on those the playbook step would map at all.
                rank_of = {story.id: index for index, story in enumerate(top)}
                # This run's extractions, plus any story whose event never got analyzed
                # because an earlier run stopped between the two steps.
                waiting = stories_awaiting_impacts(
                    session, now - timedelta(hours=settings.pipeline.lookback_hours)
                )
                analyzed = sorted(
                    {*extraction.extracted, *(story.id for story in waiting)},
                    key=lambda i: rank_of.get(i, 10**6),
                )
                llm_budget = settings.impacts.llm_max_stories_per_run if assets else 0
                layer_b = settings.llm.provenance(settings.llm.summary_model, IMPACT_PROMPT_VERSION)
                for story_id in analyzed:
                    story = session.get(Story, story_id)
                    if story is None or story.latest_event is None:
                        continue
                    event = story.latest_event
                    matched = matching_rules(playbook, event)
                    calls_in = matched_impacts(matched)
                    disagreements: list[Any] = []
                    model_calls: list[Any] = []
                    if llm_budget and maps_to_impacts(event):
                        llm_budget -= 1
                        try:
                            output = request_impacts(
                                llm, story, event, calls_in, list(assets.values()), settings
                            )
                            checked = validate_impacts(
                                output.value,
                                assets,
                                {rule.id for rule in matched},
                                settings.impacts.max_impacts_per_story,
                            )
                            model_calls = checked.impacts
                            disagreements = checked.disagreements
                            report.llm_impact_calls += 1
                            report.llm_impact_declines += checked.no_clear_impact
                            report.llm_notes += [
                                f"story {story_id}: {note}" for note in checked.dropped
                            ]
                        except LLMQuotaError as exc:
                            llm_budget = 0
                            report.errors.append(
                                {"stage": "impacts", "error": f"llm layer stopped: {exc}"}
                            )
                        except LLMError as exc:
                            report.errors.append(
                                {"stage": "impacts", "story_id": story_id, "error": str(exc)}
                            )
                    merged = merge_impacts(
                        calls_in, model_calls, {d.rule_id for d in disagreements}
                    )
                    created = store_calls(session, story, event, merged, now, layer_b)
                    for disagreement in disagreements:
                        session.add(
                            RuleDisagreementRow(
                                story_id=story.id,
                                event_id=event.id,
                                rule_id=disagreement.rule_id,
                                reason=disagreement.reason,
                                model=layer_b.model,
                                prompt_version=layer_b.prompt_version,
                                temperature=layer_b.temperature,
                                seed=layer_b.seed,
                                created_at=now,
                            )
                        )
                    report.llm_disagreements += len(disagreements)
                    report.stories_analyzed += 1
                    report.impacts_created += len(created)
                    report.llm_impacts_added += sum(
                        1 for impact in created if impact.origin == "llm"
                    )
                    for impact in created:
                        if impact.rule_id:
                            report.impact_rules[impact.rule_id] = (
                                report.impact_rules.get(impact.rule_id, 0) + 1
                            )
                    # Commit before the next story's LLM call: holding a write transaction
                    # across a network call locks the database against the rate limiter,
                    # which writes from its own session.
                    session.commit()

                stage = "prices"
                # New impacts need a reference price; the ones heading for the next digest
                # need their move kept current. Without a provider the step is skipped, so
                # nothing reaches the network unless a caller asks for it.
                since = last_digest_sent_at(session) or now - timedelta(
                    hours=settings.pipeline.lookback_hours
                )
                priced = price_impacts(
                    session,
                    impacts_to_price(session, since) if prices else [],
                    _assets(report.errors),
                    prices,
                    settings,
                    now,
                )
                report.impacts_priced = priced.priced
                report.impacts_refreshed = priced.refreshed
                report.impacts_waiting = priced.waiting
                report.price_symbols = priced.symbols
                report.price_bars_stored = priced.bars_stored
                report.price_unusable = priced.reasons
                for symbol, reason in priced.unusable:
                    report.errors.append({"stage": "prices", "symbol": symbol, "error": reason})
                session.commit()
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
            run.llm_impact_calls = report.llm_impact_calls
            run.llm_impact_declines = report.llm_impact_declines
            run.errors = report.errors
            session.commit()
    return report


def non_news_entries(grouping: GroupingResult) -> list[dict[str, Any]]:
    entries = []
    for placement in grouping.placements:
        if not placement.article.non_news:
            continue
        story = placement.story
        entries.append(
            {
                "title": placement.article.title,
                "source": placement.article.source_name,
                "reason": non_news_reason(placement.article.title),
                "decision": placement.decision,
                "story_id": story.id if story else None,
                "story_headline": story.headline if story else None,
            }
        )
    return entries


def log_borderline(grouping: GroupingResult, settings: Settings, run_id: int) -> int:
    """Append every embedding match scoring inside grouping.borderline_log_range, and every
    article the seed check kept out of its best story, to data/logs/grouping_borderline.jsonl,
    for retuning the thresholds later."""
    if grouping.method != "embedding":
        return 0
    low, high = settings.grouping.borderline_log_range
    rows = []
    for placement in grouping.placements:
        score = placement.score
        if score is None or not (low <= score <= high or placement.seed_rejected):
            continue
        article, best = placement.article, placement.best_story
        rows.append(
            {
                "logged_at": utcnow().isoformat(),
                "run_id": run_id,
                "threshold": settings.grouping.embedding_threshold,
                "seed_threshold": settings.grouping.seed_threshold,
                "score": round(score, 4),
                "seed_score": (
                    None if placement.seed_score is None else round(placement.seed_score, 4)
                ),
                "seed_rejected": placement.seed_rejected,
                "decision": placement.decision,
                "article": {
                    "title": article.title,
                    "source": article.source_name,
                    "url": article.url,
                    "published_at": article.published_at.isoformat(),
                    "non_news": bool(article.non_news),
                },
                "nearest_story": {
                    "id": best.id if best else None,
                    "headline": best.headline if best else None,
                    "articles": len(best.articles) if best else None,
                },
            }
        )
    if rows:
        path = settings.resolve_path(settings.paths.log_dir) / "grouping_borderline.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


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
        # "analyzed": event extracted and playbook applied. "summarized" covers
        # stories whose extraction failed or hasn't run yet.
        .where(Story.status.in_(DIGEST_STATUSES), Story.updated_at > since)
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
        assets = _assets()
        labels = move_labels(session, stories, assets, settings, now)
        rules = track_record(session, "rule_id")
        names = {asset.symbol: asset.display_name for asset in assets.values()}
        items = [
            digest_item(
                story,
                assets,
                settings.delivery.max_impacts_in_digest,
                labels,
                story_track_line(story, rules, settings.scoring.min_samples_to_show_rate, names),
            )
            for story in stories
        ]
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


def _llm_client(
    settings: Settings, session_factory: sessionmaker[Session]
) -> tuple[LLMClient | None, str | None]:
    try:
        return make_llm_client(settings.llm, session_factory, settings.tz), None
    except LLMConfigError as exc:
        return None, str(exc)


def _assets(errors: list[dict[str, Any]] | None = None) -> dict[str, AssetConfig]:
    """The asset universe by symbol; a broken assets.yaml is recorded, never fatal."""
    try:
        return {asset.symbol: asset for asset in load_assets()}
    except Exception as exc:
        log.warning("assets.yaml could not be loaded: %s", exc)
        if errors is not None:
            errors.append({"stage": "assets", "error": f"assets.yaml not loaded: {exc}"})
        return {}


def move_labels(
    session: Session,
    stories: Sequence[Story],
    assets: dict[str, AssetConfig],
    settings: Settings,
    now: datetime,
) -> dict[int, str]:
    """ "already moved" / "moving against this call" per impact, from cached daily bars only
    (no network at digest time)."""
    impacts = [impact for story in stories for impact in story.impacts]
    labels: dict[int, str] = {}
    daily: dict[str, list] = {}
    for impact in impacts:
        if impact.symbol not in daily:
            daily[impact.symbol] = cached_bars(session, impact.symbol, DAILY, now - DAILY_LEAD)
        label = label_for(impact, assets.get(impact.symbol), daily[impact.symbol], settings)
        if label:
            labels[impact.id] = label
    return labels


def asset_warning(
    session_factory: sessionmaker[Session], now: datetime | None = None
) -> str | None:
    """A warning line if any asset in config/assets.yaml isn't currently validated (never
    checked, failed its last check, or last checked over 30 days ago), else None."""
    try:
        assets = load_assets()
    except Exception as exc:  # a broken assets.yaml must not stop a run
        return f"asset universe: config/assets.yaml can't be loaded ({exc})"
    with session_factory() as session:
        gaps = validation_gaps(session, assets, now or utcnow())
    return validation_warning(gaps, len(assets))


def run_once(settings: Settings, session_factory: sessionmaker[Session]) -> list[str]:
    """One pipeline pass with the configured LLM client; returns the summary lines to show."""
    llm, llm_unavailable = _llm_client(settings, session_factory)
    embedder: Embedder | None = None
    embedder_unavailable: str | None = None
    if settings.grouping.method == "embedding":
        embedder, embedder_unavailable = load_embedder(settings.grouping.embedding_model)
    feeds = load_feeds(include_disabled=True)
    report = run_pipeline(
        session_factory,
        settings,
        feeds,
        llm,
        llm_unavailable=llm_unavailable,
        embedder=embedder,
        embedder_unavailable=embedder_unavailable,
        prices=YahooPrices(settings.http.timeout_seconds),
    )

    pending = (
        f" + {report.pending_carried} pending from earlier runs" if report.pending_carried else ""
    )
    left = f", left for next run {report.skipped_quota}" if report.skipped_quota else ""
    lines = [
        f"run {report.run_id}: feeds {report.feeds_ok} ok / {report.feeds_failed} failed · "
        f"articles fetched {report.articles_fetched}, in last "
        f"{settings.pipeline.lookback_hours}h {report.articles_recent}, "
        f"new {report.articles_new} ({report.duplicates_dropped} duplicates dropped)",
        f"stories: {report.stories_created} new, {report.articles_attached} articles attached "
        f"to existing ({report.grouping_method} grouping; non-news: "
        f"{report.non_news_attached} attached, {report.non_news_ungrouped} left out) · "
        f"top {report.stories_ranked} ranked{pending} · summarized "
        f"{report.summarized}, unchanged {report.skipped_unchanged}, failed {report.failed}, "
        f"LLM errors {report.llm_call_errors}{left}",
        f"events: extracted {report.events_extracted}"
        + (
            f" ({report.events_pending_carried} carried over)"
            if report.events_pending_carried
            else ""
        )
        + f", failed {report.events_failed}, API errors {report.event_call_errors}"
        + (
            f", left for next run {report.events_skipped_quota}"
            if report.events_skipped_quota
            else ""
        ),
        f"prices: {report.impacts_priced} impacts got a reference price, "
        f"{report.impacts_refreshed} moves refreshed, {report.impacts_waiting} waiting for "
        f"their market to open, over {report.price_symbols} symbols "
        f"({report.price_bars_stored} bars stored)"
        + (
            "; unusable: " + ", ".join(f"{n} {why}" for why, n in report.price_unusable.items())
            if report.price_unusable
            else ""
        ),
        f"impacts: {report.impacts_created} from {report.stories_analyzed} analyzed "
        f"({report.llm_impact_calls} LLM calls, {report.llm_impact_declines} declined, "
        f"{report.llm_impacts_added} added by the model, "
        f"{report.llm_disagreements} rule disagreements) "
        + (
            "stories (" + ", ".join(f"{rule} {n}" for rule, n in report.impact_rules.items()) + ")"
            if report.impact_rules
            else "stories"
        ),
        f"LLM ({settings.llm.provider}, {settings.llm.summary_model}): {report.llm_calls} calls, "
        f"tokens {report.input_tokens} input, {report.output_tokens} output",
    ]
    if llm is not None and llm.limiter is not None:
        quota = llm.limiter.status(settings.llm.summary_model)
        if quota is not None:
            lines.append(
                f"quota: {quota.used}/{quota.limit} requests used on quota day {quota.day} "
                f"(budget {quota.budget}), resets {llm.limiter.format_reset()}"
            )
    if report.grouping_method == "embedding":
        low, high = settings.grouping.borderline_log_range
        lines.append(
            f"borderline matches ({low}-{high}) logged: {report.borderline_logged} "
            f"→ {settings.paths.log_dir}/grouping_borderline.jsonl"
        )
    if report.non_news:
        lines.append(f"non-news headlines this run ({len(report.non_news)}):")
        for entry in report.non_news:
            where = (
                f'attached to story {entry["story_id"]} "{entry["story_headline"]}"'
                if entry["story_id"]
                else "left out (no matching story)"
            )
            lines.append(f"  [{entry['reason']}] {entry['title']} ({entry['source']}) → {where}")
    if report.llm_notes:
        lines.append(f"impact-layer notes ({len(report.llm_notes)}):")
        lines += [f"  {note}" for note in report.llm_notes]
    if report.event_notes:
        lines.append(f"event notes ({len(report.event_notes)}):")
        lines += [f"  {note}" for note in report.event_notes]
    warning = asset_warning(session_factory)
    if warning:
        lines.append(f"warning: {warning}")
    lines += [f"  error: {error}" for error in report.errors]
    return lines


def _lock_dir(settings: Settings) -> Path:
    return settings.resolve_path(settings.paths.lock_dir)


@app.command()
def run() -> None:
    """One full pipeline pass: fetch, dedupe, group, rank, summarize."""
    settings, session_factory = _bootstrap()
    with job_lock(_lock_dir(settings), "pipeline") as acquired:
        if not acquired:
            typer.echo("another pipeline run is still going; skipped this one")
            return
        for line in run_once(settings, session_factory):
            typer.echo(line)


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
    # Only a send is locked: a dry run writes nothing and can't double-deliver.
    with job_lock(_lock_dir(settings), "digest") as acquired:
        if send and not acquired:
            typer.echo("another digest is being sent; skipped this one")
            return
        _digest(settings, session_factory, send)


def _digest(settings: Settings, session_factory: sessionmaker[Session], send: bool) -> None:
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


# ---------------------------------------------------------------- scoring


@dataclass
class ScoreRunReport:
    run_id: int
    prices: PriceReport
    scores: ScoreReport
    unreferenced: int = 0


def run_score(
    session_factory: sessionmaker[Session],
    settings: Settings,
    prices: PriceProvider,
    now: datetime | None = None,
    rescore: bool = False,
) -> ScoreRunReport:
    """Fill in any reference prices still missing, then judge every call whose horizon is
    complete (SPEC 7.9). Safe to run repeatedly: scores are written once."""
    now = now or utcnow()
    assets = _assets()
    with session_factory() as session:
        run = Run(kind="score", started_at=utcnow(), errors=[])
        session.add(run)
        session.commit()
        # 1. Impacts whose market had not opened when they were created.
        waiting = unpriced_impacts(session)
        priced = price_impacts(session, waiting, assets, prices, settings, now)
        # 2. Anything still without a reference after the grace period is unscorable.
        unreferenced = mark_unreferenced(session, settings, now)
        # 3. Judge the calls whose horizons are complete.
        scores = score_impacts(session, assets, prices, settings, now, rescore=rescore)

        errors = [
            {"stage": "prices", "symbol": symbol, "error": reason}
            for symbol, reason in priced.unusable
        ] + [
            {"stage": "score", "symbol": symbol, "error": reason}
            for symbol, reason in scores.problems
        ]
        run.finished_at = utcnow()
        run.stories_processed = scores.total_scored
        run.errors = errors
        session.commit()
        return ScoreRunReport(run.id, priced, scores, unreferenced)


def _varies(session: Session, group: str) -> bool:
    """Whether judged calls exist under more than one value of `group`."""
    return len({row.key for row in track_record(session, group)}) > 1


def track_record_lines(session: Session, settings: Settings) -> list[str]:
    """The track-record tables. Counts are always shown; rates only once there are enough
    judged calls to mean anything."""
    minimum = settings.scoring.min_samples_to_show_rate
    lines = []
    groups = ["rule_id", "event_type", "origin", "confidence", "horizon_days"]
    # Sampling settings are fixed, so a table per value is noise until one of them changes.
    groups += [group for group in ("temperature", "seed") if _varies(session, group)]
    for group in groups:
        rows = track_record(session, group)
        if not rows:
            continue
        lines.append(f"\nby {group}:")
        lines.append(
            f"  {'key':<28} {'horizon':>7} {'hit':>4} {'miss':>5} {'no move':>8} "
            f"{'unscorable':>11} {'stories':>8}  rate"
        )
        for row in rows:
            rate = (
                f"{row.rate:.0%}"
                if row.rate is not None and row.shows_rate(minimum)
                else f"n={row.judged} too small"
            )
            lines.append(
                f"  {row.key[:28]:<28} {row.horizon_days:>6}d {row.hits:>4} {row.misses:>5} "
                f"{row.no_move:>8} {row.unscorable:>11} {len(row.stories):>8}  {rate}"
            )
    return lines


@app.command()
def score(
    rescore: Annotated[
        bool, typer.Option("--rescore", help="Recompute scores that already exist.")
    ] = False,
) -> None:
    """Score every call whose horizon is complete, and print the track record."""
    settings, session_factory = _bootstrap()
    with job_lock(_lock_dir(settings), "score") as acquired:
        if not acquired:
            typer.echo("another scoring run is still going; skipped this one")
            return
        _score(settings, session_factory, rescore)


def _score(settings: Settings, session_factory: sessionmaker[Session], rescore: bool) -> None:
    report = run_score(
        session_factory, settings, YahooPrices(settings.http.timeout_seconds), rescore=rescore
    )
    typer.echo(
        f"score run {report.run_id}: {report.prices.priced} reference prices filled in, "
        f"{report.prices.waiting} still waiting for their market, "
        f"{report.unreferenced} gave up (no reference in time)"
    )
    outcomes = ", ".join(f"{count} {name}" for name, count in sorted(report.scores.scored.items()))
    typer.echo(
        f"scored {report.scores.total_scored} calls ({outcomes or 'none'}); "
        f"{report.scores.not_due} not due yet, over {report.scores.symbols} symbols"
    )
    for symbol, reason in report.prices.unusable + report.scores.problems:
        typer.echo(f"  {symbol}: {reason}")
    with session_factory() as session:
        for line in track_record_lines(session, settings):
            typer.echo(line)


@app.command()
def schedule_times() -> None:
    """Print the times the scheduler uses, as JSON.

    `scripts/install_tasks.ps1` reads this, so the Windows tasks are always the times in
    settings.yaml rather than a second copy of them that can drift.
    """
    load_env()
    settings = load_settings()
    typer.echo(
        json.dumps(
            {
                "timezone": settings.timezone,
                "pipeline_hours": pipeline_hours(
                    settings.schedule.pipeline_every_hours, settings.delivery.digest_times
                ),
                "digest_times": list(settings.delivery.digest_times),
                "score_time": settings.schedule.score_time,
            },
            indent=2,
        )
    )


@app.command()
def health(
    days: Annotated[
        int, typer.Option("--days", min=1, help="How many days back to report on.")
    ] = HEALTH_DAYS,
) -> None:
    """Whether the scheduler is keeping up, what the LLM layers cost, and which rules have
    enough judged calls to mean anything. Reads the database only."""
    settings, session_factory = _bootstrap()
    with session_factory() as session:
        for line in health_lines(session, settings, utcnow(), days):
            typer.echo(line)


# ---------------------------------------------------------------- tickers


@dataclass
class TickerValidation:
    results: list[TickerResult]
    checked_at: datetime
    previous_check_at: datetime | None
    report_path: str

    @property
    def failed(self) -> list[TickerResult]:
        return [result for result in self.results if not result.ok]


def run_ticker_validation(
    settings: Settings,
    session_factory: sessionmaker[Session],
    fetch: Fetch | None = None,
    now: datetime | None = None,
    report_path: Path | None = None,
) -> TickerValidation:
    """Check every symbol in config/assets.yaml against Yahoo, store one ticker_checks row per
    symbol, and write the report (default data/ticker_report.md)."""
    assets = load_assets()
    now = now or utcnow()
    with session_factory() as session:
        previous = max(
            (check.checked_at for check in latest_checks(session).values()), default=None
        )
    results = validate_assets(assets, fetch or yahoo_fetch(settings.http.timeout_seconds), now)
    with session_factory() as session:
        record_checks(session, results, now)
        session.commit()
    path = report_path or settings.resolve_path("data/ticker_report.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown_report(results, now), encoding="utf-8")
    return TickerValidation(results, now, previous, str(path))


def ticker_report_lines(validation: TickerValidation, settings: Settings) -> list[str]:
    lines = []
    if validation.previous_check_at is None:
        lines.append("previous check: none")
    else:
        age = (validation.checked_at - validation.previous_check_at).days
        when = validation.previous_check_at.astimezone(settings.tz).strftime("%d %b %Y %H:%M %Z")
        stale = " (older than 30 days)" if age > 30 else ""
        lines.append(f"previous check: {when}, {age} days ago{stale}")
    widths = [14, 6, 10, 12, 34, 4, 8, 18, 8]
    lines.append("  ".join(h.ljust(w) for h, w in zip(REPORT_HEADER, widths, strict=False)))
    for row in report_rows(validation.results):
        cells = [cell[:w].ljust(w) for cell, w in zip(row, widths, strict=False)]
        lines.append("  ".join([*cells, row[-1]]).rstrip())
    failed = validation.failed
    flagged = [r for r in validation.results if r.ok and r.flags]
    lines.append(
        f"{len(validation.results)} symbols: {len(validation.results) - len(failed)} ok, "
        f"{len(failed)} failed, {len(flagged)} ok but flagged for review · report: "
        f"{validation.report_path}"
    )
    if failed:
        lines.append(
            "Failed symbols are not replaced automatically. Fix or remove them in "
            "config/assets.yaml only with a verified symbol."
        )
    return lines


@app.command("validate-tickers")
def validate_tickers() -> None:
    """Check every symbol in config/assets.yaml has recent daily prices on Yahoo Finance."""
    settings, session_factory = _bootstrap()
    validation = run_ticker_validation(settings, session_factory)
    for line in ticker_report_lines(validation, settings):
        typer.echo(line)
    if validation.failed:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------- scheduler


def build_scheduler(
    settings: Settings,
    pipeline_job: Callable[[], None],
    digest_job: Callable[[], None],
    score_job: Callable[[], None] | None = None,
) -> BlockingScheduler:
    tz = settings.tz
    scheduler = BlockingScheduler(timezone=tz)
    hours = pipeline_hours(settings.schedule.pipeline_every_hours, settings.delivery.digest_times)
    scheduler.add_job(
        pipeline_job,
        CronTrigger(hour=",".join(str(hour) for hour in hours), minute=0, timezone=tz),
        id="pipeline",
        name="pipeline run",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=15 * 60,
    )
    for time_text in settings.delivery.digest_times:
        hour, minute = (int(part) for part in time_text.split(":"))
        scheduler.add_job(
            digest_job,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            id=f"digest-{time_text}",
            name=f"digest {time_text}",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30 * 60,
        )
    if score_job is not None:
        hour, minute = (int(part) for part in settings.schedule.score_time.split(":"))
        scheduler.add_job(
            score_job,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            id="score",
            name=f"score {settings.schedule.score_time}",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60 * 60,
        )
    return scheduler


@app.command()
def scheduler() -> None:
    """Run the pipeline every schedule.pipeline_every_hours and send digests at
    delivery.digest_times (both in settings.timezone). Stop with Ctrl+C."""
    settings, session_factory = _bootstrap()

    lock_dir = _lock_dir(settings)

    def pipeline_job() -> None:
        try:
            with job_lock(lock_dir, "pipeline") as acquired:
                if not acquired:
                    log.warning("skipped this pipeline run: the last one is still going")
                    return
                for line in run_once(settings, session_factory):
                    log.info(line)
        except Exception:
            log.exception("scheduled pipeline run failed")

    def digest_job() -> None:
        try:
            with job_lock(lock_dir, "digest") as acquired:
                if not acquired:
                    log.warning("skipped this digest: another one is being sent")
                    return
                _send_digest()
        except Exception:
            log.exception("scheduled digest failed")

    def _send_digest() -> None:
        report = run_digest(
            session_factory,
            settings,
            send=True,
            token=get_secret("TELEGRAM_BOT_TOKEN"),
            chat_id=get_secret("TELEGRAM_CHAT_ID"),
        )
        log.info("scheduled digest: %d stories, sent=%s", report.stories, report.sent)

    def score_job() -> None:
        try:
            with job_lock(lock_dir, "score") as acquired:
                if not acquired:
                    log.warning("skipped this scoring run: the last one is still going")
                    return
                report = run_score(
                    session_factory, settings, YahooPrices(settings.http.timeout_seconds)
                )
                log.info(
                    "scheduled score: %d calls judged, %d references filled",
                    report.scores.total_scored,
                    report.prices.priced,
                )
        except Exception:
            log.exception("scheduled scoring failed")

    jobs = build_scheduler(settings, pipeline_job, digest_job, score_job)
    warning = asset_warning(session_factory)
    if warning:
        log.warning(warning)
        typer.echo(f"warning: {warning}")
    now = datetime.now(settings.tz)
    for job in jobs.get_jobs():
        next_fire = job.trigger.get_next_fire_time(None, now)
        typer.echo(f"{job.name}: {job.trigger} · next {next_fire:%d %b %H:%M %Z}")
    try:
        jobs.start()
    except (KeyboardInterrupt, SystemExit):
        typer.echo("scheduler stopped")
