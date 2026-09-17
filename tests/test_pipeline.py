"""End-to-end: `run` and `digest` against mocked feeds, a fake LLM, and a mocked Telegram."""

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.cli import run_digest, run_pipeline
from app.config import FeedConfig, Settings
from app.db import init_db, make_engine, make_session_factory
from app.delivery.telegram import TelegramError
from app.llm.client import LLMClient
from app.models import Run, Story
from tests.conftest import NOW, make_feed
from tests.fakes import FakeProvider, echo_summary_responder, rss

US_FEED = make_feed(name="Paper US", url="https://us.example.com/rss", region="US", weight=2)
IN_FEED = make_feed(name="Paper IN", url="https://in.example.com/rss", region="IN", weight=3)
DOWN_FEED = make_feed(name="Down", url="https://down.example.com/rss", region="GLOBAL", weight=1)
FEEDS: list[FeedConfig] = [US_FEED, IN_FEED, DOWN_FEED]

EU_US = "EU chief backs plan for Canada to become 'associate member'"
EU_IN = "EU's von der Leyen wants Canada to become bloc's first associate member"
GAZA = "At least 20 killed as multi-storey building collapses in Gaza City"
CHESS = "Chess olympiad opens in Budapest with record entries"


class FeedServer:
    """Serves RSS per host; `items` can be changed between runs."""

    def __init__(self) -> None:
        self.items: dict[str, list[tuple[str, str, object, str]]] = {
            "us.example.com": [
                (EU_US, "https://us.example.com/eu", NOW - timedelta(hours=3), "Brussels said."),
                (GAZA, "https://us.example.com/gaza", NOW - timedelta(hours=2), "Rescuers dug."),
            ],
            "in.example.com": [
                (EU_IN, "https://in.example.com/eu", NOW - timedelta(hours=2), "Ottawa reacts."),
                (
                    CHESS,
                    "https://in.example.com/chess",
                    NOW - timedelta(hours=1),
                    "Players arrive.",
                ),
            ],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host not in self.items:
            return httpx.Response(500)
        return httpx.Response(200, content=rss(self.items[request.url.host]))  # type: ignore[arg-type]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def _no_backoff(settings: Settings) -> None:
    settings.http.backoff_base_seconds = 0.0  # the failing mock feed would otherwise sleep


@pytest.fixture
def db(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    return make_session_factory(engine)


def _llm(settings: Settings) -> tuple[LLMClient, FakeProvider]:
    fake = FakeProvider(responder=echo_summary_responder)
    return LLMClient(settings.llm, fake, sleep=lambda seconds: None), fake


def test_run_twice_does_not_resummarize(db: sessionmaker[Session], settings: Settings) -> None:
    settings.grouping.threshold = 64
    server = FeedServer()

    llm, fake = _llm(settings)
    first = run_pipeline(db, settings, FEEDS, llm, now=NOW, transport=server.transport)
    assert (first.feeds_ok, first.feeds_failed) == (2, 1)
    assert first.articles_new == 4
    assert (first.stories_created, first.articles_attached) == (3, 1)  # the EU pair grouped
    assert first.summarized == 3 and len(fake.calls) == 3
    assert first.input_tokens == 3 * 120 and first.output_tokens == 3 * 60
    assert any(e["stage"] == "fetch" and e["feed"] == "Down" for e in first.errors)

    llm, fake = _llm(settings)
    second = run_pipeline(db, settings, FEEDS, llm, now=NOW, transport=server.transport)
    assert second.articles_new == 0 and second.duplicates_dropped == 4
    assert second.summarized == 0 and second.skipped_unchanged == 3
    assert fake.calls == []

    # Two more outlets' articles on the EU story: only that story is summarized again.
    later = NOW + timedelta(hours=1)
    server.items["us.example.com"].append(
        (
            "Canada invited to become EU's first 'associate member' as trade war intensifies",
            "https://us.example.com/eu-2",
            NOW + timedelta(minutes=30),
            "More detail.",
        )
    )
    server.items["in.example.com"].append(
        (
            "EU proposes associate member status for Canada",
            "https://in.example.com/eu-2",
            NOW + timedelta(minutes=40),
            "Analysis.",
        )
    )
    llm, fake = _llm(settings)
    third = run_pipeline(db, settings, FEEDS, llm, now=later, transport=server.transport)
    assert third.articles_new == 2 and third.articles_attached == 2
    assert third.summarized == 1 and third.skipped_unchanged == 2

    with db() as session:
        runs = session.scalars(select(Run).order_by(Run.id)).all()
        assert [r.kind for r in runs] == ["pipeline"] * 3
        assert [r.stories_processed for r in runs] == [3, 0, 1]
        assert runs[0].input_tokens == 360 and runs[0].finished_at is not None
        assert runs[1].input_tokens == 0
        eu = session.scalars(select(Story).where(Story.processed_article_count == 4)).one()
        assert eu.status == "summarized" and eu.updated_at == later


def test_missing_api_key_skips_summarization(db: sessionmaker[Session], settings: Settings) -> None:
    report = run_pipeline(
        db,
        settings,
        FEEDS,
        None,
        now=NOW,
        transport=FeedServer().transport,
        llm_unavailable="GEMINI_API_KEY is not set (llm.provider is gemini)",
    )
    assert report.articles_new == 4 and report.summarized == 0
    assert any("skipped: GEMINI_API_KEY is not set" in e["error"] for e in report.errors)
    with db() as session:
        assert session.scalars(select(Run)).one().errors == report.errors


def test_digest_dry_run_then_send(db: sessionmaker[Session], settings: Settings) -> None:
    llm, _ = _llm(settings)
    run_pipeline(db, settings, FEEDS, llm, now=NOW, transport=FeedServer().transport)

    dry = run_digest(db, settings, send=False, now=NOW + timedelta(minutes=5))
    assert dry.stories == 3 and not dry.sent
    text = "\n".join(dry.messages)
    assert "Gaza" in text and "Research notes, not financial advice." in text
    with db() as session:
        assert session.scalars(select(Run).where(Run.kind == "digest")).all() == []

    sent_texts: list[str] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        sent_texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {}})

    sent = run_digest(
        db,
        settings,
        send=True,
        token="1:abc",
        chat_id="42",
        now=NOW + timedelta(minutes=10),
        transport=httpx.MockTransport(telegram),
    )
    assert sent.sent and sent_texts == sent.messages

    after = run_digest(db, settings, send=False, now=NOW + timedelta(minutes=15))
    assert after.stories == 0  # nothing new since the digest that was sent


def test_failed_send_does_not_move_digest_window(
    db: sessionmaker[Session], settings: Settings
) -> None:
    llm, _ = _llm(settings)
    run_pipeline(db, settings, FEEDS, llm, now=NOW, transport=FeedServer().transport)

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked"})

    with pytest.raises(TelegramError, match="blocked"):
        run_digest(
            db,
            settings,
            send=True,
            token="1:abc",
            chat_id="42",
            now=NOW + timedelta(minutes=10),
            transport=httpx.MockTransport(broken),
        )
    retry = run_digest(db, settings, send=False, now=NOW + timedelta(minutes=20))
    assert retry.stories == 3
    with db() as session:
        failed = session.scalars(select(Run).where(Run.kind == "digest")).one()
        assert failed.errors and failed.stories_processed == 0
