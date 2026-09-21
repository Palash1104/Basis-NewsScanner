"""Format stories into Telegram HTML messages (SPEC 10)."""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from app.config import AssetConfig
from app.models import Article, Impact, Story
from app.pipeline.dedupe import normalize_source
from app.presentation import AssetCall, story_calls

TELEGRAM_LIMIT = 4096
MAX_SOURCE_LINKS = 3
UP, DOWN, MIXED = "\u25b2", "\u25bc", "\u2195"
_ORDER_LABEL = {"first": "1st", "second": "2nd"}
# Where the call came from: the rules, the model, or both agreeing.
_ORIGIN_LABEL = {"playbook": "playbook", "llm": "LLM", "both": "both"}
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
    impacts: list[str] = field(default_factory=list)  # formatted impact lines
    track_record: str | None = None  # only when a contributing rule has enough judged calls


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


def _entry(call: AssetCall) -> str:
    """The asset, its move so far, and the "already moved" label where one applies. A call
    with no price yet shows the name alone (SPEC 7.8's "price unavailable")."""
    if call.move is None:
        return call.name
    return f"{call.name} {call.move}" + (f" ({call.label})" if call.label else "")


def impact_lines(
    impacts: Sequence[Impact],
    assets: dict[str, AssetConfig],
    limit: int,
    labels: dict[int, str] | None = None,
) -> list[str]:
    """Impact lines for one story, rendering what `app.presentation` decided: first-order
    before second-order, then by confidence. Assets sharing a mechanism share a line, an asset
    called both ways becomes one "mixed signals" line, and the rest becomes "+N more"."""
    calls = story_calls(impacts, assets, limit, labels)
    lines = [
        f"{MIXED} {_text(call.name)} · mixed signals: {_text(call.mechanism)}"
        for call in calls.shown
        if call.conflict
    ]

    # Assets that share a direction, order, confidence and mechanism share a line.
    by_line: dict[tuple[str, str, str, str], list[AssetCall]] = {}
    for call in calls.shown:
        if call.conflict:
            continue
        key = (call.direction, call.order, call.confidence, call.mechanism)
        by_line.setdefault(key, []).append(call)

    for (direction, order, confidence, mechanism), group in by_line.items():
        agree_count = max(call.rule_count for call in group)
        agree = f" · {agree_count} rules" if agree_count > 1 else ""
        # Say which window the move covers: the track-record line uses a different one.
        window = " · since news" if any(call.priced for call in group) else ""
        origins = sorted({origin for call in group for origin in call.origins})
        origin = "+".join(_ORIGIN_LABEL.get(name, name) for name in origins)
        arrow = UP if direction == "up" else DOWN
        names = ", ".join(_entry(call) for call in group)
        lines.append(
            f"{arrow} {_text(names)}{window} · {_ORDER_LABEL[order]} · "
            f"{confidence} · {origin}{agree} — {_text(mechanism)}"
        )

    if calls.extra:
        rest = ", ".join(
            f"{UP if call.direction == 'up' else DOWN} {_text(call.name)}" for call in calls.extra
        )
        lines.append(f"+{len(calls.extra)} more: {rest}")
    return lines


def digest_item(
    story: Story,
    assets: dict[str, AssetConfig] | None = None,
    max_impacts: int = 6,
    labels: dict[int, str] | None = None,
    track_record: str | None = None,
) -> DigestItem:
    return DigestItem(
        headline=story.headline,
        summary=story.summary or "",
        category=story.category,
        regions=list(story.regions or []),
        disagreement_note=story.disagreement_note if story.sources_disagree else None,
        sources=pick_sources(story.articles),
        impacts=impact_lines(story.impacts, assets, max_impacts, labels) if assets else [],
        track_record=track_record,
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
    lines += item.impacts
    if item.track_record:
        lines.append(f"<i>{_text(item.track_record)}</i>")
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
        text = format_story(replace(item, summary=summary))
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
    header = f"<b>BASIS digest</b> · {local:%a %d %b %Y, %H:%M} {local.tzname()} · {count}"

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
