"""The watchlist call (step 3): one Flash-Lite call per story, assessing every watchlist
company the story is a keep for - summary, then per company relevance, sentiment,
materiality, event type and a one-line reason (user, 2026-10-07).

One call covers all of a story's companies, so "Russia partners Adani Defence, HAL, BDL..."
is one request with an answer for each, which is also what lets it become one alert.

A story is called when it first has a keep (or a filing), and again - new rows, the old ones
kept - when a filing attaches, when the company's reply to NSE has been read, or when it has
gained two more kept articles. Calls spend the "watch" lane of the quota, never the main one.

The company's reply is a PDF on NSE's archive; its text is read with pypdf (approved and
pinned, 2026-10-07). A scanned reply has no text layer, and is noted rather than guessed at.
"""

import io
import logging
import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.config import Settings, WatchItem
from app.llm.client import (
    LLMCallError,
    LLMClient,
    LLMConfigError,
    LLMOutputError,
    LLMQuotaError,
    StructuredResult,
)
from app.llm.prompts import WATCH_PROMPT_VERSION, WATCH_SYSTEM, watch_user_prompt
from app.llm.schemas import StockAssessment, WatchAnalysis
from app.models import WatchArticle, WatchCall, WatchFiling, WatchStory, utcnow

log = logging.getLogger(__name__)

WATCH_MAX_TOKENS = 2048  # thinking tokens count against it, as for summaries
MAX_ARTICLES = 6
MAX_REPLY_CHARS = 6000
REPLY_PAGES = 4
CALLS_PER_PASS = 12  # one scanner pass never spends more than this
REPLY_TIMEOUT = 30.0


@dataclass(frozen=True)
class ArticleView:
    source_name: str
    title: str
    snippet: str
    published_at: datetime


@dataclass(frozen=True)
class FilingView:
    exchange: str
    subject: str
    description: str
    filed_at: datetime


@dataclass
class StoryInput:
    """Everything one call sees, detached from the database session."""

    story_id: int
    companies: list[tuple[str, str]]  # (symbol, how to name it to the model)
    articles: list[ArticleView]
    filings: list[FilingView]
    reply: str | None
    article_count: int
    filing_count: int
    trigger: str

    def prompt(self) -> str:
        return watch_user_prompt(self.companies, self.articles, self.filings, self.reply)


@dataclass
class AnalyseResult:
    called: list[int] = field(default_factory=list)  # story ids
    calls_written: int = 0
    failed: list[tuple[int, str]] = field(default_factory=list)
    stopped: str | None = None  # the watch lane's budget, or no LLM at all
    quota_spent: bool = False  # stopped because the watch lane's budget is used up
    notes: list[str] = field(default_factory=list)


def company_label(item: WatchItem) -> str:
    """How the model is told who the company is: its exchange name and symbol, which is
    what separates Hindustan Aeronautics (NSE: HAL) from Halliburton (NYSE: HAL), and the
    other names the news uses for it. Without them a "Brahma AI raises $150M" story read as
    not about Prime Focus at all (the gate, 2026-10-08)."""
    name = item.nse_name or item.name or item.symbol
    if item.nse_symbol:
        label = f"{name} (NSE: {item.nse_symbol})"
    elif item.bse_code:  # listed only on BSE (user, 2026-10-17)
        label = f"{name} (BSE: {item.bse_code})"
    else:
        label = name
    others = [
        alias
        for alias in (*item.aliases.strong, *item.aliases.weak)
        if alias.casefold() not in name.casefold() and alias != item.nse_symbol
    ]
    return f"{label}; the news also names it as: {', '.join(others)}" if others else label


def kept_symbols(story: WatchStory) -> set[str]:
    """The watchlist companies a story is about: a keep in any article, or a filing."""
    found = {
        match.symbol
        for article in story.articles
        for match in article.matches
        if match.verdict == "keep"
    }
    return found | {filing.symbol for filing in story.filings}


def kept_articles(story: WatchStory) -> list[WatchArticle]:
    return [a for a in story.articles if any(m.verdict == "keep" for m in a.matches)]


