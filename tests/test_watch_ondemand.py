"""Scanning one stock when its card is opened on /watchlist (user, 2026-10-18).

The web server only leaves a request file in the scanner's inbox; the scanner runs the scan
between its jobs and records it in `watch_scans`, which the dialog reads. No network: Google
News, NSE's API and Yahoo are stand-ins, and the model is tests/fakes.py's.
"""

import html
import re
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, WatchlistFile, load_watchlist_file
from app.db import (
    init_db,
    make_engine,
    make_read_only_engine,
    make_read_only_session_factory,
    make_session_factory,
)
from app.models import (
    WatchAlert,
    WatchArticle,
    WatchMatch,
    WatchRun,
    WatchScan,
    WatchStory,
    utcnow,
)
from app.watch import ondemand
from app.watch.preview import Profile
from app.watch.scan import Watcher, run_forever, start_heartbeat
from app.web.main import create_app
from tests.conftest import FIXTURES
from tests.fakes import FakeProvider
from tests.test_watch_analyse import answer, client
from tests.test_watch_catchup import CatchUpWeb
from tests.test_watch_scan import T0, Clock, FakeSnapshots, google_news, make_watcher

ORIGIN = "http://testserver"
HAL_NEWS = (
    "HAL wins Rs 5,000 crore order for aero engines from the Air Force",
    "Mint",
    "https://news.google.com/rss/articles/hal-order",
)
PF_NEWS = (
    "Prime Focus shares fall after tax searches at Mumbai offices",
    "Mint",
    "https://news.google.com/rss/articles/pf-search",
)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "newsdesk.db"
    engine = make_engine(path)
    init_db(engine)
    engine.dispose()
    return path


@pytest.fixture
def db(database: Path) -> sessionmaker[Session]:
    return make_session_factory(make_engine(database))


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    return tmp_path / "scan_requests"


@pytest.fixture
def watchlist() -> WatchlistFile:
    return load_watchlist_file()  # tests/fixtures/watchlist.yaml (conftest)


def profile(symbol: str) -> Profile:
    return Profile(
        closes=[(T0 - timedelta(days=days), 100.0 + days) for days in range(400, 0, -1)],
        currency="INR",
        sector="Industrials",
        industry="Aerospace & Defense",
        market_cap=3.1e12,
    )


# ---------------------------------------------------------------- the inbox


def test_a_request_file_stays_inside_the_inbox(inbox: Path) -> None:
    assert ondemand.request_path(inbox, "M&M.NS") == inbox / "M_M.NS.json"
    assert ondemand.request_path(inbox, "../../etc/passwd").parent == inbox
    with pytest.raises(ValueError):
        ondemand.request_path(inbox, "_scanner")  # the heartbeat's name is not a stock's


def test_requests_are_written_whole_and_read_oldest_first(inbox: Path) -> None:
    later = ondemand.write_request(inbox, "HAL.NS", T0)
    first = ondemand.write_request(inbox, "BDL.NS", T0 - timedelta(seconds=5))
    ondemand.write_heartbeat(inbox, "idle", T0, T0)
    (inbox / "junk.json").write_text("{not json", encoding="utf-8")
    assert [r.id for r in ondemand.pending_requests(inbox)] == [first.id, later.id]
    assert not (inbox / "junk.json").exists()  # nothing could ever take it
    assert not list(inbox.glob(".*.tmp"))  # no temporary file left behind
    ondemand.remove_request(inbox, later)
    assert [r.symbol for r in ondemand.pending_requests(inbox)] == ["BDL.NS"]


# ---------------------------------------------------------------- the scanner


def scanner(
    settings: Settings,
    db: sessionmaker[Session],
    watchlist: WatchlistFile,
    inbox: Path,
    *news: tuple[str, str, str],
    provider: FakeProvider | None = None,
    budget: int = 120,
) -> tuple[Watcher, CatchUpWeb, list[str]]:
    web = CatchUpWeb()
    web.google = google_news(*news)
    watcher = make_watcher(settings, db, watchlist, web, Clock(T0), FakeSnapshots())
    watcher.scan_inbox = inbox
    watcher.profile_fetch = profile
    watcher.llm = client(settings, provider or FakeProvider(responder=answer), budget)
    sent: list[str] = []
    watcher.sender = sent.extend
    return watcher, web, sent


