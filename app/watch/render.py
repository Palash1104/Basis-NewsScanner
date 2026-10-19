"""How a watchlist item reads in Telegram (user, 2026-10-16).

Every story, wherever it is sent - an instant alert, a follow-up, a "while you were away"
summary, the digest's watchlist section - is the same shape:

    ▲ Bharat Dynamics: Bags ₹1,307 Crore Indian Navy Contract for Heavyweight Torpedoes
    NSE filing 14:56 · first media report 14:59
    One more line only when the call's reason says something the headline doesn't.

The headline links to the article it came from, and drops the company's name when it opens
with it (the name is right before it). A price move is the same two lines:

    ▲ Zen Technologies +2.7% today, +3.8% vs defence index
    15:11 · no news found

Summaries group items by how much they matter: HIGH, MEDIUM, PRICE MOVES, then LOW folded
into an expandable block.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape

from app.config import Settings
from app.models import WatchArticle, WatchCall, WatchFiling, WatchStory
from app.watch.analyse import kept_articles, kept_symbols, reporting_articles, stale_after
from app.watch.moves import MoveEvent

MARK = {"positive": "▲", "negative": "▼", "neutral": "●"}
NO_CALL_MARK = "·"
HEADLINE_CHARS = 110
LOW_SHOWN = 25  # in one expandable block; the rest are counted
SECTIONS = ("high", "medium", "moves", "low")
SECTION_TITLES = {"high": "HIGH", "medium": "MEDIUM", "moves": "PRICE MOVES", "low": "LOW"}
# A reason restating the headline in other words adds nothing: at least this share of its
# words already in the headline means it is left out.
RESTATES = 0.6

_STOP = {
    "a", "an", "the", "of", "for", "from", "to", "in", "on", "at", "by", "with", "and", "or",
    "its", "it", "is", "are", "was", "were", "be", "as", "has", "have", "had", "this", "that",
    "company", "company's", "limited", "ltd", "rs", "inr", "worth", "crore", "cr", "new",
}  # fmt: skip
_WORD = re.compile(r"[\w₹$]+(?:[.,]\d+)*", re.UNICODE)
# What may sit between a company's name and the rest of a headline that opens with it:
# "Data Patterns (India) Ltd: Company Update", "Aequs Ltd - Insider Trading".
_AFTER_NAME = re.compile(
    r"^(?:\s*\([^)]{1,30}\))?(?:\s*(?:Ltd\.?|Limited|Inc\.?|Corp\.?))?(?P<sep>\s*[:|]|\s+[-–—]\s)?\s*",
    re.I,
)
# A headline that goes on like this is a sentence about the company: cutting its name would
# leave "Is Rated Hold".
_KEEP_IF_NEXT = re.compile(r"^(is|are|was|were|has|have|had|'s|’s|’|')(\s|$)", re.I)


def t(value: str) -> str:
    return escape(value, quote=False)


def local(moment: datetime, settings: Settings, ref: datetime | None = None) -> str:
    """HH:MM on `ref`'s day, the day too otherwise."""
    at = moment.astimezone(settings.tz)
    if ref is not None and at.date() == ref.astimezone(settings.tz).date():
        return f"{at:%H:%M}"
    return f"{at:%a %d %b %H:%M}"


def day_word(moment: datetime, settings: Settings, ref: datetime) -> str:
    """ "today", or the day, for a move since the last close."""
    at = moment.astimezone(settings.tz)
    if at.date() == ref.astimezone(settings.tz).date():
        return "today"
    return f"on {at:%a %d %b}"


def _stem(word: str) -> str:
    word = word.lower().replace(",", "")
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(text) if w.lower() not in _STOP and len(w) > 1}


def adds_information(reason: str, headline: str) -> bool:
    """Whether the call's reason says something the headline doesn't."""
    words = _words(reason)
    if not words:
        return False
    return len(words & _words(headline)) / len(words) < RESTATES


def short_headline(headline: str, names: Sequence[str]) -> str:
    """The headline without the company's name when it opens with it - the name is right
    before it in the item - cut at a word near HEADLINE_CHARS. "Bharat Dynamics Bags Rs
    1,307 Crore..." reads "Bags Rs 1,307 Crore...", "Aequs Ltd: Insider Trading / SAST"
    reads "Insider Trading / SAST"; "Data Patterns (India) Ltd is Rated Hold" is left
    whole."""
    text = headline.strip()
    for name in sorted(names, key=len, reverse=True):
        if not name or not text.lower().startswith(name.lower()):
            continue
        after = text[len(name) :]
        if after[:1].isalnum():
            break  # "HALO..." is not "HAL"
        found = _AFTER_NAME.match(after)
        rest = after[found.end() :].strip() if found else after.strip()
        enough = 2 if found and found.group("sep") else 3
        if rest and not _KEEP_IF_NEXT.match(rest) and len(rest.split()) >= enough:
            text = rest[0].upper() + rest[1:]
        break
    if len(text) > HEADLINE_CHARS:
        cut = text[: HEADLINE_CHARS - 1].rsplit(" ", 1)[0].rstrip(",;:-–— ")
        text = f"{cut}…"
    return text