# ---------------------------------------------------------------- when a story happened
# When a story happened is when it was first reported, never when BASIS first saw it (user,
# 2026-10-19). A Google News search for a stock returns old pages too - a 2020 price page, a
# 2024 results story - whatever window it is asked for, and those were stored with
# first_seen_at = the day they turned up. The scanner no longer stores an article published
# more than `watch.catch_up_max_days` before it first sees it; among those stored before that,
# an old page grouped into a current story doesn't date the story.


def stale_after(settings: Settings) -> timedelta:
    """How old an article can be when BASIS first sees it and still be news."""
    return timedelta(days=settings.watch.catch_up_max_days)


def stale_article(article: WatchArticle, max_age: timedelta) -> bool:
    """Published more than `max_age` before BASIS first saw it: old news a search turned up."""
    return article.published_at < article.first_seen_at - max_age


def reporting_articles(story: WatchStory, max_age: timedelta) -> list[WatchArticle]:
    """The kept articles that report the story: those that were news when BASIS saw them.
    An old page grouped in counts only when the story has nothing else - no current article
    and no filing - and the story is then old news itself."""
    kept = kept_articles(story)
    current = [a for a in kept if not stale_article(a, max_age)]
    if current or story.filings:
        return current
    return kept


def first_reported(story: WatchStory, max_age: timedelta) -> datetime:
    """When the news first came out: the earliest reporting article's publication or filing
    time, and never later than when BASIS saw it."""
    times = [a.published_at for a in reporting_articles(story, max_age)]
    times += [f.filed_at for f in story.filings]
    return min([*times, story.first_seen_at])


def stale_story(story: WatchStory, max_age: timedelta) -> bool:
    """First reported more than `max_age` before BASIS first saw it: never called, never
    scored, never shown as this week's news."""
    return first_reported(story, max_age) < story.first_seen_at - max_age


def trigger_for(story: WatchStory, calls: Sequence[WatchCall]) -> str | None:
    """Why the story needs a call now, or None if its current call still stands."""
    if not kept_symbols(story):
        return None
    if not calls:
        return "new"
    last = max(calls, key=lambda call: (call.created_at, call.id))
    if not last.read_reply and any(f.reply_text for f in story.filings):
        return "reply"
    if len(story.filings) > last.filing_count:
        return "filing"
    if len(kept_articles(story)) >= last.article_count + 2:
        return "articles"
    return None


def choose_articles(story: WatchStory, max_age: timedelta) -> list[WatchArticle]:
    """Up to MAX_ARTICLES of the articles reporting the story (never an old page grouped
    in), one per outlet first, earliest first: the first reports carry the news, later ones
    mostly repeat it."""
    ordered = sorted(reporting_articles(story, max_age), key=lambda a: (a.first_seen_at, a.id))
    chosen: list[WatchArticle] = []
    outlets: set[str] = set()
    for article in ordered:
        if article.source_name not in outlets:
            chosen.append(article)
            outlets.add(article.source_name)
    for article in ordered:
        if article not in chosen:
            chosen.append(article)
    return chosen[:MAX_ARTICLES]


def story_input(
    story: WatchStory, stocks: dict[str, WatchItem], trigger: str, max_age: timedelta
) -> StoryInput | None:
    symbols = [symbol for symbol in stocks if symbol in kept_symbols(story)]
    if not symbols:
        return None
    replies = [f.reply_text for f in story.filings if f.reply_text]
    return StoryInput(
        story_id=story.id,
        companies=[(symbol, company_label(stocks[symbol])) for symbol in symbols],
        articles=[
            ArticleView(a.source_name, a.title, a.snippet, a.published_at)
            for a in choose_articles(story, max_age)
        ],
        filings=[
            FilingView(f.exchange, f.subject, f.description, f.filed_at)
            for f in sorted(story.filings, key=lambda f: f.filed_at)
        ],
        reply=replies[-1] if replies else None,
        article_count=len(kept_articles(story)),
        filing_count=len(story.filings),
        trigger=trigger,
    )


def request_watch_call(
    llm: LLMClient, given: StoryInput, settings: Settings
) -> StructuredResult[WatchAnalysis]:
    return llm.structured(
        model=settings.llm.summary_model,
        system=WATCH_SYSTEM,
        user=given.prompt(),
        schema=WatchAnalysis,
        max_tokens=WATCH_MAX_TOKENS,
        purpose=f"watch story {given.story_id}",
        lane="watch",
    )


