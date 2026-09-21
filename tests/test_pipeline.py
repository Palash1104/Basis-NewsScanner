"""End-to-end: `run` and `digest` against mocked feeds, a fake LLM, and a mocked Telegram."""

import json
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.cli import run_digest, run_pipeline
from app.config import FeedConfig, RateLimitSettings, Settings
from app.db import init_db, make_engine, make_session_factory
from app.delivery.telegram import TelegramError
from app.llm.client import LLMClient
from app.llm.ratelimit import MemoryDailyUsageStore, RateLimiter
from app.models import Article, Event, Impact, RuleDisagreementRow, Run, Story
from app.pipeline.prices import Bar
from tests.conftest import NOW, make_feed
from tests.fakes import (
    FakeEmbedder,
    FakePrices,
    FakeProvider,
    echo_summary_responder,
    impacts_json,
    provider_response,
    rss,
)

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
    # One rerank, then a summary, an extraction and an impact call per story: 1 + 3 + 3 + 3.
    assert first.summarized == 3 and first.events_extracted == 3 and len(fake.calls) == 10
    assert first.reranked == 3 and first.llm_impact_calls == 3
    # The fake event (US/India tariffs, escalating) matches one rule, twice per story.
    assert first.stories_analyzed == 3 and first.impacts_created == 6
    assert first.impact_rules == {"us_tariffs_on_india": 6}
    assert first.input_tokens == 10 * 120 and first.output_tokens == 10 * 60
    assert any(e["stage"] == "fetch" and e["feed"] == "Down" for e in first.errors)

    llm, fake = _llm(settings)
    second = run_pipeline(db, settings, FEEDS, llm, now=NOW, transport=server.transport)
    assert second.articles_new == 0 and second.duplicates_dropped == 4
    assert second.summarized == 0 and second.skipped_unchanged == 3
    assert second.events_extracted == 0 and len(fake.calls) == 1  # the rerank only

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
    assert third.events_extracted == 1  # re-extracted along with its re-summary
    assert len(fake.calls) == 4  # rerank, summary, extraction, impacts

    with db() as session:
        runs = session.scalars(select(Run).order_by(Run.id)).all()
        assert [r.kind for r in runs] == ["pipeline"] * 3
        assert [r.stories_processed for r in runs] == [3, 0, 1]
        assert runs[0].input_tokens == 1200 and runs[0].finished_at is not None
        assert runs[1].input_tokens == 120  # the rerank, which runs even with no summaries
        eu = session.scalars(select(Story).where(Story.processed_article_count == 4)).one()
        assert eu.status == "analyzed" and eu.updated_at == later
        # Re-extraction doesn't duplicate impacts the story already has.
        assert [(i.symbol, i.rule_id) for i in eu.impacts] == [
            ("^NSEI", "us_tariffs_on_india"),
            ("INR=X", "us_tariffs_on_india"),
        ]


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
    # Playbook impacts appear with their mechanism (SPEC §13, Phase 2).
    assert "▼ Nifty 50 · 2nd · low · playbook — US tariffs threaten Indian exports" in text
    assert "▲ USD/INR (rupee weaker) · 2nd · low · playbook" in text
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


def _budget_limited_llm(settings: Settings, budget: int) -> tuple[LLMClient, FakeProvider]:
    limits = {
        settings.llm.summary_model: RateLimitSettings(
            requests_per_minute=100,
            input_tokens_per_minute=1_000_000,
            requests_per_day=100,
            requests_per_day_budget=budget,
        )
    }
    limiter = RateLimiter(
        limits, MemoryDailyUsageStore(), ZoneInfo("America/Los_Angeles"), require_limits=True
    )
    fake = FakeProvider(responder=echo_summary_responder)
    return LLMClient(settings.llm, fake, limiter=limiter, sleep=lambda seconds: None), fake