def story_link(story: WatchStory) -> str | None:
    """Where the headline points: the first report BASIS saw, or the filing."""
    articles = sorted(kept_articles(story), key=lambda a: a.first_seen_at)
    if articles:
        return articles[0].url
    filings = sorted((f for f in story.filings if f.link), key=lambda f: f.filed_at)
    return filings[0].link if filings else None


def _linked(text: str, url: str | None) -> str:
    return f'<a href="{escape(url, quote=True)}">{t(text)}</a>' if url else t(text)


def lead(calls: Sequence[WatchCall], story: WatchStory, names: Mapping[str, str]) -> str:
    """ "▲ <b>HAL</b> · ▲ <b>Bharat Dynamics</b>", from the calls, or the companies the
    story is about when it has not been called yet."""
    if calls:
        return " · ".join(
            f"{MARK[c.sentiment]} <b>{t(names.get(c.symbol, c.symbol))}</b>" for c in calls
        )
    symbols = sorted(kept_symbols(story), key=lambda s: names.get(s, s))
    return " · ".join(f"{NO_CALL_MARK} <b>{t(names.get(s, s))}</b>" for s in symbols)


def status_line(story: WatchStory, settings: Settings, ref: datetime) -> str:
    """When it came out and how far it has got: "NSE filing 14:56 · first media report
    14:59", "07:26 · media only, not filed", "first media report 10:23 · NSE asked for
    clarification 11:05 · company replied 15:33"."""
    return _status(reporting_articles(story, stale_after(settings)), story.filings, settings, ref)


def _status(
    articles: Sequence[WatchArticle],
    filings: Sequence[WatchFiling],
    settings: Settings,
    ref: datetime,
) -> str:
    steps: list[tuple[datetime, str]] = []
    if articles:
        first = min(min(a.published_at, a.first_seen_at) for a in articles)
        steps.append((first, "first media report"))
    seen_exchanges: set[tuple[str, str]] = set()
    for filing in sorted(filings, key=lambda f: f.filed_at):
        label = _filing_step(filing)
        if (filing.exchange, filing.kind) in seen_exchanges:
            continue
        seen_exchanges.add((filing.exchange, filing.kind))
        steps.append((filing.filed_at, label))
    steps.sort(key=lambda step: step[0])
    if articles and not filings:
        return f"{local(steps[0][0], settings, ref)} · media only, not filed"
    return " · ".join(f"{label} {local(at, settings, ref)}" for at, label in steps)


# Some of a story's articles and filings: news that came into a story BASIS already had.
Items = tuple[Sequence[WatchArticle], Sequence[WatchFiling]]


def _parts(
    story: WatchStory, settings: Settings, ref: datetime, items: Items | None
) -> tuple[str, str | None, str]:
    """An item's headline, link and status line: the story's own, or - for news that came
    into a story BASIS already had (app/watch/alerts.py `new_news`) - its first report's (the
    filing's, when there is no report), with a status line from those items alone."""
    if items is None:
        return story.headline, story_link(story), status_line(story, settings, ref)
    articles, filings = items
    if articles:
        first = min(articles, key=lambda a: (min(a.published_at, a.first_seen_at), a.id or 0))
        headline, link = first.title, first.url
    else:
        filing = min(filings, key=lambda f: f.filed_at)
        headline, link = f"{filing.company}: {filing.subject}", filing.link or None
    return headline, link, _status(articles, filings, settings, ref)


def _filing_step(filing: WatchFiling) -> str:
    if filing.kind == "clarification_sought":
        return f"{filing.exchange} asked for clarification"
    if filing.kind == "company_reply":
        return "company replied"
    return f"{filing.exchange} filing"


def story_names(story: WatchStory, names: Mapping[str, str]) -> list[str]:
    return [names.get(s, s) for s in kept_symbols(story)]