def matched_assessments(
    analysis: WatchAnalysis, requested: Sequence[str]
) -> tuple[list[StockAssessment], list[str]]:
    """The assessments for the companies asked about. One for a company not asked about is
    dropped; a company left out gets no call this time. Both are noted."""
    wanted = set(requested)
    kept: list[StockAssessment] = []
    notes: list[str] = []
    for item in analysis.stocks:
        if item.symbol in wanted and item.symbol not in {k.symbol for k in kept}:
            kept.append(item)
        else:
            notes.append(f"dropped an assessment for {item.symbol}: not asked about")
    missing = wanted - {k.symbol for k in kept}
    if missing:
        notes.append(f"no assessment returned for {', '.join(sorted(missing))}")
    return kept, notes


# ---------------------------------------------------------------- the company's reply


def pdf_text(content: bytes, pages: int = REPLY_PAGES, limit: int = MAX_REPLY_CHARS) -> str:
    """The first pages' text, whitespace collapsed. Empty for a scan with no text layer."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    text = " ".join((page.extract_text() or "") for page in reader.pages[:pages])
    return re.sub(r"\s+", " ", text).strip()[:limit]


def fetch_reply(url: str, user_agent: str) -> bytes:
    response = httpx.get(
        url, headers={"User-Agent": user_agent}, timeout=REPLY_TIMEOUT, follow_redirects=True
    )
    response.raise_for_status()
    return response.content


def read_replies(
    session_factory: sessionmaker[Session],
    settings: Settings,
    fetch: Callable[[str, str], bytes] = fetch_reply,
) -> list[str]:
    """Read every company reply that has a PDF and hasn't been read. Returns notes."""
    notes: list[str] = []
    with session_factory() as session:
        waiting = session.scalars(
            select(WatchFiling).where(
                WatchFiling.kind == "company_reply",
                WatchFiling.reply_text.is_(None),
                WatchFiling.link != "",
            )
        ).all()
        todo = [(filing.id, filing.link, filing.symbol) for filing in waiting]
    for filing_id, link, symbol in todo:
        try:
            text = pdf_text(fetch(link, settings.http.user_agent))
        except Exception as exc:  # unreadable today; tried again next pass
            notes.append(f"{symbol} reply {link}: {type(exc).__name__}: {exc}")
            continue
        if not text:
            notes.append(f"{symbol} reply {link}: a scan with no text layer, not read")
        with session_factory() as session:
            filing = session.get(WatchFiling, filing_id)
            if filing is not None:
                filing.reply_text = text
                session.commit()
    return notes


# ---------------------------------------------------------------- the pass


def open_since(since: datetime) -> ColumnElement[bool]:
    """Stories still taking news: begun since `since`, or with an article or a filing first
    seen since then - the stories the grouper still adds to."""
    return or_(
        WatchStory.first_seen_at >= since,
        WatchStory.id.in_(
            select(WatchArticle.story_id).where(
                WatchArticle.first_seen_at >= since, WatchArticle.story_id.is_not(None)
            )
        ),
        WatchStory.id.in_(
            select(WatchFiling.story_id).where(
                WatchFiling.first_seen_at >= since, WatchFiling.story_id.is_not(None)
            )
        ),
    )