def test_the_scanner_runs_a_requested_scan_and_records_each_step(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, inbox: Path
) -> None:
    provider = FakeProvider(responder=answer)
    watcher, web, sent = scanner(settings, db, watchlist, inbox, PF_NEWS, provider=provider)
    request = ondemand.write_request(inbox, "PFOCUS.NS", T0 - timedelta(seconds=3))

    assert watcher.serve_scan_requests() == 1
    assert ondemand.pending_requests(inbox) == []  # taken
    assert watcher.serve_scan_requests() == 0  # and not run twice

    searched = [r.url for r in web.requests if r.url.host == "news.google.com"]
    assert len(searched) == 1 and "Prime Focus" in str(searched[0].params["q"])
    asked = [r.url.params["symbol"] for r in web.requests if r.url.host == "www.nseindia.com"]
    assert asked == ["PFOCUS"]  # NSE's API for this one company, nobody else
    assert "PFOCUS.NS" in watcher.snapshots.calls  # type: ignore[union-attr]

    with db() as session:
        scan = session.scalars(select(WatchScan)).one()
        assert scan.request_id == request.id and scan.status == "done"
        states = {step["key"]: step["state"] for step in scan.steps}
        assert states == {
            "google_news": "done",
            "nse": "done",
            "bse": "skipped",  # the tests' settings have no PEAD file
            "price": "done",
            "analyse": "done",
        }
        assert scan.new_articles == 1 and scan.new_filings == 2  # NSE's two notices
        assert scan.new_stories >= 1 and scan.calls >= 1
        assert scan.profile and scan.profile["sector"] == "Industrials"
        run = session.scalars(select(WatchRun).where(WatchRun.job == "on_demand")).one()
        assert run.details == {"symbol": "PFOCUS.NS", "request_id": request.id, "scan": scan.id}
        # Old filings found on demand never start a "while you were away" summary.
        assert session.scalars(select(WatchRun).where(WatchRun.job == "late")).all() == []
        # And the scan itself sends nothing.
        assert session.scalars(select(WatchAlert)).all() == [] and sent == []
    assert provider.calls and all(
        re.findall(r'<company symbol="([^"]+)">', call["user"]) == ["PFOCUS.NS"]
        for call in provider.calls
    )


def test_a_spent_watch_lane_stores_the_headlines_and_calls_nothing(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, inbox: Path
) -> None:
    provider = FakeProvider(responder=answer)
    watcher, _, _ = scanner(settings, db, watchlist, inbox, HAL_NEWS, provider=provider, budget=1)
    assert watcher.llm is not None and watcher.llm.limiter is not None
    # Today's one watch-lane request is gone already.
    watcher.llm.limiter.acquire(settings.llm.summary_model, 100, retry=False, lane="watch")
    ondemand.write_request(inbox, "HAL.NS", T0)
    watcher.serve_scan_requests()
    with db() as session:
        scan = session.scalars(select(WatchScan)).one()
    assert scan.status == "done" and scan.calls == 0 and scan.new_articles == 1
    assert "budget is spent" in (scan.note or "")
    assert {s["key"]: s["state"] for s in scan.steps}["analyse"] == "skipped"
    assert provider.calls == []
    assert watcher._calls_paused_until is not None  # the next scan doesn't even try

    ondemand.write_request(inbox, "HAL.NS", T0 + timedelta(minutes=5))
    watcher.clock = Clock(T0 + timedelta(minutes=5))
    watcher.serve_scan_requests()
    with db() as session:
        second = session.scalars(select(WatchScan).order_by(WatchScan.id.desc())).first()
    assert second is not None and "budget is spent" in (second.note or "")
    assert provider.calls == []