def test_quota_runs_out_mid_run_and_next_run_catches_up(
    db: sessionmaker[Session], settings: Settings
) -> None:
    settings.pipeline.rerank_candidates = 0  # this test is about the summary budget
    server = FeedServer()
    llm, fake = _budget_limited_llm(settings, budget=1)
    first = run_pipeline(db, settings, FEEDS, llm, now=NOW, transport=server.transport)

    # Fetching and grouping finished; one summary fit the budget, two were left for later.
    assert first.articles_new == 4 and first.stories_created == 3
    assert first.summarized == 1 and first.skipped_quota == 2 and len(fake.calls) == 1
    # Its event extraction didn't fit either: left for the next run too.
    assert first.events_extracted == 0 and first.events_skipped_quota == 1
    stop = next(e for e in first.errors if "stopped early" in e["error"])
    assert "daily budget reached" in stop["error"] and len(stop["left_for_next_run"]) == 2
    with db() as session:
        run = session.scalars(select(Run)).one()
        assert run.finished_at is not None and run.stories_processed == 1
        pending = session.scalars(select(Story).where(Story.summary_pending.is_(True))).all()
        assert len(pending) == 2 and all(story.status == "new" for story in pending)

    # Next run (quota available again, and only 1 top story): the 2 pending stories go first.
    settings.pipeline.max_stories_per_run = 1
    llm, fake = _budget_limited_llm(settings, budget=10)
    second = run_pipeline(db, settings, FEEDS, llm, now=NOW, transport=server.transport)
    assert second.pending_carried == 2
    assert second.summarized == 2 and second.skipped_quota == 0
    # The carried-over extraction, the two new summaries and their extractions, then an
    # impact call for each of the three analyzed stories: 2 + 3 + 3.
    assert second.events_pending_carried == 1 and second.events_extracted == 3
    assert second.llm_impact_calls == 3 and len(fake.calls) == 8
    with db() as session:
        assert session.scalars(select(Story).where(Story.summary_pending.is_(True))).all() == []
        assert session.scalars(select(Story).where(Story.event_pending.is_(True))).all() == []
        assert len(session.scalars(select(Event)).all()) == 3
        assert len(session.scalars(select(Story).where(Story.status == "analyzed")).all()) == 3


def _summarized_story(session: Session, headline: str, regions: list[str], status: str) -> Story:
    story = Story(
        first_seen_at=NOW,
        updated_at=NOW,
        headline=headline,
        summary="One. Two.",
        category="Other",
        regions=regions,
        status=status,
    )
    story.articles = [
        Article(
            url=f"https://example.com/{headline.replace(' ', '-')}",
            source_name="BBC",
            source_region="GLOBAL",
            source_weight=3,
            title=headline,
            snippet="",
            published_at=NOW,
            fetched_at=NOW,
        )
    ]
    session.add(story)
    return story


def test_digest_keeps_empty_region_stories_and_drops_stale_ones(
    db: sessionmaker[Session], settings: Settings
) -> None:
    with db() as session:
        _summarized_story(session, "Fiji declares national HIV crisis", [], "summarized")
        _summarized_story(session, "Regrouped story with an old summary", ["US"], "needs_resummary")
        session.commit()

    report = run_digest(db, settings, send=False, now=NOW + timedelta(minutes=1))
    text = "\n".join(report.messages)
    assert report.stories == 1
    assert (
        "<b>Fiji declares national HIV crisis</b>\n<i>Other</i>" in text
    )  # no region, still shown
    assert "Regrouped story" not in text


