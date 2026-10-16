"""Adding and removing watchlist stocks from the BASIS page (user, 2026-10-16).

tests/fixtures/nse_equity_list.csv is shaped like NSE's EQUITY_L.csv: its header is copied
from the live file (the column names after the first carry a leading space)."""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.assets import PriceSample
from app.config import CONFIG_DIR, Settings, load_watchlist_file
from app.db import (
    init_db,
    make_engine,
    make_read_only_engine,
    make_read_only_session_factory,
    make_session_factory,
)
from app.models import Article
from app.pipeline.fetch import SourceResolver
from app.watch import companies, watchlist_edit
from app.watch.scan import Watcher
from app.web.main import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "nse_equity_list.csv"
NOW = datetime(2026, 10, 16, 13, 0, tzinfo=UTC)
ORIGIN = "http://testserver"

GOOGLE_NEWS = b"""<rss version="2.0"><channel><title>Search</title>
<item><title>Tata Motors shares jump 4% after JLR sales beat - Mint</title>
<link>https://news.google.com/rss/articles/1</link><source url="https://livemint.com">Mint</source>
<pubDate>Thu, 15 Oct 2026 05:00:00 GMT</pubDate></item>
<item><title>Tata Motors, M&amp;M, Maruti: top auto picks - Moneycontrol</title>
<link>https://news.google.com/rss/articles/2</link><source url="https://moneycontrol.com">Moneycontrol</source>
<pubDate>Wed, 14 Oct 2026 05:00:00 GMT</pubDate></item>
</channel></rss>"""


@pytest.fixture
def listed() -> list[companies.Company]:
    return companies.parse_equity_list(FIXTURE.read_bytes())


@pytest.fixture
def watchlist_copy(tmp_path: Path) -> Path:
    path = tmp_path / "config" / "watchlist.yaml"
    path.parent.mkdir()
    shutil.copy(CONFIG_DIR / "watchlist.yaml", path)
    return path


# ---------------------------------------------------------------- NSE's list


def test_the_list_is_read_as_nse_writes_it(listed: list[companies.Company]) -> None:
    bdl = companies.find(listed, "BDL")
    assert bdl is not None and bdl.name == "Bharat Dynamics Limited"
    assert bdl.isin == "INE171Z01018" and bdl.series == "EQ" and bdl.yahoo_symbol == "BDL.NS"
    with pytest.raises(ValueError, match="not NSE's equity list"):
        companies.parse_equity_list(b"a,b\n1,2\n")


def test_type_ahead_finds_by_symbol_or_any_word_of_the_name(
    listed: list[companies.Company],
) -> None:
    assert [c.symbol for c in companies.search(listed, "BDL")][:1] == ["BDL"]
    assert "BDL" in [c.symbol for c in companies.search(listed, "dynam")]
    assert [c.symbol for c in companies.search(listed, "tata")] == ["TATAMOTORS"]
    assert companies.search(listed, "t") == []  # one letter is not a search


def test_a_draft_takes_the_name_and_judges_the_symbol(listed: list[companies.Company]) -> None:
    bdl = companies.draft(companies.find(listed, "BDL"))  # type: ignore[arg-type]
    assert bdl.name == "Bharat Dynamics"
    assert bdl.aliases.strong == ["Bharat Dynamics"] and bdl.aliases.weak == ["BDL"]
    pfocus = companies.draft(companies.find(listed, "PFOCUS"))  # type: ignore[arg-type]
    assert pfocus.aliases.strong == ["Prime Focus", "PFOCUS"] and pfocus.aliases.weak == []
    assert pfocus.nse_name == "Prime Focus Limited" and pfocus.isin == "INE367G01020"
    assert companies.draft(companies.find(listed, "360ONE")).name == "360 One Wam"  # type: ignore[arg-type]
    assert companies.parse_aliases(" Tata Motors, , TML ,Tata Motors") == ["Tata Motors", "TML"]


def test_a_refresh_replaces_the_list_only_with_the_list(tmp_path: Path) -> None:
    path = tmp_path / "list.csv"
    good = httpx.MockTransport(lambda r: httpx.Response(200, content=FIXTURE.read_bytes()))
    assert "10 companies" in companies.refresh(path, "test", 5, transport=good)
    bad = httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>blocked</html>"))
    assert "not refreshed" in companies.refresh(path, "test", 5, transport=bad)
    assert len(companies.load_companies(path)) == 10  # the good copy stays


# ---------------------------------------------------------------- the file


