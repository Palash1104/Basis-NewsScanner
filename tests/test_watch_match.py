"""The watchlist matcher, on real headlines from the alias review (2026-10-07/08).

Each case is a headline BASIS actually fetched. The ones marked "was wrong" are mistakes an
earlier rule made, kept here so they stay fixed.
"""

import pytest

from app.config import WatchAliases, WatchItem, load_watchlist
from app.watch.match import Matcher, listed_with_others, template_page


@pytest.fixture(scope="module")
def matcher() -> Matcher:
    return Matcher(load_watchlist())


def verdicts(matcher: Matcher, title: str, snippet: str = "") -> dict[str, str]:
    return {m.symbol.removesuffix(".NS"): m.verdict for m in matcher.match(title, snippet)}


# ---------------------------------------------------------------- about the company


@pytest.mark.parametrize(
    ("title", "symbol"),
    [
        (
            "Prime Focus shares tank 8% after Income Tax raids at Mumbai offices: Exclusive",
            "PFOCUS",
        ),
        ("Income Tax raids at Prime Focus, CA’s premises; shares tank over 9%", "PFOCUS"),
        ("Brahma AI raises $150 million round led by Multiples", "PFOCUS"),
        ("Ramayana producer Namit Malhotra’s offices raided by Income Tax Department", "PFOCUS"),
        (
            "Apollo Micro Systems offers to acquire Premier Explosives shares for Rs. 705.65",
            "APOLLO",
        ),
        ("HAL hands over two LCA trainer plane to Air Force", "HAL"),
        ("Fighter strength falling, Tejas delays a challenge, says IAF Chief", "HAL"),
        ("HAL to fully acquire Hatsoff Helicopter Training from CAE", "HAL"),
        ("BDL signs Rs 811 crore contract with Defence Ministry for SAT-SAAW", "BDL"),
        (
            "Aequs Board Approves ₹650 Crore Promoter Warrant Funding For Capacity Expansion",
            "AEQUS",
        ),
        ("Astra Microwave closes trading window ahead of Q2FY27 results", "ASTRAMICRO"),
        # Was wrong: "Target Prices" missed as a plural, and no market word for "upside".
        (
            "HAL Delivery Push Draws Bullish Calls From CLSA, Citi, Goldman Sachs With Upto 27% "
            "Upside — Check Target Prices",
            "HAL",
        ),
        # Was wrong: Hindi headlines had no English market word ("HAL clarifies… Rafale deal").
        ("HAL ने स्पष्ट किया कि 114 Rafale सौदे पर कोई आधिकारिक सूचना प्राप्त नहीं हुई।", "HAL"),
        # Was wrong: "aerospace" and "training" were not HAL vocabulary.
        (
            "From Training Pipeline To Civil Skies: HAL’s Three-Pronged Push For Indian Aerospace",
            "HAL",
        ),
        # Was wrong: read as a comparison, though "Bull vs Bear" compares no companies.
        ("Bull vs Bear on HAL Shares: Will defence stock rise 71% or fall 9%?", "HAL"),
        # Was wrong: read as a list, though its commas list brokers and products.
        (
            "HAL Shares Rise as Citi, Goldman Sachs Retain Buy Ratings: Tejas, HTT-40 and Dhruv "
            "NG Deliveries in Focus",
            "HAL",
        ),
        # Was wrong: a director leaving had no market word.
        ("Data Patterns independent director steps down citing other commitments", "DATAPATTNS"),
        # Two companies in one clause is a partnership story, not a list.
        ("Adani emerges key Russia defence partner in India; HAL, Bharat Dynamics on board", "HAL"),
        # Was wrong (second round): "fighter upgrades" counted as a company in the list.
        (
            "Russia partners Adani Defence, HAL, BDL for missiles, fighter upgrades, defence "
            "production: Report",
            "HAL",
        ),
        # Was wrong (second round): tax and regulatory words were not market words.
        (
            "Ramayana producer Namit Malhotra’s office under income tax scanner ahead of Ranbir "
            "Kapoor-Yash film’s release",
            "PFOCUS",
        ),
        # Was a mention when the rule counted commas; it is the company's results.
        (
            "PFOCUS Q2 2026 Earnings: Revenue Climbs 29.66% to ₹4,587 Crore as EPS Reaches ₹3.92, "
            "Stock Slips 5.27%",
            "PFOCUS",
        ),
        ("Brahma AI secures $150 million, valued $2B, aims 1% market share", "PFOCUS"),
    ],
)
def test_news_about_the_company_is_kept(matcher: Matcher, title: str, symbol: str) -> None:
    assert verdicts(matcher, title).get(symbol) == "keep"


