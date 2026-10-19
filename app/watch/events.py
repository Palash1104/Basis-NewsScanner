"""Two cheap facts about a company headline that embeddings get wrong, used by the scanner's
grouping (app/watch/group.py, app/watch/scan.py):

* Its money amounts. "BDL's ₹56.8 Crore Missile-Sight Tender Raises Defence Procurement
  Questions" and "Defence Ministry signs over ₹810 crore contract with BDL" read alike to the
  embedding model - same company, same ministry words - and were grouped as one story on
  2026-10-15. The call then summarized both and the alert showed one headline with the
  other's reason, rated high positive. Two headlines stating different amounts in the same
  currency are two events, whatever else they share.

* Whether it is about an order or contract. NSE's order filings all say "Bagging/Receiving of
  orders/contracts" and nothing else, so similarity can't tell which order they are, and a
  vague "Expands Defense Order Book" headline resembles a "Bags ₹1,307 Crore Navy Contract"
  one less than it resembles nothing. Order news about one stock within a few hours of
  itself is one event unless the amounts disagree (user, 2026-10-16).

* Whether it is share-price commentary or an event. To the embedding model every headline
  about Zen Technologies resembles every other, so "Zen Technologies Share Price Rises 5.39% on
  Valuation View" (13 Oct) took in the Cogknit acquisition four days later - NSE's filing, its
  press release and 12 reports - and the acquisition was never called on its own. A story
  begun by commentary takes no event (user, 2026-10-19); commentary may still join an event's
  story ("Zen shares rise after Cogknit deal" belongs with the deal).
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

# "₹1,307 crore", "Rs 56.8Cr", "Rs. 1307 Crore", "INR 810 cr", "$150M", "USD 2.5 billion",
# and "56.8 crore rupees". A figure counts only with a currency mark AND a unit (or "rupees"
# after it): "1.4 crore shares" is a count, "5 Key Projects" a number, and "₹114" or "₹1,600"
# a share price, which a report of the same order may or may not quote.
_NUMBER = r"(\d{1,3}(?:,\d{2,3})+|\d+)(?:\.(\d+))?"
_UNIT = (
    r"(lakh\s*crore|lakh\s*cr|crores?|cr\.?|lakhs?|lacs?|million|mn|m|billion|bn|b|"
    r"trillion|tn)"
)
_MONEY = re.compile(
    rf"(?<![\w.])(?:(₹|rs\.?|inr|\$|usd|us\$)\s*{_NUMBER}\s*{_UNIT}(?![a-z])"
    rf"|{_NUMBER}\s*(crores?|cr|lakh\s*crore|lakhs?)\s+rupees?(?![a-z]))",
    re.I,
)
_SCALE = {
    "lakhcrore": 1e12,
    "lakhcr": 1e12,
    "crore": 1e7,
    "crores": 1e7,
    "cr": 1e7,
    "lakh": 1e5,
    "lakhs": 1e5,
    "lac": 1e5,
    "lacs": 1e5,
    "million": 1e6,
    "mn": 1e6,
    "m": 1e6,
    "billion": 1e9,
    "bn": 1e9,
    "b": 1e9,
    "trillion": 1e12,
    "tn": 1e12,
}
# How far apart two figures for one deal may be: reports round ("₹1,300 crore" for 1,307).
SAME_AMOUNT = 0.05

ORDER_WORDS = re.compile(
    r"(?<!\w)(orders?|order book|contracts?|bags?|bagged|bagging|wins?|won|secures?|secured|"
    r"letter of (award|intent)|LoA|LoI|L1 bidder|lowest bidder|supply deal)(?!\w)",
    re.I,
)


@dataclass(frozen=True)
class Amount:
    currency: str  # INR | USD
    value: float  # in units of the currency

    def same_as(self, other: "Amount") -> bool:
        if self.currency != other.currency:
            return False
        larger = max(self.value, other.value)
        return larger == 0 or abs(self.value - other.value) / larger <= SAME_AMOUNT


def amounts(text: str) -> frozenset[Amount]:
    """The money amounts stated in `text`."""
    found = set()
    for match in _MONEY.finditer(text):
        if match.group(1) is not None:
            mark, whole, fraction, unit = match.group(1, 2, 3, 4)
        else:
            mark, (whole, fraction, unit) = "₹", match.group(5, 6, 7)
        number = float(whole.replace(",", "") + (f".{fraction}" if fraction else ""))
        currency = "USD" if mark.lower() in ("$", "usd", "us$") else "INR"
        key = re.sub(r"\s+", "", unit.lower()).rstrip(".")
        found.add(Amount(currency, number * _SCALE[key]))
    return frozenset(found)


def amounts_conflict(first: Iterable[Amount], second: Iterable[Amount]) -> bool:
    """True when both state an amount in a currency they share and none of them agree:
    "₹56.8 crore" and "₹810 crore" are different deals. Nothing stated, or only different
    currencies, is no conflict."""
    left, right = list(first), list(second)
    shared = {a.currency for a in left} & {b.currency for b in right}
    if not shared:
        return False
    return not any(a.same_as(b) for a in left for b in right if a.currency in shared)


def is_order_news(text: str) -> bool:
    """A headline or filing about an order or contract won."""
    return ORDER_WORDS.search(text) is not None


# Something the company did or that happened to it. Words that also turn up in market chatter
# are left out on purpose: "losses" ("extends losses"), "profit booking", "under fire".
EVENT_WORDS = re.compile(
    r"(?<!\w)(acquir\w*|acquisition|takeover|merger|amalgamat\w*|demerger|stake|"
    r"joint venture|JV|subsidiary|invests?|investment|tie-?up|partner(s|ship|ed)?|MoU|"
    r"agreements?|deal|results?|earnings|net profit|net loss|profit (rises|falls|jumps|drops|"
    r"declines|surges|up|down)|revenue|EBITDA|Q[1-4]|Q[1-4]\s?FY\d*|FY\d+ results|guidance|"
    r"dividend|bonus|buyback|split|rights issue|fund ?rais\w*|QIP|IPO|NCDs?|allot\w*|pledg\w*|"
    r"warrants?|preferential|appoint\w*|resign\w*|steps? down|CEO|CFO|MD|chairman|director|"
    r"raids?|searches|income tax|GST|penalty|show cause|notice|SEBI|NCLT|tribunal|court|"
    r"fire at|fire in|blaze|explosion|accident|strike|launch\w*|unveil\w*|deliver\w*|"
    r"commission\w*|tender|plant|facility)(?!\w)",
    re.I,
)
# The share price itself: a move with its size, the price page, the valuation, a list of
# stocks to buy or watch.
COMMENTARY_WORDS = re.compile(
    r"(share|stock) price|price today|prices? and data|historical prices|price targets?|"
    r"target price|(?<!\w)(rises?|rose|jumps?|surges?|soars?|gains?|falls?|fell|slips?|"
    r"drops?|tanks?|declines?|plunges?|rall(y|ies)|climbs?)(?!\w).{0,30}\d+(\.\d+)?\s?%|"
    r"VWAP|52-week|all-time high|top (gainers|losers)|valuation|best stock|"
    r"stocks? to (buy|watch)|buzzing stocks|rise streak|multibaggers?|look at the numbers|"
    r"key financial ratios|peer comparison",
    re.I,
)


def is_event(text: str) -> bool:
    """A headline reporting an event of its own: a money amount, an order, or an event word.
    A filing is always one, whatever its words (the scanner decides that, not this)."""
    return bool(amounts(text)) or is_order_news(text) or EVENT_WORDS.search(text) is not None


def is_commentary(text: str) -> bool:
    """A headline about the share price itself, with no event of its own: "Share Price Rises
    5.39% on Valuation View", "6 Defence stocks turn multibaggers", "Buzzing stocks: ...".
    "Wins ₹1,307 Crore Navy Order; Shares Rise 0.13%" is an event."""
    return COMMENTARY_WORDS.search(text) is not None and not is_event(text)
