"""The scanner's grouping on real items (tests/fixtures/watch_grouping_bdl.json): what went
wrong on 2026-10-15, and the two rules that fix it (app/watch/events.py).

1. Story 137 mixed two events, a Rs 56.8 crore tender and an unrelated Rs 810 crore contract.
   The call summarized both, and the "while you were away" item showed the tender headline
   with the contract's reason, rated high positive. Different amounts are now different
   stories, however alike the embedding model finds the headlines.
2. NSE's order filing (14:56 IST) and the Rs 1,307 crore torpedo order reports (14:56-15:39)
   came out as two items. An NSE order filing says nothing but "Bagging/Receiving of
   orders/contracts", so it now joins the stock's order story reported closest to it, and
   a vague order headline joins one when similarity finds it nothing.
"""

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, WatchlistFile, load_watchlist_file
from app.db import init_db, make_engine, make_session_factory
from app.models import WatchArticle, WatchFiling, WatchMatch
from app.pipeline.embed import article_text
from app.pipeline.fetch import SourceResolver
from app.watch.events import amounts, amounts_conflict, is_order_news
from app.watch.scan import Watcher

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "watch_grouping_bdl.json").read_text(encoding="utf-8")
)


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
