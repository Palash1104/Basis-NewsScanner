"""Adding and removing watchlist stocks from the BASIS page (user, 2026-10-16), with BSE
companies and new listings (user, 2026-10-17).

The fixtures are written the way the three company lists are:

  nse_equity_list.csv  eleven real rows of NSE's EQUITY_L.csv as downloaded on 2026-10-17,
                       header and all (the column names after the first carry a leading
                       space). Tata Motors is TMPV there since its demerger.
  nse_sme_list.csv     SME_EQUITY_L.csv's real header (underscores, a trailing comma, two-digit
                       years) over two made-up companies: SMEDEMO and SMEOLD are not real.
  bse_scrips.json      rows of BSE's ListofScripData. ABB's is the PEAD tool's real row
                       verbatim; the other codes and ISINs are BSE's own, from its list of
                       2026-10-16 (the PEAD tool's scrip master), with names and scrip ids
                       written for the test. 999999 "Not A Real Company" is made up, and
                       suspended.
"""

import json
import shutil
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.assets import NoPriceData, PriceSample
from app.config import CONFIG_DIR, Settings, WatchAliases, WatchItem, load_watchlist_file
from app.db import (
    init_db,
    make_engine,
    make_read_only_engine,
    make_read_only_session_factory,
    make_session_factory,
)
from app.models import Article
from app.pipeline.fetch import SourceResolver
from app.watch import companies, preview, watchlist_edit
from app.watch.analyse import company_label
from app.watch.scan import Watcher
from app.web.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
NSE_CSV = (FIXTURES / "nse_equity_list.csv").read_bytes()
SME_CSV = (FIXTURES / "nse_sme_list.csv").read_bytes()
BSE_ROWS = json.loads((FIXTURES / "bse_scrips.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 16, 13, 0, tzinfo=UTC)  # 18:30 IST, 16 Oct
TODAY = date(2026, 10, 16)
ORIGIN = "http://testserver"
# "Add to watchlist" at the top of a preview: live, or not.
SUBMIT_LIVE = 'class="btn btn-primary watch-pv-add" type="submit">'
SUBMIT_OFF = 'class="btn btn-primary watch-pv-add" type="submit" disabled>'

GOOGLE_NEWS = b"""<rss version="2.0"><channel><title>Search</title>
<item><title>Tata Motors shares jump 4% after JLR sales beat - Mint</title>
<link>https://news.google.com/rss/articles/1</link><source url="https://livemint.com">Mint</source>
<pubDate>Thu, 15 Oct 2026 05:00:00 GMT</pubDate></item>
<item><title>Tata Motors, M&amp;M, Maruti: top auto picks - Moneycontrol</title>
<link>https://news.google.com/rss/articles/2</link><source url="https://moneycontrol.com">Moneycontrol</source>
<pubDate>Wed, 14 Oct 2026 05:00:00 GMT</pubDate></item>
</channel></rss>"""


def exchanges(
    nse: bytes = NSE_CSV,
    sme: bytes = SME_CSV,
    bse: list[dict[str, object]] | None = None,
    refuse: tuple[str, ...] = (),
    seen: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """NSE's two files and BSE's API, answering as they do; `refuse` names the ones that
    answer 403 the way NSE and BSE refuse a request they don't like."""
    rows = BSE_ROWS if bse is None else bse

    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.host == "api.bseindia.com":
            source = "bse"
        elif request.url.path.endswith("SME_EQUITY_L.csv"):
            source = "nse_sme"
        else:
            source = "nse"
        if source in refuse:
            return httpx.Response(403, text="<html><h1>Access Denied</h1></html>")
        if source == "bse":
            return httpx.Response(200, json=rows)
        return httpx.Response(200, content=nse if source == "nse" else sme)

    return httpx.MockTransport(answer)


def download(
    path: Path, now: datetime = NOW, pead_master: Path | None = None, **answers: object
) -> str:
    return companies.refresh(
        path,
        "test",
        transport=exchanges(**answers),  # type: ignore[arg-type]
        now=now,
        pead_master=pead_master,
    )


@pytest.fixture
def lists_path(tmp_path: Path) -> Path:
    path = tmp_path / "companies.json"
    download(path)
    return path


@pytest.fixture
def listed(lists_path: Path) -> list[companies.Company]:
    return companies.load_companies(lists_path)


def company(listed: list[companies.Company], wanted: str) -> companies.Company:
    found = companies.find(listed, wanted)
    assert found is not None, wanted
    return found


@pytest.fixture
def watchlist_copy(tmp_path: Path) -> Path:
    path = tmp_path / "config" / "watchlist.yaml"
    path.parent.mkdir()
    shutil.copy(CONFIG_DIR / "watchlist.yaml", path)
    return path


# ---------------------------------------------------------------- reading the lists


def test_nse_s_two_lists_are_read_as_nse_writes_them() -> None:
    main = companies.parse_nse_csv(NSE_CSV)
    assert len(main) == 11
    assert main[4] == {
        "symbol": "BDL",
        "name": "Bharat Dynamics Limited",
        "series": "EQ",
        "listed": "23-MAR-2018",
        "isin": "INE171Z01026",
    }
    sme = companies.parse_nse_csv(SME_CSV)  # underscores and a trailing comma
    assert [row["symbol"] for row in sme] == ["SMEDEMO", "SMEOLD"]
    assert sme[0]["series"] == "SM" and sme[0]["listed"] == "14-OCT-26"
    with pytest.raises(ValueError, match="not an NSE equity list"):
        companies.parse_nse_csv(b"<html><body>Please wait</body></html>")
    with pytest.raises(ValueError, match="no companies"):
        companies.parse_nse_csv(NSE_CSV.splitlines()[0] + b"\n")


def test_bse_s_list_is_read_without_its_suspended_codes() -> None:
    rows = companies.parse_bse_json(BSE_ROWS)
    assert rows[0] == {
        "code": "500002",
        "id": "ABB",
        "name": "ABB India Limited",  # the issuer's name, not the abbreviated scrip name
        "isin": "INE117A01022",
    }
    assert "999999" not in [row["code"] for row in rows] and len(rows) == 8
    assert companies.parse_bse_json({"Table": BSE_ROWS}) == rows  # the other shape BSE uses
    with pytest.raises(ValueError, match="no companies"):
        companies.parse_bse_json({"Table": []})


def test_a_company_on_both_exchanges_is_one_company(listed: list[companies.Company]) -> None:
    # 11 on NSE's main board, 2 on SME, and ABB only on BSE; the other seven BSE codes are
    # NSE companies too.
    assert len(listed) == 14
    bdl = company(listed, "BDL")
    assert bdl.name == "Bharat Dynamics Limited" and bdl.exchanges == ["NSE", "BSE"]
    assert bdl.bse_code == "541143" and bdl.isin == "INE171Z01026" and bdl.bse_isin is None
    assert bdl.listed == date(2018, 3, 23)
    assert bdl.yahoo_candidates == ["BDL.NS", "BDL.BO", "541143.BO"]
    abb = company(listed, "ABB")
    assert abb.exchanges == ["BSE"] and abb.symbol == "ABB"
    assert abb.yahoo_candidates == ["ABB.BO", "500002.BO"]  # BSE's scrip id, then its code
    sme = company(listed, "SMEDEMO")
    assert sme.sme and sme.listed == date(2026, 10, 14) and sme.yahoo_symbol == "SMEDEMO.NS"
    # Found by ISIN, BSE code, BSE ticker in either form, or NSE symbol.
    for wanted in ("INE066F01020", "541154", "541154.BO", "HAL.BO", "HAL.NS", "hal"):
        assert company(listed, wanted).nse_symbol == "HAL", wanted
    assert companies.find(listed, "NOTREAL") is None


def test_a_split_one_exchange_has_not_caught_up_with_is_still_one_company() -> None:
    """360 ONE on 2026-10-16: NSE still had the ISIN from before its split, BSE the new one.
    GATECH and its DVR share an issuer, so a third ISIN of theirs (made up) is left alone."""
    nse = [
        {"symbol": "360ONE", "name": "360 ONE WAM LIMITED", "series": "EQ", "isin": "INE466L01020"},
        {"symbol": "GATECH", "name": "GACM Technologies Limited", "isin": "INE224E01028"},
        {"symbol": "GATECHDVR", "name": "GACM Technologies Limited", "isin": "INE224E01036"},
    ]
    bse = [
        {"code": "542772", "id": "360ONE", "name": "360 ONE WAM Ltd", "isin": "INE466L01038"},
        {"code": "531723", "id": "GATECH", "name": "GACM Technologies Ltd", "isin": "INE224E01044"},
    ]
    merged = companies.merge({"nse": nse, "bse": bse}, {})
    assert len(merged) == 4
    one = company(merged, "360ONE")
    assert (one.isin, one.bse_isin, one.bse_code) == ("INE466L01020", "INE466L01038", "542772")
    assert company(merged, "INE466L01038") == one
    # BSE filings come keyed by BSE's ISIN, so that is the one a watchlist entry takes.
    assert companies.draft(one).isin == "INE466L01038"
    assert company(merged, "531723").nse_symbol is None  # not guessed onto either


def test_a_rights_entitlement_is_not_offered() -> None:
    """NATCO-RE, on NSE's list of 2026-10-17 and BSE's as 751145 (its scrip id here made up):
    it trades for a few days, then is gone."""
    row = companies.parse_nse_csv(
        NSE_CSV.splitlines()[0] + b"\n"
        b"NATCO-RE,Natco Pharma Limited-RE,BE,12-OCT-2026,2,1,INE987B20018,2\n"
    )
    bse = [{"code": "751145", "id": "NATCORE", "name": "Natco Pharma Ltd", "isin": "INE987B20018"}]
    assert companies.merge({"nse": row, "bse": bse}, {}) == []


def test_type_ahead_finds_by_name_symbol_or_bse_code(listed: list[companies.Company]) -> None:
    def symbols(query: str) -> list[str]:
        return [c.symbol for c in companies.search(listed, query)]

    assert symbols("BDL")[:1] == ["BDL"]
    assert "BDL" in symbols("dynam")  # any word of the name
    assert symbols("541154") == ["HAL"]  # a BSE code
    assert symbols("tata") == ["TMPV"]
    assert symbols("abb")[:1] == ["ABB"]
    assert symbols("INE0IX101010") == ["DATAPATTNS"]
    assert symbols("t") == []  # one letter is not a search
    assert symbols("zzzz") == []


def test_a_draft_takes_the_name_and_judges_the_symbol(listed: list[companies.Company]) -> None:
    bdl = companies.draft(company(listed, "BDL"))
    assert bdl.name == "Bharat Dynamics" and bdl.symbol == "BDL.NS"
    assert bdl.aliases.strong == ["Bharat Dynamics"] and bdl.aliases.weak == ["BDL"]
    assert (bdl.nse_name, bdl.isin, bdl.bse_code) == (
        "Bharat Dynamics Limited",
        "INE171Z01026",
        "541143",
    )
    pfocus = companies.draft(company(listed, "PFOCUS"))
    assert pfocus.aliases.strong == ["Prime Focus", "PFOCUS"] and pfocus.aliases.weak == []
    assert companies.draft(company(listed, "360ONE")).name == "360 One Wam"
    assert companies.parse_aliases(" Tata Motors, , TML ,Tata Motors") == ["Tata Motors", "TML"]


def test_a_company_only_on_bse_is_drafted_as_a_bse_ticker(
    listed: list[companies.Company],
) -> None:
    abb = company(listed, "ABB")
    item = companies.draft(abb)
    assert item.symbol == "ABB.BO" and item.nse_symbol is None and item.nse_name is None
    assert item.bse_code == "500002" and item.isin == "INE117A01022"
    assert item.aliases.strong == ["ABB India"] and item.aliases.weak == ["ABB"]
    # The ticker Yahoo answered to in the preview, if it is one of the company's own.
    assert companies.draft(abb, yahoo="500002.BO").symbol == "500002.BO"
    assert companies.draft(abb, yahoo="ABB.NS").symbol == "ABB.BO"
    assert company_label(item) == "ABB India (BSE: 500002)"  # what the model is told
    with pytest.raises(ValueError, match="not a BSE scrip code"):
        WatchItem.model_validate({**item.model_dump(), "bse_code": "ABB"})


# ---------------------------------------------------------------- keeping them fresh


def test_each_list_keeps_its_last_good_copy(tmp_path: Path) -> None:
    path = tmp_path / "companies.json"
    seen: list[httpx.Request] = []
    assert download(path, seen=seen) == "company lists: NSE 11, NSE SME 2, BSE 8"
    bse = next(r for r in seen if r.url.host == "api.bseindia.com")
    assert bse.headers["Origin"] == "https://www.bseindia.com"  # BSE refuses without it
    assert "application/json" in bse.headers["Accept"]

    line = download(path, NOW + timedelta(days=1), refuse=("bse",), nse=b"<html>wait</html>")
    assert "NSE failed (ValueError: not an NSE equity list" in line
    assert "BSE failed (RuntimeError: refused (HTTP 403))" in line and "NSE SME 2" in line
    found, status = companies.load(path)
    assert len(found) == 14  # nothing lost
    by_source = {s.source: s for s in status}
    assert by_source["bse"].count == 8 and "refused" in (by_source["bse"].error or "")
    assert by_source["bse"].fetched_at == NOW  # the last good download
    assert by_source["nse_sme"].error is None and by_source["nse_sme"].label == "NSE SME"


def test_bse_s_list_is_fetched_the_way_the_pead_tool_fetches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BSE answered httpx 403 on 2026-10-17 where the PEAD tool's `requests` call had worked
    the evening before: the list is fetched with `requests`, with the PEAD tool's headers."""
    import requests

    asked: list[tuple[str, dict[str, str], float]] = []

    class Answer:
        status_code = 200
        content = json.dumps(BSE_ROWS).encode()

    def get(url: str, headers: dict[str, str], timeout: float) -> Answer:
        asked.append((url, headers, timeout))
        return Answer()

    monkeypatch.setattr(requests, "get", get)
    path = tmp_path / "companies.json"
    assert companies.refresh(path, "test", sources=["bse"]) == "company lists: BSE 8"
    assert asked == [(companies.BSE_LIST_URL, companies.BSE_HEADERS, companies.TIMEOUT)]
    refused = companies.refresh(
        path, "test", sources=["bse"], bse_get=lambda url, headers, timeout: (403, b"")
    )
    assert refused == "company lists: BSE failed (RuntimeError: refused (HTTP 403))"


def test_when_bse_refuses_the_pead_tool_s_call_it_is_asked_as_a_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-17 16:50: BSE refused the PEAD tool's own call from this laptop too."""
    import requests
    from curl_cffi import requests as browser

    class Refused:
        status_code = 403
        content = b"<html><h1>Access Denied</h1></html>"

    class Answer:
        status_code = 200
        content = json.dumps(BSE_ROWS).encode()

    asked: list[str] = []

    class Session:
        def __init__(self, impersonate: str) -> None:
            asked.append(f"impersonate {impersonate}")

        def __enter__(self) -> "Session":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def get(self, url: str, headers: dict[str, str] | None = None, **_: object) -> Answer:
            asked.append(url)
            if url == companies.BSE_LIST_URL:
                assert headers is not None and "User-Agent" not in headers  # Chrome's own
            return Answer()

    monkeypatch.setattr(requests, "get", lambda *a, **k: Refused())
    monkeypatch.setattr(browser, "Session", Session)
    path = tmp_path / "companies.json"
    assert companies.refresh(path, "test", sources=["bse"]) == "company lists: BSE 8"
    assert asked == ["impersonate chrome", "https://www.bseindia.com/", companies.BSE_LIST_URL]


PEAD_MASTER = {
    "bse": {"541154": "INE066F01020", "541143": "INE171Z01026", "500002": "INE117A01022"},
    "nse": {"HAL": "INE066F01020"},
    "updated": "2026-10-16",
}


def test_while_bse_refuses_its_codes_come_from_the_pead_tool_s_copy(tmp_path: Path) -> None:
    """The PEAD tool's scrip master has BSE's codes and ISINs but no names: NSE companies get
    their BSE code; a company only on BSE (ABB here) waits for BSE's own list."""
    master = tmp_path / "scrip_master.json"
    master.write_text(json.dumps(PEAD_MASTER), encoding="utf-8")
    path = tmp_path / "companies.json"
    line = download(path, refuse=("bse",), pead_master=master)
    assert "BSE failed (RuntimeError: refused (HTTP 403))" in line
    assert "BSE codes from the PEAD tool's copy 3" in line
    listed = companies.load_companies(path)
    assert company(listed, "HAL").bse_code == "541154" and company(listed, "BDL").bse_code
    assert companies.find(listed, "500002") is None  # no name, so not offered
    status = {s.source: s for s in companies.load(path)[1]}["bse"]
    assert status.via == "pead" and status.fetched_at is None  # still due: BSE is asked again
    assert companies.due(path, NOW + timedelta(hours=2)) == ["bse"]
    # BSE answers: its own list replaces the copy, and nothing is dated new for the change.
    download(path, NOW + timedelta(hours=2), pead_master=master)
    status = {s.source: s for s in companies.load(path)[1]}["bse"]
    assert status.via is None and status.count == 8 and status.error is None
    assert company(companies.load_companies(path), "500002").name == "ABB India Limited"
    assert "BSE:541154" not in companies.read_file(path).first_seen
    assert companies.pead_master_path("C:/x/announcements.db") == Path("C:/x/scrip_master.json")
    assert companies.pead_master_path(None) is None


def test_the_lists_are_downloaded_once_a_day_and_rest_after_a_failure(tmp_path: Path) -> None:
    path = tmp_path / "companies.json"
    assert companies.due(path, NOW) == ["nse", "nse_sme", "bse"]
    download(path)
    assert companies.due(path, NOW + timedelta(hours=19)) == []
    later = NOW + timedelta(hours=21)
    assert companies.due(path, later) == ["nse", "nse_sme", "bse"]
    download(path, later, refuse=("bse",))
    assert companies.due(path, later + timedelta(hours=1)) == []  # BSE rests
    assert companies.due(path, later + timedelta(hours=2)) == ["bse"]
    assert companies.refresh(path, "test", sources=[]) == "company lists: nothing due"


def test_a_new_listing_is_offered_before_anything_is_typed(tmp_path: Path) -> None:
    path = tmp_path / "companies.json"
    without = [r for r in BSE_ROWS if r["SCRIP_CD"] not in ("500002", "544898")]
    download(path, NOW - timedelta(days=2), bse=without)
    # The first download dates nothing: BASIS hadn't looked before, so nothing is new.
    assert companies.read_file(path).first_seen == {}
    download(path)
    assert companies.read_file(path).first_seen == {
        "BSE:500002": "2026-10-16",
        "BSE:544898": "2026-10-16",
    }
    listed = companies.load_companies(path)
    abb = company(listed, "ABB")
    assert abb.first_seen == TODAY and abb.added_on == TODAY and abb.is_new(TODAY)
    # ESDS is new to BSE's list here but NSE dates its listing (4 Sep): that date wins.
    esds = company(listed, "ESDS")
    assert esds.added_on == date(2026, 9, 4) and not esds.is_new(TODAY)
    assert [c.symbol for c in companies.recent(listed, TODAY)] == ["ABB", "SMEDEMO"]
    assert companies.recent(listed, TODAY + timedelta(days=60)) == []


def test_the_nse_list_of_the_first_version_is_used_until_the_new_file_exists(
    tmp_path: Path,
) -> None:
    path = tmp_path / "companies.json"
    lines = NSE_CSV.decode().splitlines()
    (tmp_path / "nse_equity_list.csv").write_text(
        "\n".join(line for line in lines if not line.startswith("ESDS,")), encoding="utf-8"
    )
    found, status = companies.load(path)  # before any download
    assert len(found) == 10 and [s.source for s in status] == ["nse"]
    download(path)
    # Seeded from the old list, so only what it lacked counts as new to NSE's.
    assert companies.read_file(path).first_seen == {"NSE:ESDS": "2026-10-16"}


def test_the_web_server_downloads_the_lists_in_the_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    calls: list[list[str]] = []
    real = companies.refresh

    def slow(path: Path, user_agent: str, **options: object) -> str:
        calls.append(list(options["sources"]))  # type: ignore[call-overload]
        gate.wait(5)
        return real(path, user_agent, transport=exchanges(), lock_dir=options["lock_dir"])  # type: ignore[arg-type]

    monkeypatch.setattr(companies, "refresh", slow)
    path = tmp_path / "companies.json"
    lists = companies.ListRefresher(path, tmp_path / "locks", "test", enabled=True)
    assert lists.kick() is True
    assert lists.kick() is True  # still running: not started twice
    gate.set()
    assert lists._thread is not None
    lists._thread.join(5)
    assert calls == [["nse", "nse_sme", "bse"]]
    assert lists.last_line == "company lists: NSE 11, NSE SME 2, BSE 8"
    assert lists.kick() is False  # nothing due
    assert lists.kick(force=True) is True  # the page's "Try again"
    lists._thread.join(5)
    assert len(calls) == 2
    off = companies.ListRefresher(tmp_path / "x.json", tmp_path / "locks", "test", enabled=False)
    assert off.kick() is False


# ---------------------------------------------------------------- BSE filings


def test_bse_filings_are_matched_on_what_the_lists_say_today(
    listed: list[companies.Company],
) -> None:
    """HAL's watchlist ISIN until 2026-10-17 was the one from before its split; the PEAD tool
    keys BSE filings by BSE's current ISIN and code, so those are matched as well."""
    aliases = WatchAliases(strong=["Hindustan Aeronautics"])
    stale = WatchItem(
        type="stock", symbol="HAL.NS", name="HAL", isin="INE066F01012", aliases=aliases
    )
    bse_only = companies.draft(company(listed, "ABB"), yahoo="500002.BO")
    unlisted = WatchItem(
        type="stock", symbol="XYZ.NS", name="Xyz", aliases=WatchAliases(strong=["Xyz"])
    )
    isins, codes = companies.filing_keys([stale, bse_only, unlisted], listed)
    assert isins == {
        "INE066F01012": "HAL.NS",
        "INE066F01020": "HAL.NS",
        "INE117A01022": "500002.BO",
    }
    assert codes == {"541154": "HAL.NS", "500002": "500002.BO"}
    assert companies.filing_keys([stale], []) == ({"INE066F01012": "HAL.NS"}, {})


# ---------------------------------------------------------------- the file


def test_adding_then_removing_leaves_the_file_as_it_was(
    watchlist_copy: Path, listed: list[companies.Company], tmp_path: Path
) -> None:
    original = watchlist_copy.read_text(encoding="utf-8")
    item = companies.draft(company(listed, "TMPV"))
    backups = tmp_path / "backups"
    added = watchlist_edit.add_stock(
        watchlist_copy, item, watchlist_edit.version_of(original), NOW, backup_dir=backups
    )
    symbols = [e.symbol for e in added.watchlist]
    # After the last stock, before the commodities.
    assert symbols.index("TMPV.NS") == symbols.index("ASTRAMICRO.NS") + 1
    text = watchlist_copy.read_text(encoding="utf-8")
    assert "# Added from the BASIS page on 2026-10-16" in text
    assert '# Never "Astra" alone' in text  # every comment kept
    entry = load_watchlist_file(watchlist_copy).stocks[-1]
    assert entry.aliases.strong == ["Tata Motors Passenger Vehicles"]
    assert entry.aliases.weak == ["TMPV"] and entry.bse_code == "500570"
    watchlist_edit.remove_entry(
        watchlist_copy,
        "TMPV.NS",
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
    item = companies.draft(company(listed, "TMPV"))
    with pytest.raises(watchlist_edit.EditRefused, match="changed since this page loaded"):
        watchlist_edit.add_stock(watchlist_copy, item, stale, NOW, backup_dir=watchlist_copy.parent)
    current = watchlist_edit.version_of(watchlist_copy.read_text(encoding="utf-8"))
    bdl = companies.draft(company(listed, "BDL"))
    with pytest.raises(watchlist_edit.EditRefused, match="already on the watchlist"):
        watchlist_edit.add_stock(
            watchlist_copy, bdl, current, NOW, backup_dir=watchlist_copy.parent
        )


# ---------------------------------------------------------------- the page


def yahoo(symbol: str) -> PriceSample:
    """Yahoo as the page meets it: ABB under its BSE code only, everything else by name.
    Yesterday's close, from the real clock the page checks it against."""
    if symbol == "ABB.BO":
        raise NoPriceData("No data found, symbol may be delisted")
    exchange = "BSE" if symbol.endswith(".BO") else "NSI"
    return PriceSample(
        [(datetime.now(UTC) - timedelta(hours=20), 712.5)],
        {"exchangeName": exchange, "currency": "INR"},
    )


def make_page(
    tmp_path: Path, settings: Settings, watchlist_copy: Path, lists_path: Path
) -> TestClient:
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
    app = create_app(
        settings,
        make_read_only_session_factory(make_read_only_engine(database)),
        watchlist_path=watchlist_copy,
        companies_path=lists_path,
        backup_dir=tmp_path / "backups",
        allowed_hosts=frozenset({"testserver"}),
        preview_transport=httpx.MockTransport(lambda r: httpx.Response(200, content=GOOGLE_NEWS)),
        ticker_fetch=yahoo,
    )
    return TestClient(app)


@pytest.fixture
def page(tmp_path: Path, settings: Settings, watchlist_copy: Path, lists_path: Path) -> TestClient:
    return make_page(tmp_path, settings, watchlist_copy, lists_path)


class FakeLists:
    """Stands in for the web server's background download."""

    def __init__(self, running: bool, enabled: bool = True) -> None:
        self.running, self.enabled, self.forced = running, enabled, False

    def kick(self, force: bool = False) -> bool:
        self.forced = self.forced or force
        return self.running


def _form(client: TestClient) -> dict[str, str]:
    """The token and file version the page hands its forms."""
    body = client.get("/watchlist").text
    token = body.split('name="token" value="')[1].split('"')[0]
    version = body.split('name="version" value="')[1].split('"')[0]
    return {"token": token, "version": version}


def test_the_page_has_an_add_button_a_search_box_and_a_remove_on_every_card(
    page: TestClient,
) -> None:
    body = page.get("/watchlist").text
    assert '<a class="btn btn-primary" href="#watch-add">Add a stock</a>' in body
    assert body.count('action="/watchlist/remove"') == 12  # 8 stocks and 4 commodities
    assert 'id="watch-search"' in body and 'hx-get="/watchlist/companies"' in body
    assert 'class="screen screen-wide"' in body  # the full window, not a column
    # "Add a stock" is a closed dialog: the search, the empty preview, the wait a preview
    # shows while it is asked for, and the footer's lists state. "Add to watchlist" comes
    # with a preview, so there is none yet.
    assert '<section id="watch-add" class="watch-add"' in body and 'role="dialog"' in body
    assert "Preview a company" in body and "data-watch-close" in body
    assert 'id="watch-preview-wait" class="watch-wait htmx-indicator"' in body
    assert "Searching Google News over the last 30 days" in body
    assert "NSE 11 · NSE SME 2 · BSE 8" in body
    assert "watch-pv-add" not in body
    assert "js/watch-add.js" in body


def test_the_search_box_is_there_before_the_lists_are(
    tmp_path: Path, settings: Settings, watchlist_copy: Path
) -> None:
    """The first complaint (2026-10-17): with no list yet there was nothing to type in."""
    client = make_page(tmp_path, settings, watchlist_copy, tmp_path / "none.json")
    assert 'id="watch-search"' in client.get("/watchlist").text
    lists = FakeLists(running=True)
    client.app.state.lists = lists  # type: ignore[attr-defined]
    body = client.get("/watchlist/companies", params={"q": "mazdock"}).text
    assert "Downloading the NSE and BSE company lists" in body
    assert 'hx-include="#watch-search" hx-trigger="load delay:1500ms"' in body  # asks again
    client.app.state.lists = FakeLists(running=False, enabled=False)  # type: ignore[attr-defined]
    assert "uv run newsdesk companies --refresh" in client.get("/watchlist/companies").text


def test_a_failed_download_says_why_and_offers_to_try_again(
    tmp_path: Path, settings: Settings, watchlist_copy: Path
) -> None:
    path = tmp_path / "companies.json"
    download(path, refuse=("nse", "nse_sme", "bse"))
    client = make_page(tmp_path, settings, watchlist_copy, path)
    lists = FakeLists(running=False)
    client.app.state.lists = lists  # type: ignore[attr-defined]
    body = client.get("/watchlist/companies", params={"q": "hal"}).text
    assert "The company lists couldn't be downloaded." in body
    assert "<li>BSE: RuntimeError: refused (HTTP 403)</li>" in body
    assert "retry=1" in body and "Try again</button>" in body
    assert "BSE download failed: HTTP 403" in body  # the footer, in a few words
    client.get("/watchlist/companies", params={"retry": "1"})
    assert lists.forced


def test_suggestions_come_as_you_type(page: TestClient) -> None:
    body = page.get("/watchlist/companies", params={"q": "dynamics"}).text
    assert "Bharat Dynamics" in body and "on the watchlist" in body
    assert "<span>NSE BDL</span><span>BSE 541143</span>" in body
    assert "Tata Motors Passenger Vehicles" in page.get("/watchlist/companies?q=tata").text
    assert "Hindustan Aeronautics" in page.get("/watchlist/companies?q=541154").text
    abb = page.get("/watchlist/companies", params={"q": "abb"}).text
    assert "<span>BSE 500002 · ABB</span>" in abb and "NSE ABB" not in abb
    assert (
        "No company listed on NSE or BSE matches"
        in page.get("/watchlist/companies", params={"q": "zzzz"}).text
    )
    footer = page.get("/watchlist/companies", params={"q": "zzzz"}).text
    # The dialog footer's lists state comes along with every answer.
    assert 'id="watch-lists" class="watch-lists" hx-swap-oob="true"' in footer
    assert "NSE 11 · NSE SME 2 · BSE 8" in footer and "new listings appear within a day" in footer


def test_with_nothing_typed_the_newest_listings_are_offered(
    tmp_path: Path, settings: Settings, watchlist_copy: Path
) -> None:
    """Against the real clock, as the page is: one SME company listed three days ago."""
    listed_on = datetime.now(companies.INDIA).date() - timedelta(days=3)
    sme = SME_CSV.replace(b"14-OCT-26", listed_on.strftime("%d-%b-%y").upper().encode())
    path = tmp_path / "companies.json"
    download(path, sme=sme)
    client = make_page(tmp_path, settings, watchlist_copy, path)
    body = client.get("/watchlist/companies").text
    assert "New listings · last 30 days" in body
    assert "Sme Demo Industries" in body
    assert f"New · listed {listed_on.strftime('%d %b')}" in body
    assert "Old Sme Demo" not in body and "Bharat Dynamics" not in body


def test_the_preview_checks_yahoo_and_shows_keeps_and_drops(page: TestClient) -> None:
    first = page.get("/watchlist/preview", params={"company": "INE155A01022"}).text
    assert 'value="Tata Motors Passenger Vehicles"' in first and 'value="TMPV"' in first
    assert "<span>NSE · TMPV</span><span>BSE · 500570</span><span>INE155A01022</span>" in first
    # The tabs, Overview first; the names to edit are under Key Info, in the same form.
    assert 'data-tab="overview" aria-selected="true"' in first
    assert 'data-panel="info" role="tabpanel" hidden' in first and 'id="watch-name"' in first
    assert "No price history from Yahoo." in first  # a stand-in Yahoo: no profile asked
    # "Preview again" with the names edited.
    body = page.get(
        "/watchlist/preview",
        params={"company": "INE155A01022", "strong": "Tata Motors, TMPV", "weak": ""},
    ).text
    assert "TMPV.NS on Yahoo: last close 712.50 INR (NSI)" in body
    assert 'name="yahoo" value="TMPV.NS"' in body
    assert 'value="Tata Motors, TMPV"' in body  # editable before adding
    assert "Tata Motors shares jump 4% after JLR sales beat" in body  # Google News: a keep
    assert "Tata Motors to cut EV prices" in body  # a stored headline
    assert "top auto picks" in body  # a mention
    assert "<b>2</b> keep" in body and "<b>1</b> mention" in body
    assert SUBMIT_LIVE in body and 'id="watch-add-form"' in body
    assert page.get("/watchlist/preview", params={"company": "NOTREAL"}).status_code == 404


def test_a_bse_only_company_is_previewed_under_the_ticker_yahoo_knows(page: TestClient) -> None:
    body = page.get("/watchlist/preview", params={"company": "INE117A01022"}).text
    assert "<span>BSE · 500002</span><span>INE117A01022</span>" in body and "NSE ·" not in body
    assert "500002.BO on Yahoo: last close 712.50 INR (BSE)" in body  # ABB.BO had nothing
    assert 'name="yahoo" value="500002.BO"' in body


def test_a_company_yahoo_does_not_know_cannot_be_added(page: TestClient) -> None:
    def nothing(symbol: str) -> PriceSample:
        raise NoPriceData("none")

    page.app.state.ticker_fetch = nothing  # type: ignore[attr-defined]
    body = page.get("/watchlist/preview", params={"company": "INE117A01022"}).text
    assert "Yahoo has no recent prices under ABB.BO or 500002.BO" in body
    assert 'name="yahoo" value=""' in body and SUBMIT_OFF in body
    assert "Yahoo has no prices for it, so its moves couldn&#39;t be followed." in body


def test_an_already_listed_company_says_so_where_add_would_be(page: TestClient) -> None:
    body = page.get("/watchlist/preview", params={"company": "INE171Z01026"}).text
    assert SUBMIT_OFF in body
    assert "Already on the watchlist as Bharat Dynamics." in body


def test_the_preview_shows_the_company_as_yahoo_describes_it(
    tmp_path: Path, settings: Settings, watchlist_copy: Path, lists_path: Path
) -> None:
    """The user's design: price and day's move by the Add button; three months' line, key
    figures and what the company does on Overview; the year and its returns on Price History.
    Every figure here is made up for the test."""
    start = datetime(2025, 10, 17, 10, 0, tzinfo=UTC)
    closes = [(start + timedelta(days=d), 100.0 + d / 10) for d in range(366)]
    closes[-1] = (closes[-1][0], closes[-2][1] * 1.001)

    def profile(symbol: str) -> preview.Profile:
        assert symbol == "TMPV.NS"
        return preview.Profile(
            closes=closes,
            currency="INR",
            sector="Consumer Cyclical",
            industry="Auto Manufacturers",
            market_cap=2.5e12,
            summary="Makes passenger cars and electric vehicles. " * 12,
        )

    client = make_page(tmp_path, settings, watchlist_copy, lists_path)
    client.app.state.profile_fetch = profile  # type: ignore[attr-defined]
    body = client.get("/watchlist/preview", params={"company": "INE155A01022"}).text
    assert "<b>₹136.54</b>" in body and ">+0.1%</span>" in body  # last close, the day's move
    assert "<span>Consumer Cyclical</span><span>₹2,50,000 Cr</span>" not in body
    assert "<span>Consumer Cyclical</span><span>₹250,000 Cr</span>" in body
    assert "<dd>₹100.00 – ₹136.54</dd>" in body  # the year's range
    assert "<dd>Auto Manufacturers</dd>" in body
    assert "About the company" in body and "Makes passenger cars" in body and "…" in body
    assert 'aria-label="Daily closes over the last three months"' in body
    assert 'aria-label="Daily closes over the last year"' in body
    assert "<dt>1 year</dt>" in body and "<dt>1 week</dt>" in body
    assert SUBMIT_LIVE in body


def test_a_profile_s_figures_are_written_out() -> None:
    assert preview.money(712.5, "INR") == "₹712.50" and preview.money(12345.6, "INR") == "₹12,346"
    assert preview.market_cap(3.482e10, "INR") == "₹3,482 Cr"
    assert preview.market_cap(None, "INR") is None
    assert preview.chart_points([1.0, 2.0], 100, 50, 0) == "0.0,50.0 100.0,0.0"
    assert preview.chart_points([1.0], 100, 50) == ""
    empty = preview.facts(preview.Profile())
    assert empty.price is None and empty.returns == [] and empty.overview == []


def test_adding_from_the_page_writes_the_file(page: TestClient, watchlist_copy: Path) -> None:
    form = _form(page) | {
        "company": "INE155A01022",
        "yahoo": "TMPV.NS",
        "name": "Tata Motors",
        "strong": "Tata Motors, TMPV",
        "weak": "TML",
        "group": "",
    }
    response = page.post(
        "/watchlist/add", data=form, headers={"Origin": ORIGIN}, follow_redirects=False
    )
    assert response.headers["location"] == "/watchlist?added=Tata%20Motors"  # dialog closed
    entry = load_watchlist_file(watchlist_copy).stocks[-1]
    assert entry.symbol == "TMPV.NS" and entry.aliases.weak == ["TML"]
    assert entry.isin == "INE155A01022" and entry.bse_code == "500570"
    assert "Added Tata Motors" in page.get("/watchlist", params={"added": "Tata Motors"}).text
    assert "Tata Motors" in page.get("/watchlist").text  # the card, without a restart


def test_adding_a_bse_only_stock_writes_its_bse_ticker(
    page: TestClient, watchlist_copy: Path
) -> None:
    form = _form(page) | {
        "company": "INE117A01022",
        "yahoo": "500002.BO",
        "name": "ABB India",
        "strong": "ABB India",
        "weak": "ABB",
    }
    response = page.post(
        "/watchlist/add", data=form, headers={"Origin": ORIGIN}, follow_redirects=False
    )
    assert response.status_code == 303 and "added=ABB%20India" in response.headers["location"]
    entry = load_watchlist_file(watchlist_copy).stocks[-1]
    assert entry.symbol == "500002.BO" and entry.bse_code == "500002"
    assert entry.nse_symbol is None and entry.nse_name is None
    assert '    bse_code: "500002"' in watchlist_copy.read_text(encoding="utf-8")
    body = page.get("/watchlist").text
    card = body[body.index('<span class="kicker">BSE</span>') :]
    assert '<span class="kicker">BSE</span>' in body and "ABB India" in card[:400]


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"yahoo": ""}, "preview the company first"),  # Yahoo not checked
        ({"yahoo": "HAL.NS"}, "preview the company first"),  # not this company's ticker
        ({"company": "NOTREAL"}, "not in the NSE or BSE company lists"),
        ({"company": "INE171Z01026", "yahoo": "BDL.NS"}, "already on the watchlist"),
    ],
    ids=["unchecked", "other-ticker", "unknown", "already-there"],
)
def test_an_add_the_preview_did_not_vouch_for_is_refused(
    page: TestClient, watchlist_copy: Path, change: dict[str, str], error: str
) -> None:
    before = watchlist_copy.read_text(encoding="utf-8")
    form = _form(page) | {"company": "INE155A01022", "yahoo": "TMPV.NS", "strong": "Tata"}
    response = page.post(
        "/watchlist/add",
        data=form | change,
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    location = response.headers["location"]
    assert response.status_code == 303 and location.startswith("/watchlist?add_error=")
    assert location.endswith("#watch-add")
    body = page.get(location).text
    assert error in body and 'class="watch-add open"' in body  # the dialog reopens, says why
    assert watchlist_copy.read_text(encoding="utf-8") == before


def test_removing_from_the_page_writes_the_file(page: TestClient, watchlist_copy: Path) -> None:
    form = _form(page) | {"symbol": "GC=F"}
    response = page.post(
        "/watchlist/remove", data=form, headers={"Origin": ORIGIN}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "GC=F" not in [e.symbol for e in load_watchlist_file(watchlist_copy).watchlist]
    assert response.headers["location"] == "/watchlist?removed=Gold"  # said on the page
    body = page.get(response.headers["location"]).text
    assert '<p class="watch-page-flash" role="status">Removed Gold.</p>' in body
    assert '<section id="watch-add" class="watch-add"' in body  # the dialog stays shut


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
        companies_path=tmp_path / "companies.json",
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
    item = companies.draft(company(listed, "TMPV"))
    item = item.model_copy(update={"aliases": WatchAliases(strong=["Tata Motors"], weak=["TMPV"])})
    watchlist_edit.add_stock(
        watchlist_copy, item, watchlist_edit.version_of(text), NOW, backup_dir=tmp_path
    )
    assert watcher.refresh_watchlist() is True
    assert "TMPV.NS" in [s.symbol for s in watcher.stocks]
    assert [m.symbol for m in watcher.matcher.match("Tata Motors shares jump 4%")] == ["TMPV.NS"]
    assert any("Tata+Motors" in q.url for q in watcher.queries)
    # A broken file keeps the list in use.
    watchlist_copy.write_text("watchlist: [oops", encoding="utf-8")
    assert watcher.refresh_watchlist() is False
    assert "TMPV.NS" in [s.symbol for s in watcher.stocks]


def test_a_shut_market_shows_its_last_session_not_no_price(settings: Settings) -> None:
    """Saturday 17:00 IST, 2026-10-17: NSE's last bar was more than 24 hours old, and every
    card read "no price yet". The last session's move is shown instead (Prime Focus's real
    closes: 277.45 on Thursday, 290.30 on Friday)."""
    from app.models import PriceBar
    from app.web.watch_queries import watch_cards

    engine = make_engine(":memory:")
    init_db(engine)
    with make_session_factory(engine)() as session:
        for at, close in (
            (datetime(2026, 10, 15, 8, 45, tzinfo=UTC), 278.5),
            (datetime(2026, 10, 15, 9, 45, tzinfo=UTC), 277.45),
            (datetime(2026, 10, 16, 3, 45, tzinfo=UTC), 285.6),
            (datetime(2026, 10, 16, 9, 45, tzinfo=UTC), 290.3),
        ):
            session.add(
                PriceBar(
                    symbol="PFOCUS.NS",
                    interval="60m",
                    ts=at,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1,
                )
            )
        session.commit()
        watched = load_watchlist_file(CONFIG_DIR / "watchlist.yaml")
        saturday = datetime(2026, 10, 17, 11, 30, tzinfo=UTC)
        card = next(
            c
            for c in watch_cards(session, watched, {}, settings, saturday)
            if c.symbol == "PFOCUS.NS"
        )
    assert (card.price, card.change, card.change_label) == ("₹290.30", "+4.6%", "last")
