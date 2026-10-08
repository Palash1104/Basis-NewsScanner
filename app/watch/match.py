"""Which watchlist stocks an article is about, decided locally, before any LLM call.

Every article the fast scan fetches passes through here, so this is what keeps the quota for
stories that are really about a stock. Each stock an article touches gets one verdict:

  keep     it is about the company: a strong alias in the headline, or a weak alias in the
           headline with a market word or one of the stock's own vocabulary words.
  mention  the company is one name in a list or comparison ("HAL, BEL, BDL shares: top
           picks", "GRSE vs Data Patterns"), or on a templated page ("Stocks to watch...").
           Shown as context; not analysed, never alerted.
  drop     not about the company: named only in the snippet, a weak alias with no context or
           in noise (an air exercise flying Tejas), or another company sharing the name.

The rules were tuned on ~20,000 real headlines; scripts/watch_alias_review.py re-runs them
and writes every verdict to data/watch_alias_review.md. Mistakes they made on the way, now
covered by tests: "Target Prices" missed as plural; Hindi headlines ("HAL ने स्पष्ट किया…
सौदे") with no English market word; "Bull vs Bear on HAL Shares" read as a comparison; "HAL
Shares Rise as Citi, Goldman Sachs Retain…" read as a list because its commas list brokers.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.config import WatchItem

Verdict = Literal["keep", "mention", "drop"]

# Words that make a weak alias in the headline about the listed company. Hindi headlines are
# common on business sites (Alice Blue, Whalesbook), so the main market words are in Devanagari
# too: shares, order, crore, deal, profit, results, loss.
MARKET_WORDS = re.compile(
    r"(?<!\w)(shares?|stocks?|NSE|BSE|Sensex|Nifty|Q[1-4]\w*|FY\d\d\w*|results?|profit|loss|"
    r"revenue|earnings|orders?|contracts?|stake|deals?|acqui\w+|merger|board|dividend|CEO|MD|"
    r"CFO|CMD|chairman|directors?|resign\w*|steps? down|appoint\w*|ratings?|target prices?|"
    r"upside|downside|bullish|bearish|brokerages?|IPO|crore|cr|guidance|margins?|sales|"
    r"plant|capex|AGM|EGM|warrants?|valuation|listing|raises?|raised|funding|investments?|"
    r"invests?|open offer|preferential|allotment|clarif\w*|raid\w*|search(es)?|probe|"
    r"penalty|deliver(y|ies)|buy|sell|income tax|I-T|tax department|SEBI|"
    r"Enforcement Directorate|"
    r"शेयर|ऑर्डर|करोड़|सौदा|सौदे|मुनाफा|मुनाफे|नतीजे|घाटा)(?!\w)",
    re.I,
)
# Templated pages that name companies without being news about one: stock-pick lists,
# auto-generated price pages, market-open movers.
TEMPLATE_PAGE = re.compile(
    r"share price (live|today|news)|live updates|top gainers and losers|stocks? to (watch|buy|"
    r"trade)|stock picks|top picks|market wrap|breakout stocks|stocks in (focus|news)|"
    r"stock forecast|analyst predictions|buy,? sell,? or hold|price action:|stock reactions|"
    r"market open\b|\b\d+\s+(?:[\w-]+\s+){0,3}stocks\b|buzzing stocks|stocks today|"
    r"among (\w+ )?stocks|full list|"
    r"(rises?|gains?|falls?|up|down|soars?|slips?) for (the )?(\w+ )?(straight |consecutive )?"
    r"sessions?|share price (rises?|falls?|gains?|jumps?|drops?|slips?)",
    re.I,
)
# Not news at all, whoever it names: job adverts ("PESB Invites Applications for Director
# (HR), HAL" got past the "director" market word).
JOB_ADVERT = re.compile(
    r"recruitment|vacanc\w+|apply online|invites applications|\badvertised\b|notification out|"
    r"walk-?in interview",
    re.I,
)
# Where one clause of a headline ends: a list of names lives inside one clause.
CLAUSE_BREAK = re.compile(r"[:;|—–?!/]|\s-\s")
LIST_SEPARATOR = re.compile(r",|\s(?:and|&)\s")
VERSUS = re.compile(r"\bvs\.?$|^vs\b|\bversus$|^versus\b", re.I)
# What may sit beside a name in its own list item and still leave it "just a name".
BARE_NAME_TAIL = re.compile(r"(shares?|stocks?|ltd\.?|limited)?", re.I)


@dataclass(frozen=True)
class Match:
    symbol: str
    verdict: Verdict
    reason: str
    alias: str | None = None


def _phrase(text: str, case_sensitive: bool) -> re.Pattern[str]:
    """A whole-word match. `&` counts as part of a word, so "L&T" never matches inside
    "L&TFH", and an all-caps alias is matched case-sensitively."""
    return re.compile(rf"(?<![\w&]){re.escape(text)}(?![\w&])", 0 if case_sensitive else re.I)


def _words(words: Sequence[str]) -> re.Pattern[str] | None:
    """Any of `words`, whole, case-insensitive, with a plural allowed ("missiles")."""
    if not words:
        return None
    alternatives = "|".join(re.escape(word) for word in words)
    return re.compile(rf"(?<![\w&])({alternatives})(e?s)?(?![\w&])", re.I)


def template_page(title: str) -> str | None:
    """Why `title` is a templated list or price page, or None."""
    found = TEMPLATE_PAGE.search(title)
    return f"list page ('{found.group(0).strip()}')" if found else None


def listed_with_others(title: str, start: int, end: int) -> str | None:
    """Whether the name at title[start:end] is one item in a list or comparison.

    Decided for this name only, inside its own clause, and only when its item in the run is
    the bare name ("HAL", "BDL shares"): "HAL, BEL, BDL shares: top picks" lists HAL. But in
    "HAL Shares Rise as Citi, Goldman Sachs Retain…" the commas list brokers, and in
    "Fighter strength falling, Tejas delays a challenge, says IAF Chief" they separate
    phrases - in both the name's own item says something about it, so it is the story.
    """
    before = title[:start].rstrip()
    after = title[end:].lstrip()
    if VERSUS.search(before) or VERSUS.search(after):
        return "comparison"
    clause_start = max((m.end() for m in CLAUSE_BREAK.finditer(title, 0, start)), default=0)
    clause_end_match = CLAUSE_BREAK.search(title, end)
    clause_end = clause_end_match.start() if clause_end_match else len(title)
    # Split the clause into items, keeping where each one sits, to find this name's own.
    items: list[tuple[int, int]] = []
    cursor = clause_start
    for separator in LIST_SEPARATOR.finditer(title, clause_start, clause_end):
        items.append((cursor, separator.start()))
        cursor = separator.end()
    items.append((cursor, clause_end))
    own = next(((a, b) for a, b in items if a <= start and end <= b), None)
    if own is None:
        return None
    last = items[-1]
    prefix = title[own[0] : start].strip()
    suffix = title[end : own[1]].strip()
    # This name's own item is just the name - or, as the last item, the name followed by the
    # whole list's verb: "Raymond, Unimech Aerospace, Aequs gain up to 10%".
    bare = BARE_NAME_TAIL.fullmatch(f"{prefix} {suffix}".strip()) or (own == last and not prefix)
    if not bare:
        return None
    # The other items must look like names: capitalized, and short unless they are the last
    # item and carry the verb. "fighter upgrades" in "Russia partners Adani Defence, HAL, BDL
    # for missiles, fighter upgrades" is not a company.
    named = 0
    marker = False
    for a, b in items:
        text = title[a:b].strip()
        if (a, b) == own or not text:
            continue
        if re.match(r"others?\b", text, re.I):
            marker = True  # "HAL, Astra Microwave, other defence stocks rally"
        elif (text[0].isupper() or text[0].isdigit()) and (
            len(text.split()) <= 3 or (a, b) == last
        ):
            named += 1
    if named >= 2 or (named >= 1 and marker):
        return "one of several companies"
    return None


class _Stock:
    def __init__(self, item: WatchItem) -> None:
        self.symbol = item.symbol
        self.strong = [(a, _phrase(a, a.isupper())) for a in item.aliases.strong]
        # Weak aliases are always case-sensitive: "HAL" the company, not "Hal" the name.
        self.weak = [(a, _phrase(a, True)) for a in item.aliases.weak]
        self.vocab = _words(item.vocab)
        self.noise = _words(item.noise)
        self.exclude = [(x, _phrase(x, False)) for x in item.exclude]
        self.stop = [re.compile(pattern) for pattern in item.stop]

    def _clean(self, text: str) -> str:
        """Blank out other companies sharing a name, and the alias used as an ordinary word.
        Blanked with spaces of the same length, so positions in the headline still line up."""
        for _, pattern in self.exclude:
            text = pattern.sub(lambda m: " " * len(m.group(0)), text)
        for pattern in self.stop:
            text = pattern.sub(lambda m: " " * len(m.group(0)), text)
        return text

    def verdict(self, title: str, snippet: str, page: str | None) -> Match | None:
        title_c = self._clean(title)
        all_c = self._clean(f"{title}\n{snippet}")
        hit = next(((a, m) for a, p in self.strong if (m := p.search(title_c))), None)
        strong = hit is not None
        if hit is None:
            hit = next(((a, m) for a, p in self.weak if (m := p.search(title_c))), None)
        if hit is not None:
            alias, found = hit
            if advert := JOB_ADVERT.search(title):
                return Match(self.symbol, "drop", f"job advert ('{advert.group(0)}')", alias)
            if not strong:
                # A weak alias has to earn its place first, in a list as much as anywhere:
                # "Amnesty law, BDL, Sleiman al-Assad and cocaine" lists Lebanon's central bank.
                if self.noise and (noise := self.noise.search(title_c)):
                    return Match(
                        self.symbol, "drop", f"'{alias}' in noise ('{noise.group(0)}')", alias
                    )
                context = MARKET_WORDS.search(all_c) or (self.vocab and self.vocab.search(all_c))
                if not context:
                    return Match(self.symbol, "drop", f"'{alias}' with no context", alias)
            listed = page or listed_with_others(title, found.start(), found.end())
            if listed:
                return Match(self.symbol, "mention", listed, alias)
            how = "named in the headline" if strong else "named, with context"
            return Match(self.symbol, "keep", how, alias)
        named = next((a for a, p in self.strong + self.weak if p.search(all_c)), None)
        if named:
            return Match(self.symbol, "drop", "snippet only: a passing mention", named)
        excluded = next((x for x, p in self.exclude if p.search(f"{title}\n{snippet}")), None)
        if excluded:
            return Match(self.symbol, "drop", f"another company or thing: {excluded}", excluded)
        return None


class Matcher:
    """The watchlist's stocks, compiled once; `match` is called for every fetched article."""

    def __init__(self, items: Sequence[WatchItem]) -> None:
        self._stocks = [_Stock(item) for item in items if item.type == "stock"]

    def match(self, title: str, snippet: str = "") -> list[Match]:
        page = template_page(title)
        found = []
        for stock in self._stocks:
            result = stock.verdict(title, snippet, page)
            if result is not None:
                found.append(result)
        return found
