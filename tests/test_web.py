import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.config import Settings, load_assets
from app.db import (
    init_db,
    make_engine,
    make_read_only_engine,
    make_read_only_session_factory,
    make_session_factory,
)
from app.models import Article, Event, Impact, ImpactScore, RuleDisagreementRow, Run, Story
from app.web import palette, queries
from app.web.main import create_app, ticker_items

NOW = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)
CSS_DIR = Path("app/web/static/css")
DESIGN_SYSTEM = CSS_DIR / "design-system.css"
EXPORTED_STYLESHEET = Path("design/_ds/modernist-3dfd6d1f-f6ac-418e-8f3e-37cf9f987647/styles.css")


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """A real file (not :memory:), since the web UI opens its own connection to it."""
    path = tmp_path / "newsdesk.db"
    engine = make_engine(path)
    init_db(engine)
    with make_session_factory(engine)() as session:
        story = Story(
            first_seen_at=NOW - timedelta(hours=3),
            updated_at=NOW - timedelta(hours=3),
            headline="Houthi attacks close the Red Sea to tankers",
            summary="Shipping is rerouting around the Cape.",
            status="analyzed",
            category="Geopolitics",
            regions=["Global"],
            importance_score=8.0,
        )
        session.add(story)
        session.flush()
        session.add_all(
            [
                Article(
                    url="https://reuters.example/red-sea",
                    title="Tankers avoid the Red Sea",
                    source_name="Reuters",
                    source_region="GLOBAL",
                    source_weight=3,
                    published_at=NOW - timedelta(hours=3),
                    fetched_at=NOW - timedelta(hours=3),
                    story_id=story.id,
                ),
                Article(
                    url="https://ft.example/red-sea",
                    title="Freight rates jump",
                    source_name="Financial Times",
                    source_region="GLOBAL",
                    source_weight=3,
                    published_at=NOW - timedelta(hours=2, minutes=30),
                    fetched_at=NOW - timedelta(hours=2, minutes=30),
                    story_id=story.id,
                ),
            ]
        )
        session.add_all(
            [
                Impact(
                    story_id=story.id,
                    symbol="BZ=F",
                    direction="up",
                    mechanism="shipping risk",
                    order="first",
                    confidence="high",
                    origin="playbook",
                    rule_id="oil_supply_shock",
                    reference_price=70.0,
                    move_at_detection_pct=2.4,
                    created_at=NOW - timedelta(hours=2),
                ),
                Impact(
                    story_id=story.id,
                    symbol="^TNX",
                    direction="down",
                    mechanism="safe haven",
                    order="second",
                    confidence="low",
                    origin="llm",
                    reference_price=4.1,
                    move_at_detection_pct=-1.2,
                    created_at=NOW - timedelta(hours=2),
                ),
            ]
        )
        session.add_all(
            [
                # One asset called both ways by different layers: "mixed signals".
                Impact(
                    story_id=story.id,
                    symbol="GC=F",
                    direction="up",
                    mechanism="Safe-haven demand",
                    order="first",
                    confidence="medium",
                    origin="playbook",
                    rule_id="geopolitical_risk_off",
                    conflict=True,
                    created_at=NOW - timedelta(hours=2),
                ),
                Impact(
                    story_id=story.id,
                    symbol="GC=F",
                    direction="down",
                    mechanism="Higher yields",
                    order="second",
                    confidence="low",
                    origin="llm",
                    conflict=True,
                    created_at=NOW - timedelta(hours=2),
                ),
            ]
        )
        # A story with nothing to say about markets: the common case after a decline.
        session.add(
            Story(
                first_seen_at=NOW - timedelta(hours=4),
                updated_at=NOW - timedelta(hours=4),
                headline="Parliament debates the water treaty",
                summary="Nothing for markets here.",
                status="summarized",
                category="Politics",
                regions=["India"],
                importance_score=1.0,
            )
        )
        session.add(
            Run(
                kind="pipeline",
                started_at=NOW - timedelta(hours=2, minutes=5),
                finished_at=NOW - timedelta(hours=2),
            )
        )
        session.commit()
    engine.dispose()
    return path