def stories_to_call(
    session: Session,
    stocks: dict[str, WatchItem],
    settings: Settings,
    now: datetime,
    only: Collection[str] | None = None,
) -> list[StoryInput]:
    """Stories in the window that need a call, most urgent first: filings, then stories
    named outright in a headline, then the newest. `only`: just the stories about these
    companies (a scan of one stock, app/watch/ondemand.py). A story that was old news when
    BASIS first saw it is never called.

    "In the window" is the grouper's own test (scan._window_members): a story is open while
    any of its articles or filings was first seen within `story_window_hours`, however long ago
    it began. Judged by the story's own first_seen_at instead, a story that kept taking news
    dropped out of the calls 72 hours after it began, and nothing that joined it later was
    called: Zen's Cogknit acquisition - a filing, a press release and 12 reports from 17 Oct -
    joined a 13 Oct share-price story and kept that story's 15 Oct call, neutral and low."""
    max_age = stale_after(settings)
    since = now - timedelta(hours=settings.watch.story_window_hours)
    stories = session.scalars(
        select(WatchStory)
        .where(open_since(since))
        .options(
            selectinload(WatchStory.articles).selectinload(WatchArticle.matches),
            selectinload(WatchStory.filings),
        )
    ).all()
    calls: dict[int, list[WatchCall]] = {}
    for call in session.scalars(
        select(WatchCall).where(WatchCall.story_id.in_([s.id for s in stories]))
    ):
        calls.setdefault(call.story_id, []).append(call)
    pending: list[tuple[tuple[int, int, float], StoryInput]] = []
    for story in stories:
        if stale_story(story, max_age):
            continue
        trigger = trigger_for(story, calls.get(story.id, []))
        if trigger is None:
            continue
        given = story_input(story, stocks, trigger, max_age)
        if given is None:
            continue
        if only is not None and not {symbol for symbol, _ in given.companies} & set(only):
            continue
        named = any(
            m.verdict == "keep" and m.reason == "named in the headline"
            for a in story.articles
            for m in a.matches
        )
        pending.append(
            (
                (0 if story.filings else 1, 0 if named else 1, -story.first_seen_at.timestamp()),
                given,
            )
        )
    return [given for _, given in sorted(pending, key=lambda pair: pair[0])]


def analyse(
    session_factory: sessionmaker[Session],
    llm: LLMClient | None,
    settings: Settings,
    stocks: dict[str, WatchItem],
    now: datetime | None = None,
    skip: set[tuple[int, int, int]] | None = None,
    limit: int = CALLS_PER_PASS,
    only: Collection[str] | None = None,
) -> AnalyseResult:
    """Call every story that needs it, up to `limit`. Never holds a write across a call:
    the stories are read, the session closed, and each call's rows written on their own.
    `skip` holds (story, articles, filings) whose output was invalid, so a pass doesn't
    spend a request on the same input again; the caller keeps it between passes."""
    result = AnalyseResult()
    if llm is None:
        result.stopped = "no LLM client"
        return result
    now = now or utcnow()
    with session_factory() as session:
        pending = stories_to_call(session, stocks, settings, now, only)
    skip = skip if skip is not None else set()
    provenance = settings.llm.provenance(settings.llm.summary_model, WATCH_PROMPT_VERSION)
    for given in pending:
        key = (given.story_id, given.article_count, given.filing_count)
        if key in skip:
            continue
        if len(result.called) >= limit:
            break
        try:
            output = request_watch_call(llm, given, settings)
        except (LLMQuotaError, LLMConfigError) as exc:
            result.stopped = str(exc)
            result.quota_spent = isinstance(exc, LLMQuotaError)
            log.warning("watchlist calls stopped for now: %s", exc)
            break
        except LLMCallError as exc:  # transient: next pass tries again
            result.failed.append((given.story_id, str(exc)))
            continue
        except LLMOutputError as exc:
            skip.add(key)
            result.failed.append((given.story_id, str(exc)))
            continue
        assessed, notes = matched_assessments(output.value, [s for s, _ in given.companies])
        result.notes += [f"story {given.story_id}: {note}" for note in notes]
        with session_factory() as session:
            for item in assessed:
                session.add(
                    WatchCall(
                        story_id=given.story_id,
                        symbol=item.symbol,
                        relevance=item.relevance,
                        sentiment=item.sentiment,
                        materiality=item.materiality,
                        event_type=item.event_type,
                        reason=item.reason,
                        summary=output.value.summary,
                        article_count=given.article_count,
                        filing_count=given.filing_count,
                        read_reply=given.reply is not None,
                        trigger=given.trigger,
                        model=provenance.model,
                        prompt_version=provenance.prompt_version,
                        temperature=provenance.temperature,
                        seed=provenance.seed,
                        created_at=now,
                    )
                )
            session.commit()
        result.called.append(given.story_id)
        result.calls_written += len(assessed)
    return result