# ---------------------------------------------------------------- one name among many


@pytest.mark.parametrize(
    ("title", "symbol"),
    [
        ("Buy, Sell Or Hold: HAL, Bharat Electronics, Bharat Dynamics, Mazagon Dock", "HAL"),
        (
            "India’s defence boom enters execution phase: HAL, BEL, BDL and others face "
            "delivery test",
            "BDL",
        ),
        (
            "Bharat Electronics vs HAL vs Mazagon Dock: Side-by-Side Comparison of India's Top "
            "Defence Stocks",
            "HAL",
        ),
        ("Better Defence Stock: GRSE vs Data Patterns", "DATAPATTNS"),
        ("Stocks to watch today: PC Jeweller, Sheetal Universal, Bharat Dynamics and more", "BDL"),
        ("Prime Focus Ltd gains for fifth session", "PFOCUS"),
        ("Price Action: Optiemus Infracom jumps 12%; Prime Focus falls over 2%", "PFOCUS"),
        (
            "Prime Focus - 14 smallcap stocks defy market selloff, rally up to 105% in 3 month",
            "PFOCUS",
        ),
        ("Apollo Micro Systems (NSEI:APOLLO) Stock Forecast & Analyst Predictions", "APOLLO"),
        ("Paras Defence, Data Patterns, BEL, HAL and more: Why are defence stocks on fire?", "HAL"),
        # Was wrong (second round): the last name in a list carries the whole list's verb.
        (
            "Defence stocks rally: Raymond, Unimech Aerospace, Apollo Micro Systems, Aequs gain up "
            "to 10%",
            "AEQUS",
        ),
        # Was wrong (second round): "other defence stocks" marks a list.
        (
            "HAL, Astra Microwave, other defence stocks rally up to 4% after DAC clears Rs 1.10L "
            "cr proposals",
            "ASTRAMICRO",
        ),
        (
            "Waaree Energies, Astra Microwave, Texmaco Rail among stocks to turn ex-dividend",
            "ASTRAMICRO",
        ),
        ("NSE, BDL, Ola Electric among buzzing stocks as SENSEX, NIFTY50 tumble", "BDL"),
    ],
)
def test_one_name_in_a_list_is_a_mention(matcher: Matcher, title: str, symbol: str) -> None:
    assert verdicts(matcher, title).get(symbol) == "mention"


# ---------------------------------------------------------------- not about the company


