"""Format stories into Telegram HTML messages (SPEC 10)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from app.models import Article, Story
from app.pipeline.dedupe import normalize_source

TELEGRAM_LIMIT = 4096
MAX_SOURCE_LINKS = 3
FOOTER = "<i>Research notes, not financial advice.</i>"
STORY_SEPARATOR = "\n\n"


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


def digest_item(story: Story) -> DigestItem:
    return DigestItem(
        headline=story.headline,
        summary=story.summary or "",
        category=story.category,
        regions=list(story.regions or []),
        disagreement_note=story.disagreement_note if story.sources_disagree else None,
        sources=pick_sources(story.articles),
    )


def telegram_length(text: str) -> int:
    """Length in UTF-16 code units of the raw HTML. Tags and entities only shrink when Telegram
    parses them, so this never undercounts its 4096-character limit."""
    return len(text.encode("utf-16-le")) // 2


def _text(value: str) -> str:
    return escape(value, quote=False)


def format_story(item: DigestItem) -> str:
    lines = [f"<b>{_text(item.headline)}</b>"]
    meta = " · ".join(part for part in (item.category, ", ".join(item.regions)) if part)
    if meta:
        lines.append(f"<i>{_text(meta)}</i>")
    lines.append(_text(item.summary))
    if item.disagreement_note:
        lines.append(f"<i>Sources disagree:</i> {_text(item.disagreement_note)}")
    if item.sources:
        links = " · ".join(
            f'<a href="{escape(source.url, quote=True)}">{_text(source.name)}</a>'
            for source in item.sources
        )
        lines.append(f"Sources: {links}")
    return "\n".join(lines)


def _fit_story(item: DigestItem, limit: int) -> str:
    """Format a story, shortening its summary if the story alone exceeds the limit."""
    text = format_story(item)
    summary = item.summary
    while telegram_length(text) > limit and summary:
        overflow = telegram_length(text) - limit
        summary = summary[: max(len(summary) - overflow - 20, 0)].rstrip() + "…"
        if len(summary) <= 1:
            summary = ""
        text = format_story(
            DigestItem(
                item.headline,
                summary,
                item.category,
                item.regions,
                item.disagreement_note,
                item.sources,
            )
        )
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
    header = f"<b>Newsdesk digest</b> · {local:%a %d %b %Y, %H:%M} {local.tzname()} · {count}"

    messages: list[str] = []
    current = header
    # Leave room for the header so the first message never ends up header-only.
    story_limit = limit - telegram_length(header) - len(STORY_SEPARATOR)
    blocks = [_fit_story(item, story_limit) for item in items] or [
        "No new stories since the last digest."
    ]
    for block in [*blocks, FOOTER]:
        candidate = f"{current}{STORY_SEPARATOR}{block}"
        if telegram_length(candidate) <= limit:
            current = candidate
        else:
            messages.append(current)
            current = block
    messages.append(current)
    return messages