def test_a_story_found_on_demand_alerts_once_by_the_usual_rules(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, inbox: Path
) -> None:
    """HAL's order, first reported an hour ago and rated high: the alerts job sends it, as it
    would have anyway - once, however often the card is opened."""
    watcher, _, sent = scanner(settings, db, watchlist, inbox, HAL_NEWS)
    ondemand.write_request(inbox, "HAL.NS", T0)
    watcher.serve_scan_requests()
    assert sent == []  # the scan sends nothing itself
    watcher.run_job("alerts", T0 + timedelta(minutes=1))
    watcher.clock = Clock(T0 + timedelta(minutes=4))
    ondemand.write_request(inbox, "HAL.NS", T0 + timedelta(minutes=4))  # opened again
    watcher.serve_scan_requests()
    watcher.run_job("alerts", T0 + timedelta(minutes=5))
    with db() as session:
        news = session.scalars(select(WatchAlert).where(WatchAlert.kind == "news")).all()
    assert len(news) == 1
    # The headline, as the Telegram item writes it (without the company's name up front).
    assert len([m for m in sent if "Rs 5,000 crore order for aero engines" in m]) == 1


def test_old_unknown_and_already_taken_requests_are_dropped(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, inbox: Path
) -> None:
    watcher, _, _ = scanner(settings, db, watchlist, inbox, HAL_NEWS)
    ondemand.write_request(inbox, "HAL.NS", T0 - timedelta(minutes=11))  # nobody waits now
    ondemand.write_request(inbox, "GC=F", T0)  # a commodity: nothing to scan
    taken = ondemand.write_request(inbox, "BDL.NS", T0)
    with db() as session:  # its row exists: only the file was left behind
        session.add(
            WatchScan(
                request_id=taken.id,
                symbol="BDL.NS",
                requested_at=T0,
                started_at=T0,
                updated_at=T0,
                status="done",
                finished_at=T0,
                steps=[],
                errors=[],
            )
        )
        session.commit()
    assert watcher.serve_scan_requests() == 0
    assert ondemand.pending_requests(inbox) == []
    with db() as session:
        assert len(session.scalars(select(WatchScan)).all()) == 1


def test_requests_are_taken_between_jobs_and_while_idle(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, inbox: Path
) -> None:
    watcher, _, _ = scanner(settings, db, watchlist, inbox, HAL_NEWS)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:  # the click comes while the scanner waits for its next tick
            ondemand.write_request(inbox, "HAL.NS", T0)

    run_forever(watcher, sleep, should_stop=lambda: len(sleeps) >= 3)
    assert sleeps[:3] == [2, 2, 2]  # it looks every couple of seconds, not every tick
    with db() as session:
        assert session.scalars(select(WatchScan.symbol)).all() == ["HAL.NS"]


def test_the_heartbeat_says_alive_and_goes_on_a_clean_stop(
    settings: Settings, db: sessionmaker[Session], watchlist: WatchlistFile, inbox: Path
) -> None:
    watcher, _, _ = scanner(settings, db, watchlist, inbox)
    watcher.activity = ("analyse", T0)
    stop = start_heartbeat(watcher, every=0.05)
    try:
        beat = None
        for _ in range(100):  # the thread's first beat, moments after it starts
            if (beat := ondemand.read_heartbeat(inbox)) is not None:
                break
            time.sleep(0.02)
        assert beat is not None and beat.activity == "analyse" and beat.alive(utcnow())
    finally:
        stop()
    assert ondemand.read_heartbeat(inbox) is None


# ---------------------------------------------------------------- the page


@pytest.fixture
def page(tmp_path: Path, settings: Settings, database: Path, inbox: Path) -> TestClient:
    watchlist = tmp_path / "watchlist.yaml"
    shutil.copy(FIXTURES / "watchlist.yaml", watchlist)
    app = create_app(
        settings,
        make_read_only_session_factory(make_read_only_engine(database)),
        watchlist_path=watchlist,
        companies_path=tmp_path / "companies.json",
        backup_dir=tmp_path / "backups",
        allowed_hosts=frozenset({"testserver"}),
        scan_inbox=inbox,
    )
    return TestClient(app)


