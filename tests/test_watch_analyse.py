"""The watchlist call (step 3): when a story is called, what the call sees, what is stored.

tests/fixtures/watch_calls.json holds the gate's real calls (2026-10-08, Flash-Lite, prompt
watch-v1): 26 stored stories and the model's answers, which must all pass the validators.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import LaneSettings, RateLimitSettings, Settings, WatchlistFile, load_watchlist_file
from app.db import init_db, make_engine, make_session_factory
from app.llm.client import LLMClient
from app.llm.prompts import WATCH_PROMPT_VERSION
from app.llm.ratelimit import MemoryDailyUsageStore, RateLimiter
from app.llm.schemas import WatchAnalysis
from app.models import WatchArticle, WatchCall, WatchFiling, WatchMatch, WatchStory
from app.watch.analyse import (
    analyse,
    company_label,
    matched_assessments,
    pdf_text,
    read_replies,
    stories_to_call,
)
from tests.fakes import FakeProvider, provider_response

NOW = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "watch_calls.json"


@pytest.fixture
def db(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(tmp_path / "analyse.db")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def watchlist() -> WatchlistFile:
    return load_watchlist_file()


@pytest.fixture
def stocks(watchlist: WatchlistFile) -> dict:
    return {stock.symbol: stock for stock in watchlist.stocks}


def answer(kwargs: dict[str, Any], **override: Any) -> Any:
    """A valid call for whatever companies the prompt lists."""
    symbols = re.findall(r'<company symbol="([^"]+)">', kwargs["user"])
    stock = {
        "relevance": "primary",
        "sentiment": "negative",
        "materiality": "high",
        "event_type": "regulatory",
        "reason": "Income tax officials searched the company's offices.",
    } | override
    body = {
        "summary": "Tax officials searched the offices. The shares fell.",
        "stocks": [{"symbol": symbol, **stock} for symbol in symbols],
    }
    return provider_response(json.dumps(body))


def add_story(
    session: Session,
    title: str,
    symbols: list[str],
    verdict: str = "keep",
    articles: int = 1,
    first_seen: datetime = NOW,
) -> WatchStory:
    story = WatchStory(first_seen_at=first_seen, headline=title)
    session.add(story)
    for n in range(articles):
        article = WatchArticle(
            url=f"https://example.com/{title[:12]}-{n}-{id(story)}",
            source_name=f"Outlet {n}",
            title=title if n == 0 else f"{title} ({n})",
            snippet="",
            published_at=first_seen,
            first_seen_at=first_seen + timedelta(minutes=n),
            story=story,
        )
        article.matches = [
            WatchMatch(symbol=s, verdict=verdict, reason="named in the headline", alias=None)
            for s in symbols
        ]
        session.add(article)
    session.flush()
    return story


def client(settings: Settings, provider: FakeProvider, watch_budget: int = 120) -> LLMClient:
    limits = {
        settings.llm.summary_model: RateLimitSettings(
            requests_per_minute=15,
            input_tokens_per_minute=250_000,
            requests_per_day=500,
            requests_per_day_budget=420,
            lanes={
                "main": LaneSettings(requests_per_day=300, requests_per_minute=12),
                "watch": LaneSettings(requests_per_day=watch_budget),
            },
        )
    }
    limiter = RateLimiter(
        limits,
        MemoryDailyUsageStore(),
        settings.tz,
        require_limits=True,
        sleep=lambda seconds: None,
        now=lambda: NOW,
    )
    return LLMClient(settings.llm, provider, limiter, sleep=lambda seconds: None)


# ---------------------------------------------------------------- what the model is told


def test_the_model_is_told_who_each_company_is(stocks: dict) -> None:
    assert company_label(stocks["PFOCUS.NS"]) == (
        "Prime Focus Limited (NSE: PFOCUS); the news also names it as: Brahma AI, DNEG, "
        "Namit Malhotra"
    )
    assert company_label(stocks["BDL.NS"]) == "Bharat Dynamics Limited (NSE: BDL)"


def test_only_kept_stocks_are_called_and_mentions_are_not(
    db: sessionmaker[Session], settings: Settings, stocks: dict
) -> None:
    with db() as session:
        kept = add_story(session, "Russia partners Adani Defence, HAL, BDL", ["HAL.NS", "BDL.NS"])
        add_story(session, "HAL, BEL, BDL shares: top picks", ["HAL.NS"], verdict="mention")
        session.commit()
        pending = stories_to_call(session, stocks, settings, NOW)
    assert [given.story_id for given in pending] == [kept.id]
    assert [symbol for symbol, _ in pending[0].companies] == ["HAL.NS", "BDL.NS"]


# ---------------------------------------------------------------- the pass


def test_a_call_is_stored_per_stock_with_its_provenance_on_the_watch_lane(
    db: sessionmaker[Session], settings: Settings, stocks: dict
) -> None:
    with db() as session:
        story = add_story(session, "Russia partners Adani Defence, HAL, BDL", ["HAL.NS", "BDL.NS"])
        session.commit()
    provider = FakeProvider(responder=answer)
    llm = client(settings, provider)
    result = analyse(db, llm, settings, stocks, NOW)

    assert result.called == [story.id] and result.calls_written == 2
    with db() as session:
        calls = session.scalars(select(WatchCall).order_by(WatchCall.symbol)).all()
    assert [(c.symbol, c.materiality, c.trigger) for c in calls] == [
        ("BDL.NS", "high", "new"),
        ("HAL.NS", "high", "new"),
    ]
    assert calls[0].prompt_version == WATCH_PROMPT_VERSION
    assert calls[0].model == settings.llm.summary_model
    assert calls[0].temperature == settings.llm.temperature_for(settings.llm.summary_model)
    status = llm.limiter.status(settings.llm.summary_model)  # type: ignore[union-attr]
    assert status is not None and status.lanes["watch"] == (1, 120) and status.lanes["main"][0] == 0
    # Nothing changed, nothing called again.
    assert analyse(db, llm, settings, stocks, NOW).called == []


def test_a_story_is_called_again_on_a_filing_or_two_more_articles(
    db: sessionmaker[Session], settings: Settings, stocks: dict
) -> None:
    with db() as session:
        story = add_story(session, "Prime Focus shares tank after tax raids", ["PFOCUS.NS"])
        session.commit()
    llm = client(settings, FakeProvider(responder=answer))
    analyse(db, llm, settings, stocks, NOW)
    with db() as session:
        session.add(
            WatchFiling(
                key="k1",
                exchange="NSE",
                symbol="PFOCUS.NS",
                company="Prime Focus Limited",
                subject="News Verification",
                description="The Exchange has sought clarification...",
                filed_at=NOW,
                first_seen_at=NOW,
                kind="clarification_sought",
                story_id=story.id,
            )
        )
        session.commit()
    assert analyse(db, llm, settings, stocks, NOW).called == [story.id]
    with db() as session:
        triggers = [c.trigger for c in session.scalars(select(WatchCall).order_by(WatchCall.id))]
    assert triggers == ["new", "filing"]


def test_a_spent_watch_lane_stops_the_pass_and_leaves_the_main_lane_alone(
    db: sessionmaker[Session], settings: Settings, stocks: dict
) -> None:
    with db() as session:
        add_story(session, "Prime Focus shares tank after tax raids", ["PFOCUS.NS"])
        add_story(session, "HAL wins Rs 2,000 crore order", ["HAL.NS"])
        session.commit()
    llm = client(settings, FakeProvider(responder=answer), watch_budget=1)
    result = analyse(db, llm, settings, stocks, NOW)
    assert len(result.called) == 1 and result.stopped and "watch lane budget" in result.stopped


def test_invalid_output_is_not_asked_again_as_is(
    db: sessionmaker[Session], settings: Settings, stocks: dict
) -> None:
    with db() as session:
        story = add_story(session, "Prime Focus shares tank after tax raids", ["PFOCUS.NS"])
        session.commit()
    provider = FakeProvider(responder=lambda kwargs: provider_response("not json"))
    llm = client(settings, provider)
    skip: set[tuple[int, int, int]] = set()
    first = analyse(db, llm, settings, stocks, NOW, skip)
    assert first.failed and skip == {(story.id, 1, 0)}
    calls_before = len(provider.calls)
    analyse(db, llm, settings, stocks, NOW, skip)
    assert len(provider.calls) == calls_before  # the same input isn't paid for twice


def test_assessments_for_companies_not_asked_about_are_dropped() -> None:
    analysis = WatchAnalysis.model_validate(
        {
            "summary": "HAL won an order.",
            "stocks": [
                {
                    "symbol": s,
                    "relevance": "primary",
                    "sentiment": "positive",
                    "materiality": "medium",
                    "event_type": "order_win",
                    "reason": "It won an order.",
                }
                for s in ("HAL.NS", "BEL.NS")
            ],
        }
    )
    kept, notes = matched_assessments(analysis, ["HAL.NS", "BDL.NS"])
    assert [k.symbol for k in kept] == ["HAL.NS"]
    assert notes == [
        "dropped an assessment for BEL.NS: not asked about",
        "no assessment returned for BDL.NS",
    ]


def test_a_passing_mention_cannot_be_high() -> None:
    with pytest.raises(ValueError, match="passing mention can't be high"):
        WatchAnalysis.model_validate(
            {
                "summary": "A chart.",
                "stocks": [
                    {
                        "symbol": "HAL.NS",
                        "relevance": "passing",
                        "sentiment": "neutral",
                        "materiality": "high",
                        "event_type": "other",
                        "reason": "Only a chart.",
                    }
                ],
            }
        )


# ---------------------------------------------------------------- the company's reply


def minimal_pdf(text: str) -> bytes:
    """A one-page PDF with `text` on it, built by hand (no PDF writer needed)."""
    stream = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def test_a_reply_pdf_is_read_and_triggers_a_new_call(
    db: sessionmaker[Session], settings: Settings, stocks: dict
) -> None:
    assert pdf_text(minimal_pdf("We deny the report.")) == "We deny the report."
    with db() as session:
        story = add_story(session, "Prime Focus shares tank after tax raids", ["PFOCUS.NS"])
        session.add(
            WatchFiling(
                key="r1",
                exchange="NSE",
                symbol="PFOCUS.NS",
                company="Prime Focus Limited",
                subject="News Verification",
                description="The response from the Company is attached.",
                link="https://nsearchives.nseindia.com/corporate/reply.pdf",
                filed_at=NOW,
                first_seen_at=NOW,
                kind="company_reply",
                story_id=story.id,
            )
        )
        session.commit()
    llm = client(settings, FakeProvider(responder=answer))
    analyse(db, llm, settings, stocks, NOW)  # the first call: the reply isn't read yet
    notes = read_replies(
        db, settings, fetch=lambda url, ua: minimal_pdf("Operations are unaffected.")
    )
    assert notes == []
    provider = FakeProvider(responder=answer)
    llm = client(settings, provider)
    assert analyse(db, llm, settings, stocks, NOW).called == [story.id]
    assert (
        "<company_reply>\nOperations are unaffected.\n</company_reply>" in provider.calls[0]["user"]
    )
    with db() as session:
        latest = session.scalars(select(WatchCall).order_by(WatchCall.id.desc())).first()
        assert latest is not None and latest.trigger == "reply" and latest.read_reply


# ---------------------------------------------------------------- the gate's real answers


def test_every_real_answer_from_the_gate_passes_the_validators() -> None:
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    assert len(fixtures) >= 20
    for item in fixtures:
        analysis = WatchAnalysis.model_validate(item["output"])
        kept, notes = matched_assessments(analysis, [symbol for symbol, _ in item["companies"]])
        assert kept and not notes, item["story_id"]
    junk = {f["story_id"]: f for f in fixtures}
    # The Halliburton options page and the Air Force Day history piece are not HAL news.
    for story_id in (34, 41):
        assert junk[story_id]["output"]["stocks"][0]["relevance"] == "passing"
    # High materiality went only to the Prime Focus tax-raid stories.
    high = {
        f["story_id"]
        for f in fixtures
        for stock in f["output"]["stocks"]
        if stock["materiality"] == "high"
    }
    assert high <= {1, 3, 4, 5}