def test_run_logs_borderline_matches_and_non_news(
    db: sessionmaker[Session], settings: Settings, tmp_path: Path
) -> None:
    settings.paths.log_dir = str(tmp_path / "logs")
    settings.grouping.embedding_threshold = 0.4
    settings.grouping.borderline_log_range = (0.0, 1.0)
    server = FeedServer()
    server.items["in.example.com"].append(
        (
            "What is an EU associate member? Explained",
            "https://in.example.com/x",
            NOW - timedelta(minutes=30),
            "Background.",
        )
    )
    llm, _ = _llm(settings)
    report = run_pipeline(
        db, settings, FEEDS, llm, now=NOW, transport=server.transport, embedder=FakeEmbedder()
    )

    assert report.grouping_method == "embedding"
    assert [e["title"] for e in report.non_news] == ["What is an EU associate member? Explained"]
    assert report.non_news[0]["reason"] == "explainer (explained)"
    lines = (tmp_path / "logs" / "grouping_borderline.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == report.borderline_logged > 0
    row = json.loads(lines[0])
    assert {"score", "decision", "article", "nearest_story", "threshold"} <= set(row)


def test_prices_reach_the_digest_as_moves_and_labels(
    db: sessionmaker[Session], settings: Settings
) -> None:
    """End to end: the playbook's impacts get a reference price, and the digest shows the move
    so far with an "already moved" label where the move is big enough."""
    hourly = [NOW - timedelta(hours=n) for n in range(48, -1, -1)]
    daily = [NOW - timedelta(days=n) for n in range(30, -1, -1)]

    def series(intraday_closes: list[float], daily_closes: list[float]) -> dict:
        return {
            "60m": [
                Bar(ts, c, c, c, c, 1000.0) for ts, c in zip(hourly, intraday_closes, strict=True)
            ],
            "1d": [Bar(ts, c, c, c, c, 1000.0) for ts, c in zip(daily, daily_closes, strict=True)],
        }

    # The Nifty drifts down 0.1% an hour (a big move by its own standards); USD/INR barely moves.
    nifty_intraday = [24000 * (0.999**n) for n in range(len(hourly))]
    steady = [24000 + (50 if n % 2 else 0) for n in range(len(daily))]
    inr_intraday = [88.0] * len(hourly)
    prices = FakePrices(
        {
            "^NSEI": series(nifty_intraday, steady),
            "INR=X": series(
                inr_intraday, [88.0 + (0.2 if n % 2 else 0) for n in range(len(daily))]
            ),
        }
    )
    llm, _ = _llm(settings)
    report = run_pipeline(
        db, settings, FEEDS, llm, now=NOW, transport=FeedServer().transport, prices=prices
    )

    assert report.impacts_created == 6 and report.impacts_priced == 6
    assert report.price_symbols == 2 and report.price_unusable == {}
    assert ("^NSEI", "60m") in prices.calls and ("^NSEI", "1d") in prices.calls

    dry = run_digest(db, settings, send=False, now=NOW + timedelta(minutes=5))
    text = "\n".join(dry.messages)
    assert "▼ Nifty 50 -0.3% (already moved) · since news · 2nd · low · playbook" in text
    assert "▲ USD/INR (rupee weaker) +0.0%" in text  # priced, but nothing worth flagging


def _impacts_responder(llm_json: str):
    """Like echo_summary_responder, but the impact layer answers with `llm_json`."""

    def responder(kwargs: dict) -> object:
        if kwargs["schema"].__name__ == "LLMImpacts":
            return provider_response(llm_json)
        return echo_summary_responder(kwargs)

    return responder


def test_the_llm_layer_adds_calls_merges_them_and_records_disagreements(
    db: sessionmaker[Session], settings: Settings
) -> None:
    """Layer B agrees with one playbook call, adds one of its own, and disputes a rule."""
    settings.impacts.llm_max_stories_per_run = 2
    llm_json = impacts_json(
        no_clear_impact=False,
        impacts=[
            {
                "symbol": "^NSEI",  # the playbook says this too
                "direction": "down",
                "mechanism": "Tariffs weigh on Indian equities",
                "order": "first",
                "confidence": "high",
                "horizon": "days",
            },
            {
                "symbol": "^CNXIT",  # the playbook has no rule for this
                "direction": "down",
                "mechanism": "IT exporters lose US demand",
                "order": "first",
                "confidence": "medium",
                "horizon": "weeks",
            },
            {
                "symbol": "MADEUP.NS",
                "direction": "up",
                "mechanism": "Invented",
                "order": "first",
                "confidence": "high",
                "horizon": "days",
            },
        ],
        rule_disagreements=[
            {"rule_id": "us_tariffs_on_india", "reason": "the tariffs are not in force yet"}
        ],
    )
    fake = FakeProvider(responder=_impacts_responder(llm_json))
    llm = LLMClient(settings.llm, fake, sleep=lambda seconds: None)

    report = run_pipeline(db, settings, FEEDS, llm, now=NOW, transport=FeedServer().transport)

    assert report.llm_impact_calls == 2  # capped at 2 stories a run
    assert report.llm_impacts_added == 2  # one ^CNXIT call per analyzed story
    assert report.llm_disagreements == 2
    assert report.llm_impact_declines == 0  # this fake answers with impacts every time
    assert any("MADEUP.NS" in note and "invalid symbol" in note for note in report.llm_notes)

    with db() as session:
        impacts = session.scalars(select(Impact).order_by(Impact.id)).all()
        by_origin: dict[str, set[str]] = {}
        for impact in impacts:
            by_origin.setdefault(impact.origin, set()).add(impact.symbol)
        assert by_origin["both"] == {"^NSEI"}  # agreed, so one row from both layers
        assert by_origin["llm"] == {"^CNXIT"}  # the model's own call
        # INR=X is the rule the model didn't mention; ^NSEI appears here too because the
        # third story fell outside the two-story cap and never got an LLM call.
        assert by_origin["playbook"] == {"INR=X", "^NSEI"}
        nifty = next(i for i in impacts if i.symbol == "^NSEI" and i.origin == "both")
        assert nifty.rule_id == "us_tariffs_on_india" and nifty.horizon == "days"
        assert nifty.confidence == "low"  # the rule is disputed, so its call is demoted
        assert not any(i.symbol == "MADEUP.NS" for i in impacts)
        # Layer B's provenance is on the calls it made and on nothing else.
        temperature = settings.llm.temperature_for(settings.llm.summary_model)
        assert (nifty.model, nifty.prompt_version) == (settings.llm.summary_model, "impact-v1")
        assert (nifty.temperature, nifty.seed) == (temperature, settings.llm.seed)
        playbook_only = next(i for i in impacts if i.origin == "playbook")
        assert (playbook_only.model, playbook_only.seed) == (None, None)
        disagreements = session.scalars(select(RuleDisagreementRow)).all()
        assert {d.rule_id for d in disagreements} == {"us_tariffs_on_india"}
        assert "not in force yet" in disagreements[0].reason
        # `newsdesk health` reads the decline rate from the run, since a declined call
        # writes no impacts to count.
        run = session.get(Run, report.run_id)
        assert run is not None
        assert (run.llm_impact_calls, run.llm_impact_declines) == (2, 0)
        assert disagreements[0].model == settings.llm.summary_model
        assert (disagreements[0].temperature, disagreements[0].seed) == (
            temperature,
            settings.llm.seed,
        )