def story_item(
    story: WatchStory,
    calls: Sequence[WatchCall],
    names: Mapping[str, str],
    settings: Settings,
    ref: datetime,
    reason: bool = True,
    items: Items | None = None,
) -> str:
    """Two lines, three when the reason adds something. `items`: only these of the story's
    articles and filings (`_parts`)."""
    full, link, status = _parts(story, settings, ref, items)
    headline = short_headline(full, story_names(story, names))
    lines = [
        f"{lead(calls, story, names)}: {_linked(headline, link)}",
        f"<i>{status}</i>" + ("" if calls else " · <i>not analysed yet</i>"),
    ]
    if reason and calls and adds_information(calls[0].reason, full):
        lines.append(t(calls[0].reason))
    return "\n".join(lines)


def low_item(
    story: WatchStory,
    calls: Sequence[WatchCall],
    names: Mapping[str, str],
    settings: Settings,
    ref: datetime,
    when: datetime,
    items: Items | None = None,
) -> str:
    """One line, for the folded LOW block."""
    full, link, _ = _parts(story, settings, ref, items)
    headline = short_headline(full, story_names(story, names))
    return f"{lead(calls, story, names)}: {_linked(headline, link)} · {local(when, settings, ref)}"


def move_lines(
    event: MoveEvent, names: Mapping[str, str], settings: Settings, ref: datetime
) -> list[str]:
    """A price or sector move: what moved and by how much, then when and why it matters."""
    mark = MARK["positive" if event.move > 0 else "negative"]
    when = local(event.at, settings, ref)
    day = day_word(event.at, settings, ref)
    if event.kind == "sector":
        group = (event.group or "sector").capitalize()
        peers = " · ".join(f"{t(names.get(s, s))} {m:+.1%}" for s, m in event.peers)
        lines = [
            f"{mark} <b>{t(group)} index</b> {event.move:+.1%} {day}",
            f"<i>{when} · peers moved together, so no stock is alerted on its own</i>",
        ]
        if peers:
            lines.append(peers)
        return lines
    name = names.get(event.symbol or "", event.symbol or "")
    net = f", {event.excess:+.1%} vs {t(event.group)} index" if event.excess is not None else ""
    return [
        f"{mark} <b>{t(name)}</b> {event.move:+.1%} {day}{net}",
        f"<i>{when} · no news found · {event.multiple:.1f}× its typical day</i>",
    ]


@dataclass
class Sections:
    """Items sorted into HIGH / MEDIUM / PRICE MOVES / LOW, each newest first."""

    high: list[tuple[datetime, str]]
    medium: list[tuple[datetime, str]]
    moves: list[tuple[datetime, str]]
    low: list[tuple[datetime, str]]

    @classmethod
    def empty(cls) -> "Sections":
        return cls([], [], [], [])

    def add(self, section: str, when: datetime, text: str) -> None:
        getattr(self, section).append((when, text))

    def __len__(self) -> int:
        return sum(len(getattr(self, name)) for name in SECTIONS)

    def blocks(self, spaced: bool = True) -> list[str]:
        """Blocks for `alerts.split`: never split inside one. The LOW items are one block
        in an expandable quote, so a split can't break its HTML."""
        out: list[str] = []
        gap = "\n" if spaced else ""
        for name in SECTIONS:
            items = [
                text for _, text in sorted(getattr(self, name), key=lambda p: p[0], reverse=True)
            ]
            if not items:
                continue
            title = f"<b>{SECTION_TITLES[name]}</b>"
            if name == "low":
                shown, rest = items[:LOW_SHOWN], items[LOW_SHOWN:]
                more = f"\n+{len(rest)} more on the watchlist page" if rest else ""
                out.append(
                    f"\n{title} · {len(items)}\n<blockquote expandable>"
                    + "\n".join(shown)
                    + more
                    + "</blockquote>"
                )
                continue
            out.append(f"\n{title}\n{items[0]}")
            out += [f"{gap}{item}" for item in items[1:]]
        return out


def section_for(calls: Sequence[WatchCall]) -> str:
    """Where a story goes: its most material call's level. A story not called yet goes
    under MEDIUM, never folded away unread."""
    return calls[0].materiality if calls else "medium"


def time_range(start: datetime, end: datetime, settings: Settings) -> str:
    """ "Thu 15 Oct, 00:04 – 19:07", or both days when it spans two."""
    a, b = start.astimezone(settings.tz), end.astimezone(settings.tz)
    if a.date() == b.date():
        return f"{a:%a %d %b}, {a:%H:%M} – {b:%H:%M}"
    return f"{a:%a %d %b %H:%M} – {b:%a %d %b %H:%M}"