def test_adding_then_removing_leaves_the_file_as_it_was(
    watchlist_copy: Path, listed: list[companies.Company], tmp_path: Path
) -> None:
    original = watchlist_copy.read_text(encoding="utf-8")
    item = companies.draft(companies.find(listed, "TATAMOTORS"))  # type: ignore[arg-type]
    backups = tmp_path / "backups"
    added = watchlist_edit.add_stock(
        watchlist_copy, item, watchlist_edit.version_of(original), NOW, backup_dir=backups
    )
    symbols = [e.symbol for e in added.watchlist]
    # After the last stock, before the commodities.
    assert symbols.index("TATAMOTORS.NS") == symbols.index("ASTRAMICRO.NS") + 1
    text = watchlist_copy.read_text(encoding="utf-8")
    assert "# Added from the BASIS page on 2026-10-16" in text
    assert '# Never "Astra" alone' in text  # every comment kept
    assert load_watchlist_file(watchlist_copy).stocks[-1].aliases.strong == [
        "Tata Motors",
        "TATAMOTORS",
    ]
    watchlist_edit.remove_entry(
        watchlist_copy,
        "TATAMOTORS.NS",
        watchlist_edit.version_of(text),
        NOW.replace(minute=1),
        backup_dir=backups,
    )
    assert watchlist_copy.read_text(encoding="utf-8") == original
    assert len(list(backups.glob("watchlist.*.yaml"))) == 2


def test_removing_a_stock_takes_its_comments_and_nothing_else(watchlist_copy: Path) -> None:
    before = load_watchlist_file(watchlist_copy)
    text = watchlist_copy.read_text(encoding="utf-8")
    after = watchlist_edit.remove_entry(
        watchlist_copy,
        "HAL.NS",
        watchlist_edit.version_of(text),
        NOW,
        backup_dir=watchlist_copy.parent,
    )
    assert [e.symbol for e in after.watchlist] == [
        e.symbol for e in before.watchlist if e.symbol != "HAL.NS"
    ]
    text = watchlist_copy.read_text(encoding="utf-8")
    assert "HATSOFF" not in text and "flypast" not in text
    assert "Lebanon's central bank" in text  # BDL's comment is still there


def test_an_edit_to_a_file_that_changed_since_the_page_loaded_is_refused(
    watchlist_copy: Path, listed: list[companies.Company]
) -> None:
    stale = watchlist_edit.version_of(watchlist_copy.read_text(encoding="utf-8"))
    watchlist_copy.write_text(watchlist_copy.read_text(encoding="utf-8") + "\n# by hand\n")
    item = companies.draft(companies.find(listed, "TATAMOTORS"))  # type: ignore[arg-type]
    with pytest.raises(watchlist_edit.EditRefused, match="changed since this page loaded"):
        watchlist_edit.add_stock(watchlist_copy, item, stale, NOW, backup_dir=watchlist_copy.parent)
    current = watchlist_edit.version_of(watchlist_copy.read_text(encoding="utf-8"))
    bdl = companies.draft(companies.find(listed, "BDL"))  # type: ignore[arg-type]
    with pytest.raises(watchlist_edit.EditRefused, match="already on the watchlist"):
        watchlist_edit.add_stock(
            watchlist_copy, bdl, current, NOW, backup_dir=watchlist_copy.parent
        )


# ---------------------------------------------------------------- the page