def token(client: TestClient) -> str:
    body = client.get("/watchlist").text
    return body.split('name="token" value="')[1].split('"')[0]


def post(client: TestClient, symbol: str = "HAL.NS", **extra: str) -> httpx.Response:
    form = {"token": token(client), "symbol": symbol} | extra
    return client.post("/watchlist/scan", data=form, headers={"Origin": ORIGIN})


def alive(inbox: Path, activity: str = "idle", since: datetime | None = None) -> None:
    now = utcnow()
    ondemand.write_heartbeat(inbox, activity, since or now, now)


def add_scan(db: sessionmaker[Session], **fields: Any) -> None:
    now = utcnow()
    values: dict[str, Any] = {
        "request_id": f"r{now.timestamp()}{len(fields)}",
        "symbol": "HAL.NS",
        "requested_at": now - timedelta(seconds=50),
        "started_at": now - timedelta(seconds=48),
        "updated_at": now - timedelta(seconds=40),
        "finished_at": now - timedelta(seconds=40),
        "status": "done",
        "steps": ondemand.new_steps(),
        "errors": [],
        "new_articles": 1,
        "new_filings": 2,
        "new_stories": 1,
        "calls": 1,
    } | fields
    with db() as session:
        session.add(WatchScan(**values))
        session.commit()


