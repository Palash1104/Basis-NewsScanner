"""The scanner's grouping on real items: what went wrong, and the rules that fix it
(app/watch/events.py).

tests/fixtures/watch_grouping_bdl.json, 2026-10-15:
1. Story 137 mixed two events, a Rs 56.8 crore tender and an unrelated Rs 810 crore contract.
   The call summarized both, and the "while you were away" item showed the tender headline
   with the contract's reason, rated high positive. Different amounts are now different
   stories, however alike the embedding model finds the headlines.
2. NSE's order filing (14:56 IST) and the Rs 1,307 crore torpedo order reports (14:56-15:39)
   came out as two items. An NSE order filing says nothing but "Bagging/Receiving of
   orders/contracts", so it now joins the stock's order story reported closest to it, and
   a vague order headline joins one when similarity finds it nothing.

tests/fixtures/watch_grouping_commentary.json, 9-19 Oct:
3. Stories begun by share-price commentary took in events: Zen's Cogknit acquisition (NSE's
   filing, the press release, 12 reports) joined "Zen Technologies Share Price Rises 5.39% on
   Valuation View" and was never called on its own; BDL's torpedo order and a tender joined a
   "rise streak" note; Astra Microwave's Q1 results joined a multibagger list. Such a story now
   takes no event.
"""

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, WatchlistFile, load_watchlist_file
from app.db import init_db, make_engine, make_session_factory
from app.models import WatchArticle, WatchFiling, WatchMatch, WatchStory
from app.pipeline.embed import article_text
from app.pipeline.fetch import SourceResolver
from app.watch.events import amounts, amounts_conflict, is_commentary, is_event, is_order_news
from app.watch.scan import Watcher

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = json.loads((FIXTURES / "watch_grouping_bdl.json").read_text(encoding="utf-8"))
COMMENTARY = json.loads((FIXTURES / "watch_grouping_commentary.json").read_text(encoding="utf-8"))
SHARE_PRICE = "share-price commentary"


class SameEmbedder:
    """The worst case: the model finds every Bharat Dynamics headline the same."""

    name = "same"

    def embed(self, texts: list[str]) -> np.ndarray:
        vectors = np.zeros((len(texts), 8), dtype=np.float32)
        vectors[:, 0] = 1.0
        return vectors


class ApartEmbedder:
    """The other worst case: it finds no two alike."""

    name = "apart"

    def embed(self, texts: list[str]) -> np.ndarray:
        size = 4096
        vectors = np.zeros((len(texts), size), dtype=np.float32)
        for row, text in enumerate(texts):
            vectors[row, hash(text) % size] = 1.0
        return vectors