@pytest.fixture
def page(tmp_path: Path, settings: Settings, watchlist_copy: Path) -> TestClient:
    database = tmp_path / "newsdesk.db"
    engine = make_engine(database)
    init_db(engine)
    with make_session_factory(engine)() as session:
        session.add(
            Article(
                url="https://example.com/tm",
                source_name="CNBC",
                source_region="IN",
                source_weight=2,
                title="Tata Motors to cut EV prices",
                snippet="",
                # The preview reads the last 30 days from the real clock.
                published_at=datetime.now(UTC) - timedelta(days=1),
                fetched_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        session.commit()
    engine.dispose()

    def yahoo(symbol: str) -> PriceSample:
        return PriceSample([(NOW, 712.5)], {"exchangeName": "NSI", "currency": "INR"})

    reader = make_read_only_engine(database)
    app = create_app(
        settings,
        make_read_only_session_factory(reader),
        watchlist_path=watchlist_copy,
        companies_path=FIXTURE,
        backup_dir=tmp_path / "backups",
        allowed_hosts=frozenset({"testserver"}),
        preview_transport=httpx.MockTransport(lambda r: httpx.Response(200, content=GOOGLE_NEWS)),
        ticker_fetch=yahoo,
    )
    return TestClient(app)


def _form(client: TestClient) -> dict[str, str]:
    """The token and file version the page hands its forms."""
    body = client.get("/watchlist").text
    token = body.split('name="token" value="')[1].split('"')[0]
    version = body.split('name="version" value="')[1].split('"')[0]
    return {"token": token, "version": version}


def test_the_page_has_an_add_button_and_a_remove_on_every_card(page: TestClient) -> None:
    body = page.get("/watchlist").text
    assert '<a class="btn btn-primary" href="#watch-add">Add a stock</a>' in body
    assert body.count('action="/watchlist/remove"') == 12  # 8 stocks and 4 commodities
    assert 'id="watch-search"' in body and 'hx-get="/watchlist/companies"' in body


def test_suggestions_come_as_you_type(page: TestClient) -> None:
    body = page.get("/watchlist/companies", params={"q": "dynamics"}).text
    assert "Bharat Dynamics" in body and "on the watchlist" in body
    assert "Tata Motors" in page.get("/watchlist/companies", params={"q": "tata"}).text
    assert (
        "No NSE-listed company matches"
        in page.get("/watchlist/companies", params={"q": "zzzz"}).text
    )


def test_the_preview_checks_yahoo_and_shows_keeps_and_drops(page: TestClient) -> None:
    body = page.get("/watchlist/preview", params={"symbol": "TATAMOTORS"}).text
    assert "TATAMOTORS.NS on Yahoo: last close 712.50 INR (NSI)" in body
    assert 'value="Tata Motors, TATAMOTORS"' in body  # editable before adding
    assert "Tata Motors shares jump 4% after JLR sales beat" in body  # Google News: a keep
    assert "Tata Motors to cut EV prices" in body  # a stored headline
    assert "top auto picks" in body  # a mention
    assert "<b>2</b> keep" in body and "<b>1</b> mention" in body
    assert "Add to watchlist</button>" in body and "disabled>Add" not in body


def test_adding_from_the_page_writes_the_file(page: TestClient, watchlist_copy: Path) -> None:
    form = _form(page) | {
        "symbol": "TATAMOTORS",
        "name": "Tata Motors",
        "strong": "Tata Motors, TATAMOTORS",
        "weak": "TML",
        "group": "",
    }
    response = page.post(
        "/watchlist/add", data=form, headers={"Origin": ORIGIN}, follow_redirects=False
    )
    assert response.status_code == 303 and "added=Tata%20Motors" in response.headers["location"]
    entry = load_watchlist_file(watchlist_copy).stocks[-1]
    assert entry.symbol == "TATAMOTORS.NS" and entry.aliases.weak == ["TML"]
    assert "Added Tata Motors" in page.get("/watchlist", params={"added": "Tata Motors"}).text
    assert "Tata Motors" in page.get("/watchlist").text  # the card, without a restart


def test_removing_from_the_page_writes_the_file(page: TestClient, watchlist_copy: Path) -> None:
    form = _form(page) | {"symbol": "GC=F"}
    response = page.post(
        "/watchlist/remove", data=form, headers={"Origin": ORIGIN}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "GC=F" not in [e.symbol for e in load_watchlist_file(watchlist_copy).watchlist]


@pytest.mark.parametrize(
    ("headers", "token"),
    [
        ({"Origin": "https://evil.example"}, True),  # another site
        ({}, True),  # no Origin and no Referer
        ({"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}, True),
        ({"Origin": ORIGIN}, False),  # no token
    ],
    ids=["other-origin", "no-origin", "cross-site", "no-token"],
)
def test_a_write_not_from_this_page_is_refused(
    page: TestClient, watchlist_copy: Path, headers: dict[str, str], token: bool
) -> None:
    before = watchlist_copy.read_text(encoding="utf-8")
    form = _form(page) | {"symbol": "GC=F"}
    if not token:
        form["token"] = "guess"
    response = page.post("/watchlist/remove", data=form, headers=headers, follow_redirects=False)
    assert response.status_code == 403
    assert watchlist_copy.read_text(encoding="utf-8") == before


def test_a_write_to_a_name_other_than_localhost_is_refused(
    tmp_path: Path, settings: Settings, watchlist_copy: Path
) -> None:
    """DNS rebinding: a site pointing its own name at 127.0.0.1 still sends its own Host."""
    database = tmp_path / "w.db"
    init_db(make_engine(database))
    app = create_app(
        settings,
        make_read_only_session_factory(make_read_only_engine(database)),
        watchlist_path=watchlist_copy,
        companies_path=FIXTURE,
    )  # the real allowed hosts: 127.0.0.1, localhost, ::1 - not "testserver"
    response = TestClient(app).post(
        "/watchlist/remove", data={"symbol": "GC=F"}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 403


# ---------------------------------------------------------------- the scanner


def test_the_scanner_picks_up_an_edit_without_a_restart(
    settings: Settings, tmp_path: Path, watchlist_copy: Path, listed: list[companies.Company]
) -> None:
    engine = make_engine(tmp_path / "scan.db")
    init_db(engine)
    db: sessionmaker[Session] = make_session_factory(engine)
    watcher = Watcher(
        settings,
        db,
        load_watchlist_file(watchlist_copy),
        [],
        SourceResolver([]),
        None,
        None,
        watchlist_path=watchlist_copy,
    )
    assert watcher.refresh_watchlist() is False  # nothing changed yet
    text = watchlist_copy.read_text(encoding="utf-8")
    item = companies.draft(companies.find(listed, "TATAMOTORS"))  # type: ignore[arg-type]
    watchlist_edit.add_stock(
        watchlist_copy, item, watchlist_edit.version_of(text), NOW, backup_dir=tmp_path
    )
    assert watcher.refresh_watchlist() is True
    assert "TATAMOTORS.NS" in [s.symbol for s in watcher.stocks]
    assert [m.symbol for m in watcher.matcher.match("Tata Motors shares jump 4%")] == [
        "TATAMOTORS.NS"
    ]
    assert any("Tata+Motors" in q.url for q in watcher.queries)
    # A broken file keeps the list in use.
    watchlist_copy.write_text("watchlist: [oops", encoding="utf-8")
    assert watcher.refresh_watchlist() is False
    assert "TATAMOTORS.NS" in [s.symbol for s in watcher.stocks]