@pytest.mark.parametrize(
    ("headers", "with_token"),
    [
        ({"Origin": "https://evil.example"}, True),
        ({}, True),
        ({"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}, True),
        ({"Origin": ORIGIN}, False),
    ],
    ids=["other-origin", "no-origin", "cross-site", "no-token"],
)
def test_a_scan_asked_for_from_elsewhere_is_refused(
    page: TestClient, inbox: Path, headers: dict[str, str], with_token: bool
) -> None:
    form = {"token": token(page) if with_token else "guess", "symbol": "HAL.NS"}
    response = page.post("/watchlist/scan", data=form, headers=headers)
    assert response.status_code == 403
    assert ondemand.pending_requests(inbox) == []


def test_only_a_stock_on_the_watchlist_can_be_scanned(page: TestClient, inbox: Path) -> None:
    assert post(page, "GC=F").status_code == 404  # a commodity
    assert post(page, "../../x").status_code == 404
    assert page.get("/watchlist/scan", params={"symbol": "NOPE.NS"}).status_code == 404
    assert ondemand.pending_requests(inbox) == []


def test_a_click_asks_once_and_shows_the_wait(page: TestClient, inbox: Path) -> None:
    alive(inbox)
    first = post(page)
    assert first.status_code == 200
    [request] = ondemand.pending_requests(inbox)
    assert request.symbol == "HAL.NS"
    body = first.text
    assert 'hx-trigger="every 1500ms"' in body and "Waiting for the scanner to pick it up" in body
    assert "Scanning HAL…" in body
    # A second click, and "Scan again", while it is asked for: still the one request.
    post(page)
    post(page, again="1")
    assert [r.id for r in ondemand.pending_requests(inbox)] == [request.id]


def test_a_scan_finished_moments_ago_is_shown_again_unless_scan_again(
    page: TestClient, db: sessionmaker[Session], inbox: Path
) -> None:
    alive(inbox)
    add_scan(db)
    body = post(page).text
    assert ondemand.pending_requests(inbox) == []
    assert re.search(r"Scanned \d+ s ago", body)
    assert "hx-trigger" not in body
    assert ">Scan again</button>" in body
    post(page, again="1")
    assert [r.symbol for r in ondemand.pending_requests(inbox)] == ["HAL.NS"]


def test_with_the_scanner_stopped_the_dialog_says_so_and_shows_what_is_stored(
    page: TestClient, inbox: Path
) -> None:
    body = html.unescape(post(page).text)  # no heartbeat at all
    assert "The scanner isn't running, so nothing was fetched." in body
    assert "Start-ScheduledTask -TaskPath" in body and "uv run newsdesk watch" in body
    assert "Latest (0)" in body and 'data-panel="filings"' in body  # what is stored
    # Only the note asks again, slowly; the body itself doesn't poll.
    assert body.count("hx-trigger=") == 1 and 'hx-trigger="every 5s"' in body
    status = page.get("/watchlist/scan", params={"symbol": "HAL.NS", "part": "status"})
    assert 'id="watch-scan-status"' in status.text and 'hx-trigger="every 5s"' in status.text
    assert "HX-Retarget" not in status.headers


def test_after_three_minutes_without_an_answer_it_stops_asking(
    page: TestClient, inbox: Path
) -> None:
    ondemand.write_request(inbox, "HAL.NS", utcnow() - timedelta(minutes=4))
    body = html.unescape(page.get("/watchlist/scan", params={"symbol": "HAL.NS"}).text)
    assert "Nothing came back within 3 minutes: the scanner isn't running." in body
    assert "hx-trigger" not in body and ">Check again</button>" in body


def test_a_busy_scanner_is_named_and_waited_for(page: TestClient, inbox: Path) -> None:
    alive(inbox, "analyse", utcnow() - timedelta(seconds=50))
    ondemand.write_request(inbox, "HAL.NS", utcnow() - timedelta(seconds=25))
    body = page.get("/watchlist/scan", params={"symbol": "HAL.NS"}).text
    assert "finishing its regular analyse pass" in body
    assert 'hx-trigger="every 5s"' in body


def test_without_the_scans_table_it_asks_for_a_restart_not_a_500(
    page: TestClient, database: Path, inbox: Path
) -> None:
    """The web app never runs init_db: until the scanner restarts with this version, there
    is no watch_scans table."""
    engine = make_engine(database)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE watch_scans"))
    engine.dispose()
    alive(inbox)
    got = page.get("/watchlist/scan", params={"symbol": "HAL.NS"})
    assert got.status_code == 200 and "Restart the scanner to enable scans" in got.text
    asked = post(page)
    assert asked.status_code == 200 and "Restart the scanner to enable scans" in asked.text
    assert ondemand.pending_requests(inbox) == []  # nobody could record it
    assert "hx-trigger" not in asked.text


def test_a_running_scan_polls_and_a_finished_one_does_not(
    page: TestClient, db: sessionmaker[Session], inbox: Path
) -> None:
    alive(inbox, "scan HAL.NS")
    steps = ondemand.with_step(ondemand.new_steps(), "google_news", "done", "4 results")
    steps = ondemand.with_step(steps, "nse", "running")
    add_scan(db, status="running", finished_at=None, updated_at=utcnow(), steps=steps)
    running = page.get("/watchlist/scan", params={"symbol": "HAL.NS"}).text
    assert 'hx-trigger="every 1500ms"' in running
    assert 'class="watch-scan-step is-done"' in running and "4 results" in running
    assert 'class="watch-scan-step is-running"' in running

    with db() as session:
        scan = session.scalars(select(WatchScan)).one()
        scan.status, scan.finished_at = "done", utcnow()
        session.commit()
    done = page.get("/watchlist/scan", params={"symbol": "HAL.NS"})
    assert "hx-trigger" not in done.text
    assert "Found 1 new article, 2 filings, 1 new story · 1 LLM call" in done.text
    # The slow note's poll gets the whole body back once there is nothing left to wait for.
    status = page.get("/watchlist/scan", params={"symbol": "HAL.NS", "part": "status"})
    assert status.headers["HX-Retarget"] == "#watch-scan-body"
    assert "hx-trigger" not in status.text


def test_latest_marks_what_is_new_since_the_previous_scan(
    page: TestClient, db: sessionmaker[Session], inbox: Path
) -> None:
    now = utcnow()
    add_scan(db, started_at=now - timedelta(hours=5), finished_at=now - timedelta(hours=5))
    add_scan(db, request_id="second", started_at=now - timedelta(seconds=30))
    with db() as session:
        for title, seen in (
            ("HAL signs a deal with Safran for engines", now - timedelta(minutes=20)),
            ("HAL delivers two Tejas jets to the Air Force", now - timedelta(days=1)),
        ):
            story = WatchStory(first_seen_at=seen, headline=title)
            article = WatchArticle(
                url=f"https://news.example/{abs(hash(title))}",
                source_name="Mint",
                title=title,
                snippet="",
                published_at=seen,
                first_seen_at=seen,
                story=story,
            )
            article.matches = [WatchMatch(symbol="HAL.NS", verdict="keep", reason="named")]
            session.add_all([story, article])
        session.commit()
    alive(inbox)
    body = page.get("/watchlist/scan", params={"symbol": "HAL.NS"}).text
    latest = body.split('data-panel="latest"')[1].split('data-panel="headlines"')[0]
    assert latest.index("Safran") < latest.index("Tejas")  # new first
    assert latest.count("watch-new") == 1
    assert "Headlines (2)" in body and "<b>2</b> keep" in body


def test_stock_cards_open_the_dialog_and_keep_their_remove(page: TestClient) -> None:
    body = page.get("/watchlist").text
    assert '<section id="watch-stock" class="watch-add watch-stock" role="dialog"' in body
    assert 'id="watch-scan-form" hx-post="/watchlist/scan"' in body
    # The stocks, not the commodities: the card itself, and "Scan" in its "⋮" menu.
    assert body.count('class="watch-card-open" data-scan-open=') == 8
    assert body.count("data-scan-open=") == 16
    assert body.count('action="/watchlist/remove"') == 12  # every card keeps Remove
    hal = body.split('data-card-symbol="HAL.NS"')[1].split("data-card-symbol=")[0]
    assert 'aria-haspopup="dialog"' in hal and " hidden>" in hal  # revealed by the script
    name = hal.split('class="watch-card-name"')[1].split("</div>")[0]
    assert "/asset/HAL.NS" not in name  # the dialog's header carries that link now
    assert '<a href="/asset/HAL.NS">Price page</a>' in hal  # and the card's menu
    gold = body.split('data-card-symbol="GC=F"')[1].split("data-card-symbol=")[0]
    assert '<a href="/asset/GC=F">' in gold and "data-scan-open" not in gold
    dialog = post(page).text
    assert '<a class="watch-scan-asset" href="/asset/HAL.NS">Price page →</a>' in dialog


def test_the_card_and_the_news_list_can_be_redrawn_alone(page: TestClient) -> None:
    card = page.get("/watchlist/card", params={"symbol": "HAL.NS"}).text
    assert card.count('data-card-symbol="HAL.NS"') == 1 and 'action="/watchlist/remove"' in card
    assert page.get("/watchlist/card", params={"symbol": "NOPE"}).status_code == 404
    assert 'id="watch-news-list"' in page.get("/watchlist/news").text


def test_the_dialog_reads_the_year_of_prices_the_scanner_fetched(
    page: TestClient, db: sessionmaker[Session], inbox: Path
) -> None:
    add_scan(db, profile=ondemand.profile_to_json(profile("HAL.NS")))
    alive(inbox)
    body = page.get("/watchlist/scan", params={"symbol": "HAL.NS"}).text
    assert "Industrials" in body and "₹3,10,000 Cr" not in body  # Indian grouping isn't used
    assert "₹310,000 Cr" in body
    assert 'aria-label="Daily closes over the last year"' in body


def test_scan_times_are_utc(db: sessionmaker[Session]) -> None:
    add_scan(db)
    with db() as session:
        scan = session.scalars(select(WatchScan)).one()
    assert scan.started_at.tzinfo is not None and scan.started_at.utcoffset() == timedelta(0)
    assert datetime.now(UTC) - scan.started_at < timedelta(minutes=1)