@pytest.fixture
def db(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(tmp_path / "grouping.db")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def watchlist() -> WatchlistFile:
    return load_watchlist_file()


def regroup(
    settings: Settings,
    db: sessionmaker[Session],
    watchlist: WatchlistFile,
    embedder: object,
    same_day: str | None = None,
) -> dict[str, set[int]]:
    """Run the fixture through the scanner's grouping in one pass, as the catch-up did, and
    return each real event's set of story ids."""
    watcher = Watcher(settings, db, watchlist, [], SourceResolver([]), embedder, None)  # type: ignore[arg-type]
    events: dict[int, str] = {}
    with db() as session:
        articles, filings = [], []
        for item in FIXTURE["articles"]:
            article = WatchArticle(
                url=f"https://news.example.com/{abs(hash(item['title']))}",
                source_name=item["source"],
                title=item["title"],
                snippet="",
                published_at=datetime.fromisoformat(item["published_at"]),
                first_seen_at=datetime.fromisoformat(item["first_seen_at"]),
            )
            article.matches = [WatchMatch(symbol="BDL.NS", verdict="keep", reason="named")]
            articles.append((article, item["event"]))
        for item in FIXTURE["filings"]:
            filing = WatchFiling(
                key=item["key"],
                exchange=item["exchange"],
                symbol=item["symbol"],
                company=item["company"],
                subject=item["subject"],
                description=item["description"],
                link=item["link"],
                filed_at=datetime.fromisoformat(item["filed_at"]),
                first_seen_at=datetime.fromisoformat(item["first_seen_at"]),
                kind=item["kind"],
            )
            filings.append((filing, item["event"]))
        session.add_all([a for a, _ in articles] + [f for f, _ in filings])
        session.flush()
        watcher._warm(
            [article_text(a.title, a.snippet) for a, _ in articles]
            + [article_text(f.subject, f.description) for f, _ in filings]
        )
        now = max(datetime.fromisoformat(i["first_seen_at"]) for i in FIXTURE["articles"])
        watcher._group(session, [], [a for a, _ in articles], [f for f, _ in filings], now)
        session.flush()
        for item, event in [*articles, *filings]:
            events[item.story_id] = events.get(item.story_id, event)  # type: ignore[index]
            assert events[item.story_id] == event, f"{item.story_id} mixes {event} and more"  # type: ignore[index]
        found: dict[str, set[int]] = {}
        for item, event in [*articles, *filings]:
            when = item.published_at if isinstance(item, WatchArticle) else item.filed_at
            if same_day is not None and not when.isoformat().startswith(same_day):
                continue
            found.setdefault(event, set()).add(item.story_id)  # type: ignore[arg-type]
        session.commit()
    return found


def test_the_fixture_is_the_real_mix_up() -> None:
    stored = {item["stored_story"] for item in FIXTURE["articles"]}
    assert stored == {135, 137, 139}
    in_137 = {item["event"] for item in FIXTURE["articles"] if item["stored_story"] == 137}
    assert in_137 == {"56.8 tender", "810 contract"}  # two events in one story


def test_a_different_amount_is_a_different_story_even_when_the_model_sees_one(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    found = regroup(settings, db, watchlist, SameEmbedder())  # asserts no story mixes events
    assert len(found["56.8 tender"]) == 1 and len(found["810 contract"]) == 1
    assert found["56.8 tender"] != found["810 contract"]


def test_the_order_filing_and_the_order_reports_are_one_story(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    found = regroup(settings, db, watchlist, SameEmbedder())
    assert len(found["torpedo order"]) == 1
    assert found["810 contract"].isdisjoint(found["torpedo order"])  # 14 hours earlier


@pytest.mark.parametrize(
    "embedder", [ApartEmbedder(), None], ids=["model-finds-nothing", "no-model"]
)
def test_order_news_groups_even_when_similarity_finds_nothing(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, embedder: object
) -> None:
    """The filing, the vague "order book" headline and every report from the same
    afternoon land on one story. EquityBulls' report the next morning is 17 hours later:
    joining it is the model's job, not the order rule's."""
    found = regroup(settings, db, watchlist, embedder, same_day="2026-10-15")
    assert len(found["torpedo order"]) == 1


def test_amounts_are_read_the_ways_headlines_write_them() -> None:
    tender = amounts("BDL’s ₹56.8 Crore Missile-Sight Tender Raises Defence Procurement Questions")
    assert tender == amounts("Hyderabad defence firm gets Rs 56.8Cr BDL single tender")
    contract = amounts("Defence Ministry signs over ₹810 crore contract with BDL for 160 SAT-SAAW")
    assert amounts_conflict(tender, contract)
    torpedo = amounts("Bharat Dynamics BDL Rs 1307 Crore Navy Torpedo Order 2026")
    assert not amounts_conflict(torpedo, amounts("BDL bags Rs 1,307 crore Navy contract"))
    assert not amounts_conflict(torpedo, amounts("BDL bags Rs 1,300 crore Navy contract"))
    # Counts, share prices and dollars against rupees are no conflict.
    assert amounts("Apollo Micro Systems lists 1.4 crore shares at ₹114") == frozenset()
    assert not amounts_conflict(torpedo, amounts("Brahma AI raises $150M"))
    assert not amounts_conflict(torpedo, frozenset())


def test_order_news_is_recognised() -> None:
    assert is_order_news("Bagging/Receiving of orders/contracts")
    assert is_order_news("Bharat Dynamics Expands Defense Order Book Following Recent Contracts")
    assert not is_order_news("BDL’s ₹56.8 Crore Missile-Sight Tender Raises Questions")
    assert not is_order_news("HAL to Raise AL-31FP Engine Output to 50 a Year")


# ---------------------------------------------------------------- commentary takes no event


class NearlyAlike:
    """The model as it behaves on one company's headlines: everything about the stock looks
    alike (0.8, past both thresholds), one event's reports a little more alike (1.0)."""

    name = "nearly-alike"

    def __init__(self) -> None:
        labels = sorted(
            {i["event"] for i in COMMENTARY["articles"]}
            | {i["event"] for i in COMMENTARY["filings"]}
        )
        self.axis = {label: n + 1 for n, label in enumerate(labels)}
        articles = COMMENTARY["articles"]
        filings = COMMENTARY["filings"]
        self.label = {article_text(i["title"], i["snippet"]): i["event"] for i in articles} | {
            article_text(i["subject"], i["description"]): i["event"] for i in filings
        }

    def embed(self, texts: list[str]) -> np.ndarray:
        vectors = np.zeros((len(texts), len(self.axis) + 1), dtype=np.float32)
        for row, text in enumerate(texts):
            vectors[row, 0] = 2.0  # cos = 4 / (4 + 1) between two events
            vectors[row, self.axis[self.label[text]]] = 1.0
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def replay(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, embedder: object
) -> tuple[dict[int, str], list[tuple[str, int]]]:
    """The fixture through the scanner's grouping as it arrived: each first_seen_at is one
    pass, whose window is what the earlier passes stored. Returns each story's headline and
    each item's (event, story)."""
    watcher = Watcher(settings, db, watchlist, [], SourceResolver([]), embedder, None)  # type: ignore[arg-type]
    passes: dict[datetime, list[tuple[WatchArticle | WatchFiling, str]]] = {}
    for n, item in enumerate(COMMENTARY["articles"]):
        article = WatchArticle(
            url=f"https://news.example.com/{n}",
            source_name=item["source"],
            title=item["title"],
            snippet=item["snippet"],
            published_at=datetime.fromisoformat(item["published_at"]),
            first_seen_at=datetime.fromisoformat(item["first_seen_at"]),
        )
        article.matches = [
            WatchMatch(symbol=symbol, verdict=verdict, reason="named")
            for symbol, verdict in item["matches"].items()
        ]
        passes.setdefault(article.first_seen_at, []).append((article, item["event"]))
    for item in COMMENTARY["filings"]:
        filing = WatchFiling(
            key=item["key"],
            exchange=item["exchange"],
            symbol=item["symbol"],
            company=item["company"],
            subject=item["subject"],
            description=item["description"],
            link=item["link"],
            filed_at=datetime.fromisoformat(item["filed_at"]),
            first_seen_at=datetime.fromisoformat(item["first_seen_at"]),
            kind=item["kind"],
            quoted_headline=item["quoted_headline"],
        )
        passes.setdefault(filing.first_seen_at, []).append((filing, item["event"]))
    placed: list[tuple[str, int]] = []
    for now in sorted(passes):
        with db() as session:
            items = passes[now]
            session.add_all([item for item, _ in items])
            session.flush()
            window = watcher._window_members(session, now)
            articles = [i for i, _ in items if isinstance(i, WatchArticle)]
            filings = [i for i, _ in items if isinstance(i, WatchFiling)]
            watcher._warm(
                [m.text for m in window]
                + [article_text(a.title, a.snippet) for a in articles]
                + [article_text(f.subject, f.description) for f in filings]
            )
            watcher._group(session, window, articles, filings, now)
            session.commit()
            placed += [(event, item.story_id) for item, event in items]  # type: ignore[misc]
    with db() as session:
        headlines = {s.id: s.headline for s in session.query(WatchStory)}
    return headlines, placed


def test_the_commentary_fixture_is_the_real_mix_up() -> None:
    stories: dict[int, set[str]] = {}
    for item in [*COMMENTARY["articles"], *COMMENTARY["filings"]]:
        stories.setdefault(item["stored_story"], set()).add(item["event"])
    assert stories == {
        59: {SHARE_PRICE, "Q1 results"},
        93: {SHARE_PRICE, "torpedo order", "56.8 tender"},
        101: {SHARE_PRICE, "Cogknit acquisition"},
    }


def test_the_commentary_and_event_tests_on_the_real_headlines() -> None:
    seeds = [
        "Zen Technologies Share Price Rises 5.39% on Valuation View",
        "Bharat Dynamics Ltd. Share Price Rise Streak, BDL gains",
        "Astra Microwave Products - 6 Defence stocks turn multibaggers in H1 FY27, rally up",
    ]
    assert all(is_commentary(seed) for seed in seeds)
    for item in COMMENTARY["articles"]:
        if item["event"] != SHARE_PRICE:
            assert is_event(item["title"]), item["title"]
            assert not is_commentary(item["title"]), item["title"]
    assert not is_commentary("Bharat Dynamics Wins ₹1,307 Crore Navy Order; Shares Rise 0.13%")
    assert is_commentary("Buzzing stocks: Watch out for these 16 stocks including BEL")
    # Market chatter is no event: these may sit in a commentary story.
    assert not is_event("Zen Technologies extends losses for a third session")
    assert not is_event("Better Drone Stock: Zen Technologies vs Ideaforge Technology")


@pytest.mark.parametrize("embedder", [NearlyAlike(), SameEmbedder()], ids=["nearly", "same"])
def test_a_story_begun_by_share_price_commentary_takes_no_event(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, embedder: object
) -> None:
    headlines, placed = replay(settings, db, watchlist, embedder)
    for event, story_id in placed:
        if is_commentary(headlines[story_id]):
            assert event == SHARE_PRICE, f"{event} joined {headlines[story_id]!r}"


def test_each_event_in_a_commentary_story_gets_its_own(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile
) -> None:
    """The Cogknit filing, its press release and the 12 reports are one story, as are the
    torpedo order's reports; the tender and the Q1 results are a story each."""
    headlines, placed = replay(settings, db, watchlist, NearlyAlike())
    stories: dict[str, set[int]] = {}
    for event, story_id in placed:
        stories.setdefault(event, set()).add(story_id)
    events = ["Cogknit acquisition", "torpedo order", "56.8 tender", "Q1 results"]
    assert all(len(stories[event]) == 1 for event in events)
    assert len(set().union(*(stories[event] for event in events))) == 4
    # Commentary may still join an event's story, but the commentary story stands alone.
    assert stories[SHARE_PRICE] - set().union(*(stories[e] for e in events))