@pytest.mark.parametrize(
    ("title", "symbol"),
    [
        ("Rare Blue-Eyed White Tiger Tejas Reaches Dehradun Zoo After 556 Km Journey", "HAL"),
        ("Konkan Railway Disruption Delays Tejas, Jan Shatabdi", "HAL"),
        ("Tejas Networks hits milestone with 17,000 routers delivered for BharatNet", "HAL"),
        ("Move Over, HAL. New AI Thinks Like a Chip Designer", "HAL"),
        ("HAL Junior Executive Recruitment 2026: 150 Posts, Apply Online from October 7th", "HAL"),
        ("India, Japan Air Force Chiefs fly the Tejas during Exercise Veer Guardian-2026", "HAL"),
        ("Card payments surge 130% in Lebanon: BDL outlines measures to curb cash economy", "BDL"),
        ("Windsor Locks Bradley International (BDL) (KBDL) live departures", "BDL"),
        ("Aequs Foundation’s ‘One Precious Notebook’ campaign transforms education", "AEQUS"),
        ("Namit Malhotra Images, HD Wallpapers, and Photos", "PFOCUS"),
        ("Watch this VFX breakdown by DNEG for ‘Coyote Vs. ACME’", "PFOCUS"),
        # Was wrong (second round): a weak alias in a list skipped the context check.
        (
            "Amnesty law, BDL, Sleiman al-Assad and cocaine: Where do Lebanon’s key legal cases "
            "stand?",
            "BDL",
        ),
        # Was wrong (first live pass): the company's football club.
        ("Hindustan Aeronautics Limited SC", "HAL"),
        # Was wrong (third round): Lebanon's central bank, with "deal" as its market word.
        ("IMF deal: Paris pushes, Beirut acts, BDL still resists", "BDL"),
        # Was wrong (second round): job adverts passed on the "director" market word.
        ("PESB Invites Applications for Director (HR), HAL", "HAL"),
        ("Director (Technical) in BDL Advertised", "BDL"),
    ],
)
def test_other_things_sharing_a_name_are_dropped(matcher: Matcher, title: str, symbol: str) -> None:
    assert verdicts(matcher, title).get(symbol) == "drop"


def test_never_matched_at_all(matcher: Matcher) -> None:
    """Names that only look like a watch stock never reach it: no alias contains them."""
    for title in (
        "Apollo Tyres Share Price Today, Apollo Tyres Stock Price Live NSE/BSE Updates",
        "Apollo Hospitals Q2 results: profit rises 18%",
        "AstraZeneca wins approval for new cancer drug",
        "ABC News Live Prime with Linsey Davis Guest List",
    ):
        assert verdicts(matcher, title) == {}


def test_a_name_only_in_the_snippet_is_a_passing_mention(matcher: Matcher) -> None:
    found = verdicts(
        matcher,
        "Stock markets rebound amid easing crude oil prices; Sensex up 299 points",
        "Gainers included Hindustan Aeronautics and Bharat Dynamics.",
    )
    assert found == {"HAL": "drop", "BDL": "drop"}


# ---------------------------------------------------------------- the pieces


def test_a_list_is_decided_for_each_name_in_its_own_clause() -> None:
    title = "HAL Shares Rise as Citi, Goldman Sachs Retain Buy Ratings: Tejas, HTT-40 and Dhruv"
    assert listed_with_others(title, 0, 3) is None  # HAL: the story
    tejas = title.index("Tejas")
    assert listed_with_others(title, tejas, tejas + 5) == "one of several companies"


def test_template_pages() -> None:
    assert template_page("Stocks to Watch: BDL Defence Deal, Max Estates Land Tie-Up")
    assert template_page("Aequs Ltd. Share Price Today: 5.03% Gain and Market Cap")
    assert template_page("Prime Focus shares tank 8% after Income Tax raids") is None


def test_case_matters_for_weak_aliases_and_capitals() -> None:
    item = WatchItem(
        type="stock",
        symbol="HAL.NS",
        name="HAL",
        aliases=WatchAliases(strong=["Hindustan Aeronautics"], weak=["HAL"]),
        vocab=["aircraft"],
    )
    m = Matcher([item])
    assert m.match("Hal Lindsey shares his aircraft stories") == []  # a first name
    assert m.match("HAL aircraft deliveries resume")[0].verdict == "keep"


def test_commodities_are_never_scanned() -> None:
    items = load_watchlist()
    commodities = [item for item in items if item.type == "commodity"]
    assert commodities and Matcher(commodities).match("Brent crude rises 3% on supply fears") == []
    with pytest.raises(ValueError, match="not news-scanned"):
        WatchItem(type="commodity", symbol="BZ=F", aliases=WatchAliases(strong=["Brent"]))
