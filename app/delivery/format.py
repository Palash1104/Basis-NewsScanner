"""Format stories into Telegram HTML messages (SPEC 10)."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from app.config import AssetConfig
from app.models import Article, Story
from app.pipeline.dedupe import normalize_source
from app.presentation import SignalBlock, signal_block, story_age, story_calls

TELEGRAM_LIMIT = 4096
MAX_SOURCE_LINKS = 3
FOOTER = "<i>Research notes, not financial advice.</i>"
STORY_SEPARATOR = "\n\n"
# Telegram HTML has bold, italic, links, code and blockquotes, and nothing else
# (core.telegram.org/bots/api#html-style, checked 2026-09-22). `expandable` hides all but the
# first few lines behind a "show more" tap, which is why the market block is one: someone
# scrolling the digest sees the headline and the summary, and opens the numbers if they care.
SIGNALS_HEADER = "MARKET SIGNALS"


@dataclass(frozen=True)
class SourceLink:
    name: str
    url: str


@dataclass(frozen=True)
class DigestItem:
    headline: str
    summary: str
    category: str | None
    regions: list[str]
    disagreement_note: str | None
    sources: list[SourceLink]
    signals: SignalBlock | None = None  # None when the story calls nothing
    age: str | None = None  # "first reported 3d ago", only when the story broke well earlier


def pick_sources(articles: Sequence[Article], limit: int = MAX_SOURCE_LINKS) -> list[SourceLink]:
    """One link per outlet, most prominent outlets first, earliest article per outlet.
    Explainers and roundups aren't listed as sources."""
    news = [article for article in articles if not article.non_news] or list(articles)
    ordered = sorted(news, key=lambda a: (-a.source_weight, a.published_at))
    links: list[SourceLink] = []
    seen: set[str] = set()
    for article in ordered:
        key = normalize_source(article.source_name)
        if key in seen:
            continue
        seen.add(key)
        links.append(SourceLink(article.source_name, article.url))
        if len(links) == limit:
            break
    return links


def _signals(block: SignalBlock) -> str:
    """The market block, as one expandable quote.

    Order matters: only the first lines show before "show more", so the assets come first,
    then the window their moves cover, then why, then the record.
    """
    shared = "".join(f" · {_text(part)}" for part in block.shared)
    lines = [f"<b>{SIGNALS_HEADER}</b>{shared}"]
    lines += [_text(str(line)) for line in block.lines]
    if block.extra:
        lines.append(f"+{len(block.extra)} more: {_text(', '.join(block.extra))}")
    if block.shows_moves:
        lines.append("<i>Moves since news</i>")
    for rule, mechanism in block.reasons:
        why = f"{rule} — {mechanism}" if rule else mechanism
        lines.append(f"<i>Why:</i> {_text(why)}")
    if block.track_record:
        lines.append(f"<i>{_text(block.track_record)}</i>")
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def digest_item(
    story: Story,
    assets: dict[str, AssetConfig] | None = None,
    max_impacts: int = 6,
    labels: dict[int, str] | None = None,
    track_record: str | None = None,
    now: datetime | None = None,
) -> DigestItem:
    age = story_age(story, now) if now else None
    return DigestItem(
        headline=story.headline,
        summary=story.summary or "",
        category=story.category,
        regions=list(story.regions or []),
        disagreement_note=story.disagreement_note if story.sources_disagree else None,
        sources=pick_sources(story.articles),
        signals=signal_block(story_calls(story.impacts, assets, max_impacts, labels), track_record)
        if assets
        else None,
        age=f"first reported {age}" if age else None,
    )


def telegram_length(text: str) -> int:
    """Length in UTF-16 code units of the raw HTML. Tags and entities only shrink when Telegram
    parses them, so this never undercounts its 4096-character limit."""
    return len(text.encode("utf-16-le")) // 2


def _text(value: str) -> str:
    return escape(value, quote=False)


def format_story(item: DigestItem, number: int | None = None) -> str:
    """One story: a numbered headline, its meta line, the summary, then the market block."""
    title = f"{number:02d} · {_text(item.headline)}" if number else _text(item.headline)
    lines = [f"<b>{title}</b>"]
    meta = " · ".join(part for part in (item.category, ", ".join(item.regions), item.age) if part)
    if meta:
        lines.append(f"<i>{_text(meta)}</i>")
    lines.append("")  # the summary reads as its own paragraph
    lines.append(_text(item.summary))
    if item.disagreement_note:
        lines.append(f"<i>Sources disagree:</i> {_text(item.disagreement_note)}")
    if item.signals:
        lines.append(_signals(item.signals))
    if item.sources:
        links = " · ".join(
            f'<a href="{escape(source.url, quote=True)}">{_text(source.name)}</a>'
            for source in item.sources
        )
        lines.append(f"Sources: {links}")
    return "\n".join(lines)


def _fit_story(item: DigestItem, limit: int, number: int) -> str:
    """Format a story, shortening its summary if the story alone exceeds the limit."""
    text = format_story(item, number)
    summary = item.summary
    while telegram_length(text) > limit and summary:
        overflow = telegram_length(text) - limit
        summary = summary[: max(len(summary) - overflow - 20, 0)].rstrip() + "…"
        if len(summary) <= 1:
            summary = ""
        text = format_story(replace(item, summary=summary), number)
    return text


def format_digest(
    items: Sequence[DigestItem],
    generated_at: datetime,
    tz: ZoneInfo,
    limit: int = TELEGRAM_LIMIT,
) -> list[str]:
    """Build the digest as one or more messages under `limit`, never splitting a story.
    The last message ends with the not-financial-advice footer."""
    local = generated_at.astimezone(tz)
    count = f"{len(items)} {'story' if len(items) == 1 else 'stories'}"
    header = f"<b>BASIS</b> · {local:%a %d %b %Y, %H:%M} {local.tzname()} · {count}"

    messages: list[str] = []
    current = header
    # Leave room for the header so the first message never ends up header-only.
    story_limit = limit - telegram_length(header) - len(STORY_SEPARATOR)
    blocks = [
        _fit_story(item, story_limit, number) for number, item in enumerate(items, start=1)
    ] or ["No new stories since the last digest."]
    for block in [*blocks, FOOTER]:
        candidate = f"{current}{STORY_SEPARATOR}{block}"
        if telegram_length(candidate) <= limit:
            current = candidate
        else:
            messages.append(current)
            current = block
    messages.append(current)
    return messages