@pytest.fixture
def client(database: Path, settings: Settings) -> TestClient:
    engine = make_read_only_engine(database)
    return TestClient(create_app(settings, make_read_only_session_factory(engine)))


# ---------------------------------------------------------------- read-only


def test_the_web_engine_refuses_to_write(database: Path) -> None:
    """The pipeline writes this file every three hours; a page must never be able to."""
    engine = make_read_only_engine(database)
    with make_read_only_session_factory(engine)() as session, pytest.raises(OperationalError):
        session.add(Run(kind="pipeline", started_at=NOW))
        session.commit()


def test_a_missing_database_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="newsdesk run"):
        make_read_only_engine(tmp_path / "nothing.db")


def test_creating_the_app_never_migrates(
    database: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`init_db` runs ALTER TABLE; the web UI must never do that to a live database."""
    import app.db

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the web app must not call init_db")

    monkeypatch.setattr(app.db, "init_db", fail)
    engine = make_read_only_engine(database)
    create_app(settings, make_read_only_session_factory(engine))


def test_pages_render_while_the_pipeline_holds_a_write_transaction(
    client: TestClient, database: Path
) -> None:
    """WAL: a writer mid-run must not stop the page from loading."""
    writer = make_engine(database)
    with make_session_factory(writer)() as session:
        session.add(Run(kind="pipeline", started_at=NOW))
        session.flush()  # holds a write transaction open
        response = client.get("/")
        assert response.status_code == 200
        session.rollback()
    writer.dispose()


# ---------------------------------------------------------------- the page


def test_the_design_page_renders_with_the_footer_and_both_themes(client: TestClient) -> None:
    body = client.get("/design").text
    assert "Research notes, not financial advice." in body
    assert "Design foundation" in body
    assert "data-theme-toggle" in body


def test_static_files_are_served_locally(client: TestClient) -> None:
    for path, expected in [
        ("/static/css/design-system.css", "--color-accent: #ec3013"),
        ("/static/css/theme.css", "--color-up"),
        ("/static/css/app.css", "@font-face"),
        ("/static/js/htmx.min.js", "htmx"),
        ("/static/js/theme.js", "newsdesk-theme"),
        ("/static/fonts/OFL.txt", "SIL OPEN FONT LICENSE"),
    ]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert expected in response.text, path


def test_nothing_is_fetched_from_the_network(client: TestClient) -> None:
    """No CDN, no Google Fonts: everything the page loads is vendored."""
    body = client.get("/").text
    for host in ("https://fonts.googleapis.com", "https://fonts.gstatic.com", "unpkg.com", "cdn."):
        assert host not in body


# ---------------------------------------------------------------- the ticker


def test_the_ticker_shows_the_biggest_move_per_asset_with_its_unit(
    database: Path, settings: Settings
) -> None:
    engine = make_read_only_engine(database)
    assets = {asset.symbol: asset for asset in load_assets()}
    with make_read_only_session_factory(engine)() as session:
        items = ticker_items(session, assets, NOW)

    assert [item.name for item in items] == ["Brent crude", "US 10-year yield"]
    assert items[0].move == "+2.4%" and items[0].up
    # Rates are judged in points, and the web uses the same formatter as the digest.
    assert items[1].move.endswith(" pts") and not items[1].up


def test_the_ticker_is_labelled_since_news_and_stamped(client: TestClient) -> None:
    body = client.get("/").text
    assert "since news" in body
    assert "as of 21 Sep 09:30 IST" in body  # the last pipeline run, in the display zone


def test_an_old_call_is_not_in_the_ticker(database: Path, settings: Settings) -> None:
    engine = make_read_only_engine(database)
    assets = {asset.symbol: asset for asset in load_assets()}
    with make_read_only_session_factory(engine)() as session:
        items = ticker_items(session, assets, NOW + timedelta(days=4))
    assert items == []


# ---------------------------------------------------------------- palette


def test_both_themes_meet_wcag_aa_except_where_the_design_says_otherwise() -> None:
    for name, theme in [("light", palette.LIGHT), ("dark", palette.DARK)]:
        for check in palette.CHECKS:
            ratio = check.ratio(theme)
            if check.accepted_below_aa:
                continue
            assert ratio >= check.minimum, f"{name}: {check.pair} is {ratio:.2f}:1"


def test_the_one_accepted_shortfall_is_the_buttons_and_stays_visible() -> None:
    """The export's button is the page ground on the accent: 3.76:1 light, 4.18:1 dark. It is
    kept because it is the design's own colour, so the number is reported rather than hidden,
    and this test fails if it silently gets worse."""
    (button,) = [check for check in palette.CHECKS if check.accepted_below_aa]
    assert button.ratio(palette.LIGHT) == pytest.approx(3.76, abs=0.01)
    assert button.ratio(palette.DARK) == pytest.approx(4.18, abs=0.01)
    verdicts = {row["pair"]: row["verdict"] for row in palette.rows()}
    assert any("below AA" in verdict for verdict in verdicts.values())


def test_the_palette_matches_the_stylesheets() -> None:
    """palette.py states the contrast guarantee; the CSS is what the browser paints."""
    css = "".join(path.read_text(encoding="utf-8") for path in CSS_DIR.glob("*.css"))
    for theme in (palette.LIGHT, palette.DARK):
        for key, value in theme.items():
            assert value in css, f"{key} ({value}) is in no stylesheet"


def test_the_vendored_design_system_is_the_export_unmodified() -> None:
    """The export is the source of truth: our copy must be byte for byte the same file."""
    assert DESIGN_SYSTEM.read_bytes() == EXPORTED_STYLESHEET.read_bytes()


def test_muted_text_is_darker_than_the_systems_own_default() -> None:
    """The export's .text-muted (55%) fails AA at body size, so pages use 70%."""
    faint = palette.blend(palette.LIGHT["text"], palette.LIGHT["bg"], palette.FAINT_ALPHA)
    muted = palette.blend(palette.LIGHT["text"], palette.LIGHT["bg"], palette.MUTED_ALPHA)
    assert palette.contrast(faint, palette.LIGHT["bg"]) < palette.AA_TEXT
    assert palette.contrast(muted, palette.LIGHT["bg"]) >= palette.AA_TEXT


# ---------------------------------------------------------------- the feed


def test_the_feed_shows_the_story_and_what_it_calls(client: TestClient) -> None:
    body = client.get("/").text
    assert "What moved, and why" in body  # the mockup's own page title
    assert "Houthi attacks close the Red Sea to tankers" in body
    assert "Shipping is rerouting around the Cape." in body
    assert "Geopolitics" in body and "Global" in body
    # The call, stated as the digest states it: direction, order, confidence, origin, why.
    assert "Brent crude" in body and "+2.4%" in body and "since news" in body
    assert "first order" in body and "playbook" in body and "shipping risk" in body
    assert " pts" in body  # rates keep their unit here too


def test_a_story_with_no_calls_says_so(client: TestClient) -> None:
    """Declining is common (13 of 20 fixtures in the Phase 5 gate), so it needs words."""
    assert "No market impact identified." in client.get("/").text


def test_an_asset_called_both_ways_is_shown_as_mixed_signals(client: TestClient) -> None:
    body = client.get("/").text
    assert "mixed signals" in body
    assert "Safe-haven demand vs Higher yields" in body


def test_an_unpriced_call_says_it_has_no_price_yet(client: TestClient) -> None:
    """SPEC 7.8's "price unavailable": the market may simply not have opened yet."""
    assert "no price yet" in client.get("/").text


def test_the_feed_links_its_sources_and_carries_the_disclaimer(client: TestClient) -> None:
    body = client.get("/").text
    assert "Sources:" in body
    assert "Research notes, not financial advice." in body


# ---------------------------------------------------------------- filters and search


def test_htmx_gets_the_list_alone(client: TestClient) -> None:
    fragment = client.get("/", headers={"HX-Request": "true"}).text
    assert '<div id="feed"' in fragment
    assert "<html" not in fragment


def test_the_region_filter_narrows_the_feed(client: TestClient) -> None:
    india = client.get("/", params={"region": "India"}).text
    assert "Parliament debates the water treaty" in india
    assert "Houthi attacks" not in india


def test_the_category_filter_narrows_the_feed(client: TestClient) -> None:
    body = client.get("/", params={"category": "Geopolitics"}).text
    assert "Houthi attacks" in body and "Parliament debates" not in body


def test_search_finds_a_story_by_a_word_in_its_headline(client: TestClient) -> None:
    body = client.get("/", params={"q": "tankers"}).text
    assert "Houthi attacks close the Red Sea to tankers" in body
    assert "Parliament debates" not in body
    assert "1 result for" in body


def test_search_matches_a_prefix_as_you_type(client: TestClient) -> None:
    assert "Houthi" in client.get("/", params={"q": "tank"}).text


def test_search_that_matches_nothing_says_so(client: TestClient) -> None:
    assert "nothing matched" in client.get("/", params={"q": "zirconium"}).text


def test_punctuation_in_a_search_cannot_break_the_query(client: TestClient) -> None:
    """FTS5 has a query syntax of its own; what someone types is words, not an expression."""
    for query in ['"', "AND", "red* OR", "NEAR(a b)", "()", "-", "^"]:
        assert client.get("/", params={"q": query}).status_code == 200, query


def test_the_search_index_is_there_for_a_database_the_pipeline_built(database: Path) -> None:
    engine = make_read_only_engine(database)
    with make_read_only_session_factory(engine)() as session:
        assert queries.search_ready(session)
        assert queries.search_story_ids(session, "tankers")


# ---------------------------------------------------------------- sparklines


def test_a_sparkline_is_empty_when_nothing_is_cached(database: Path) -> None:
    """An asset that has never been called has no bars, and gets no line rather than a
    made-up one."""
    engine = make_read_only_engine(database)
    with make_read_only_session_factory(engine)() as session:
        series = queries.series_for(session, ["BZ=F"], "1w", NOW)
    assert series["BZ=F"].closes == ()
    assert queries.sparkline_points(series["BZ=F"], 68, 22, 2) == ""


def test_a_long_series_is_sampled_down_to_the_mockups_density() -> None:
    series = queries.Series("X", tuple(float(value) for value in range(500)))
    points = queries.sparkline_points(series, 68, 22, 2).split(" ")
    assert len(points) <= queries.SPARK_MAX_POINTS
    assert points[0].startswith("0.0,") and points[-1].startswith("68.0,")


def test_each_window_reads_the_interval_that_has_the_data() -> None:
    assert queries.WINDOWS["1d"][0] == "60m"
    assert queries.WINDOWS["1w"][0] == "60m"
    assert queries.WINDOWS["1m"][0] == "1d"


def test_an_unknown_window_falls_back_rather_than_failing(client: TestClient) -> None:
    assert client.get("/", params={"range": "10y"}).status_code == 200


def test_a_story_summarized_today_shows_even_if_it_broke_earlier(
    database: Path, settings: Settings
) -> None:
    """The reserved slots summarize Indian stories days after they break; windowing on
    first_seen_at alone would hide exactly those."""
    engine = make_engine(database)
    with make_session_factory(engine)() as session:
        session.add(
            Story(
                first_seen_at=NOW - timedelta(days=4),
                updated_at=NOW - timedelta(minutes=5),
                headline="India's software exports rise 8.2%",
                summary="Exports grew.",
                status="summarized",
                category="Economy & Markets",
                regions=["India"],
                importance_score=3.5,
            )
        )
        session.commit()
    engine.dispose()

    engine = make_read_only_engine(database)
    client = TestClient(create_app(settings, make_read_only_session_factory(engine)))
    body = client.get("/", params={"region": "India"}).text
    assert "software exports rise 8.2%" in body  # the apostrophe is HTML-escaped


# ---------------------------------------------------------------- one story


def _story_id(client: TestClient) -> int:
    """The Red Sea story, found the way a reader would: from a link on the feed."""
    body = client.get("/").text
    match = re.search(r'href="/story/(\d+)">Houthi', body)
    assert match, "the feed should link its headlines to the story page"
    return int(match.group(1))


def test_the_feed_links_to_the_story_page(client: TestClient) -> None:
    assert client.get(f"/story/{_story_id(client)}").status_code == 200


def test_the_story_page_shows_what_the_story_says(client: TestClient) -> None:
    body = client.get(f"/story/{_story_id(client)}").text
    assert "Houthi attacks close the Red Sea to tankers" in body
    assert "Shipping is rerouting around the Cape." in body
    assert "Geopolitics" in body and "Global" in body


def test_the_story_page_lists_every_call_not_just_the_top_ones(client: TestClient) -> None:
    body = client.get(f"/story/{_story_id(client)}").text
    assert "Every call, and how it is doing" in body
    for expected in ["Brent crude", "US 10-year yield", "Gold", "oil_supply_shock"]:
        assert expected in body, expected
    assert "playbook" in body and "LLM" in body  # both origins are named


def test_the_story_page_shows_mixed_signals_and_both_mechanisms(client: TestClient) -> None:
    body = client.get(f"/story/{_story_id(client)}").text
    assert "mixed signals" in body
    assert "Safe-haven demand vs Higher yields" in body


def test_an_unpriced_call_and_a_missing_reference_are_spelled_out(client: TestClient) -> None:
    body = client.get(f"/story/{_story_id(client)}").text
    assert "no price yet" in body
    assert "waiting for its market" in body


def test_a_call_with_no_score_yet_says_not_due(client: TestClient) -> None:
    assert "not due yet" in client.get(f"/story/{_story_id(client)}").text


def test_scores_are_shown_with_their_workings(client: TestClient, database: Path) -> None:
    """A hit is only meaningful next to the benchmark and the threshold it beat."""
    story_id = _story_id(client)
    engine = make_engine(database)
    with make_session_factory(engine)() as session:
        impact = session.scalars(select(Impact).where(Impact.symbol == "BZ=F")).one()
        session.add(
            ImpactScore(
                impact_id=impact.id,
                horizon_days=1,
                asset_return=0.031,
                benchmark_symbol="^GSPC",
                benchmark_return=0.004,
                excess_return=0.027,
                threshold=0.012,
                outcome="hit",
                scored_at=NOW,
            )
        )
        session.commit()
    engine.dispose()

    body = client.get(f"/story/{story_id}").text
    assert "1d hit" in body
    assert "asset +3.1%" in body and "^GSPC" in body
    assert "excess +2.7%" in body and "needs 1.2%" in body


def test_the_event_and_its_provenance_are_shown(client: TestClient, database: Path) -> None:
    story_id = _story_id(client)
    engine = make_engine(database)
    with make_session_factory(engine)() as session:
        session.add(
            Event(
                story_id=story_id,
                event_type="geopolitical_conflict",
                countries=["Yemen", "Saudi Arabia"],
                regions=["Global"],
                entities=["Houthis"],
                companies=[],
                channels=["oil_supply", "shipping_routes"],
                severity="escalation",
                policy_stance="not_applicable",
                policy_actor=None,
                is_new_development=True,
                model="gemini-3.5-flash-lite",
                prompt_version="event-v3",
                temperature=0.0,
                seed=20260921,
                created_at=NOW,
            )
        )
        session.commit()
    engine.dispose()

    body = client.get(f"/story/{story_id}").text
    assert "geopolitical_conflict" in body and "escalation" in body
    assert "Yemen, Saudi Arabia" in body
    assert "oil_supply, shipping_routes" in body
    assert "event-v3" in body and "temperature 0.0" in body and "seed 20260921" in body


def test_a_story_with_no_event_says_so(client: TestClient) -> None:
    assert "No event extracted" in client.get(f"/story/{_story_id(client)}").text


def test_rule_disagreements_are_shown_with_their_reason(client: TestClient, database: Path) -> None:
    story_id = _story_id(client)
    engine = make_engine(database)
    with make_session_factory(engine)() as session:
        session.add(
            RuleDisagreementRow(
                story_id=story_id,
                event_id=None,
                rule_id="us_nato_defense_spending",
                reason="the story is about shipping, not defence budgets",
                model="gemini-3.5-flash-lite",
                prompt_version="impact-v1",
                temperature=0.0,
                seed=20260921,
                created_at=NOW,
            )
        )
        session.commit()
    engine.dispose()

    body = client.get(f"/story/{story_id}").text
    assert "Where the model disputed a rule" in body
    assert "us_nato_defense_spending" in body
    assert "the story is about shipping, not defence budgets" in body
    assert "impact-v1" in body


def test_every_source_article_is_listed_with_its_link(client: TestClient) -> None:
    body = client.get(f"/story/{_story_id(client)}").text
    assert "Tankers avoid the Red Sea" in body and "reuters.example/red-sea" in body
    assert "Freight rates jump" in body and "ft.example/red-sea" in body


def test_the_summary_provenance_is_on_the_page(client: TestClient, database: Path) -> None:
    story_id = _story_id(client)
    engine = make_engine(database)
    with make_session_factory(engine)() as session:
        story = session.get(Story, story_id)
        story.model = "gemini-3.5-flash-lite"
        story.prompt_version = "summary-v3"
        story.temperature = 0.0
        story.seed = 20260921
        session.commit()
    engine.dispose()

    body = client.get(f"/story/{story_id}").text
    assert "summary-v3" in body and "temperature 0.0" in body and "seed 20260921" in body


def test_a_story_that_does_not_exist_gets_a_page_not_a_json_blob(client: TestClient) -> None:
    response = client.get("/story/999999")
    assert response.status_code == 404
    assert "Not found" in response.text
    assert "<html" in response.text


def test_a_story_with_no_calls_says_so_on_its_page(client: TestClient, database: Path) -> None:
    engine = make_read_only_engine(database)
    with make_read_only_session_factory(engine)() as session:
        quiet = session.scalars(select(Story).where(Story.headline.like("Parliament%"))).one()
    assert "No market impact identified." in client.get(f"/story/{quiet.id}").text


def test_an_older_story_carries_its_age_on_the_feed(database: Path, settings: Settings) -> None:
    """The time shown is when the story broke, so the age belongs on that line, not a new one."""
    engine = make_engine(database)
    with make_session_factory(engine)() as session:
        session.add(
            Story(
                first_seen_at=NOW - timedelta(days=3),
                updated_at=NOW - timedelta(minutes=10),
                headline="India-New Zealand FTA ratified",
                summary="It comes into force next month.",
                status="summarized",
                category="Economy & Markets",
                regions=["India"],
                importance_score=3.3,
            )
        )
        session.commit()
    engine.dispose()

    engine = make_read_only_engine(database)
    client = TestClient(create_app(settings, make_read_only_session_factory(engine)))
    body = client.get("/", params={"region": "India"}).text
    assert "India-New Zealand FTA ratified" in body
    assert "d ago" in body


def test_a_fresh_story_carries_no_age(client: TestClient) -> None:
    body = client.get("/").text
    assert "Houthi attacks close the Red Sea to tankers" in body
    assert "first reported" not in body
