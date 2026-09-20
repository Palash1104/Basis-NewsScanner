"""Format stories into Telegram HTML messages (SPEC 10)."""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from app.config import AssetConfig
from app.models import Article, Impact, Story
from app.pipeline.dedupe import normalize_source
from app.pipeline.prices import format_move

TELEGRAM_LIMIT = 4096
MAX_SOURCE_LINKS = 3
UP, DOWN, MIXED = "\u25b2", "\u25bc", "\u2195"
_ORDER_RANK = {"first": 0, "second": 1}
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}
_ORDER_LABEL = {"first": "1st", "second": "2nd"}
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


def _display(symbol: str, assets: dict[str, AssetConfig], direction: str) -> str:
    """The asset's short name, saying what "up" means where an arrow is easy to misread."""
    asset = assets.get(symbol)
    if asset is None:
        return symbol
    if direction == "up" and asset.up_means:
        return f"{asset.display_name} ({asset.up_means})"
    return asset.display_name


def _rank(impact: Impact) -> tuple[int, int]:
    return _ORDER_RANK[impact.order], _CONFIDENCE_RANK[impact.confidence]


def _entry(impact: Impact, assets: dict[str, AssetConfig], labels: dict[int, str]) -> str:
    """The asset, its move so far, and the "already moved" label where one applies. An impact
    with no price yet shows the name alone (SPEC 7.8's "price unavailable")."""
    name = _display(impact.symbol, assets, impact.direction)
    asset = assets.get(impact.symbol)
    if asset is None or impact.reference_price is None or impact.move_at_detection_pct is None:
        return name
    move = format_move(asset, impact.reference_price, impact.move_at_detection_pct)
    label = labels.get(impact.id)
    return f"{name} {move}" + (f" ({label})" if label else "")


def impact_lines(
    impacts: Sequence[Impact],
    assets: dict[str, AssetConfig],
    limit: int,
    labels: dict[int, str] | None = None,
) -> list[str]:
    """Impact lines for one story: first-order before second-order, then by confidence.
    Impacts sharing a mechanism share a line, an asset called both ways becomes one "mixed
    signals" line, and anything past `limit` is summarized as "+N more"."""
    labels = labels or {}
    conflicted = sorted({impact.symbol for impact in impacts if impact.conflict})
    lines = []
    for symbol in conflicted:
        mechanisms = dict.fromkeys(i.mechanism for i in impacts if i.symbol == symbol)
        name = assets[symbol].display_name if symbol in assets else symbol
        lines.append(f"{MIXED} {_text(name)} · mixed signals: {_text(' vs '.join(mechanisms))}")

    # One entry per symbol and direction; rules that agree are counted, not repeated.
    grouped: dict[tuple[str, str], list[Impact]] = {}
    for impact in impacts:
        if impact.symbol not in conflicted:
            grouped.setdefault((impact.symbol, impact.direction), []).append(impact)
    entries = [
        (min(same, key=_rank), len({i.rule_id for i in same if i.rule_id}))
        for same in grouped.values()
    ]
    entries.sort(key=lambda entry: _rank(entry[0]))
    shown, extra = entries[:limit], entries[limit:]

    names_by_line: dict[tuple[str, str, str, str], list[str]] = {}
    rules_by_line: dict[tuple[str, str, str, str], int] = {}
    priced_lines: set[tuple[str, str, str, str]] = set()
    for impact, rule_count in shown:
        key = (impact.direction, impact.order, impact.confidence, impact.mechanism)
        names_by_line.setdefault(key, []).append(_entry(impact, assets, labels))
        rules_by_line[key] = max(rules_by_line.get(key, 0), rule_count)
        if impact.move_at_detection_pct is not None and impact.reference_price is not None:
            priced_lines.add(key)
    for key, names in names_by_line.items():
        direction, order, confidence, mechanism = key
        agree = f" · {rules_by_line[key]} rules" if rules_by_line[key] > 1 else ""
        # Say which window the move covers: the track-record line uses a different one.
        window = " · since news" if key in priced_lines else ""
        arrow = UP if direction == "up" else DOWN
        lines.append(
            f"{arrow} {_text(', '.join(names))}{window} · {_ORDER_LABEL[order]} · "
            f"{confidence}{agree} — {_text(mechanism)}"
        )
    if extra:
        rest = ", ".join(
            f"{UP if impact.direction == 'up' else DOWN} "
            f"{_text(_display(impact.symbol, assets, impact.direction))}"
            for impact, _ in extra
        )
        lines.append(f"+{len(extra)} more: {rest}")
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
