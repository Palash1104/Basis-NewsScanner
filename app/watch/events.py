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
