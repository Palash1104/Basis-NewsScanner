from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.config import Settings, load_assets
from app.db import (
    init_db,
    make_engine,
    make_read_only_engine,
    make_read_only_session_factory,
    make_session_factory,
)
from app.models import Impact, Run, Story
from app.web import palette
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
            summary="One. Two.",
            status="analyzed",
        )
        session.add(story)
        session.flush()
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
    body = client.get("/").text
    assert "Research notes, not financial advice." in body
    assert "Design foundation" in body
    assert "data-theme-toggle" in body
    assert client.get("/design").status_code == 200


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
