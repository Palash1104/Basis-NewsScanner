from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.assets import (
    NoPriceData,
    PriceSample,
    benchmark_for,
    check_ticker,
    markdown_report,
    record_checks,
    validate_assets,
    validation_gaps,
    validation_warning,
)
from app.cli import asset_warning, run_ticker_validation, ticker_report_lines
from app.config import AssetConfig, AssetsFile, Settings, load_assets
from app.db import init_db, make_engine, make_session_factory

NOW = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
ASSETS = {asset.symbol: asset for asset in load_assets()}

ONGC_META = {
    "longName": "Oil and Natural Gas Corporation Limited",
    "shortName": "OIL AND NATURAL GAS CORP.",
    "currency": "INR",
    "exchangeName": "NSI",
    "instrumentType": "EQUITY",
}


def _asset(**overrides: object) -> AssetConfig:
    values: dict[str, object] = {
        "symbol": "ONGC.NS",
        "name": "Oil and Natural Gas Corporation",
        "display_name": "ONGC",
        "type": "stock",
        "country": "India",
        "sector": "Energy",
    }
    return AssetConfig.model_validate(values | overrides)


def _sample(days_old: float = 1, meta: dict | None = None) -> PriceSample:
    bar = NOW - timedelta(days=days_old)
    return PriceSample([(bar - timedelta(days=1), 230.0), (bar, 232.8)], meta or ONGC_META)


class FakeYahoo:
    """Maps symbol -> list of outcomes (a PriceSample or an exception), one per call."""

    def __init__(self, outcomes: dict[str, list[object]]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def __call__(self, symbol: str) -> PriceSample:
        self.calls.append(symbol)
        outcome = self.outcomes[symbol].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


def _no_sleep(seconds: float) -> None:
    pass


# ---------------------------------------------------------------- assets.yaml


def test_universe_loads_with_one_valid_sector_each() -> None:
    assert len(ASSETS) == 82
    for symbol in ("BZ=F", "INR=X", "^NSEI", "M&M.NS", "TSM", "GLD"):
        assert symbol in ASSETS
    assert ASSETS["INR=X"].up_means == "rupee weaker"
    assert all(asset.exchange and asset.currency for asset in ASSETS.values())


@pytest.mark.parametrize(
    "change",
    [{"sector": "Automobiles"}, {"type": "crypto"}, {"colour": "red"}],
)
def test_asset_fields_are_validated(change: dict) -> None:
    with pytest.raises(ValidationError):
        _asset(**change)


def test_duplicate_symbols_are_rejected() -> None:
    entry = _asset().model_dump()
    with pytest.raises(ValidationError, match="duplicate asset symbols"):
        AssetsFile.model_validate({"assets": [entry, entry]})


# ---------------------------------------------------------------- one symbol


def test_ok_symbol_records_yahoo_metadata() -> None:
    result = check_ticker(_asset(), FakeYahoo({"ONGC.NS": [_sample()]}), NOW, sleep=_no_sleep)
    assert result.ok and result.rows == 2 and result.last_close == 232.8
    assert (result.currency, result.exchange, result.instrument_type) == ("INR", "NSI", "EQUITY")
    assert result.flags == []


def test_missing_symbol_is_retried_then_reported_empty() -> None:
    yahoo = FakeYahoo({"ONGC.NS": [NoPriceData("no data")] * 3})
    result = check_ticker(_asset(), yahoo, NOW, sleep=_no_sleep)
    assert result.status == "empty" and len(yahoo.calls) == 3
    assert "no data" in (result.error or "")


def test_transient_error_is_retried() -> None:
    yahoo = FakeYahoo({"ONGC.NS": [TimeoutError("slow"), _sample()]})
    assert check_ticker(_asset(), yahoo, NOW, sleep=_no_sleep).ok


def test_persistent_error_is_reported_not_raised() -> None:
    yahoo = FakeYahoo({"ONGC.NS": [ConnectionError("down")] * 3})
    result = check_ticker(_asset(), yahoo, NOW, sleep=_no_sleep)
    assert result.status == "error" and "ConnectionError" in (result.error or "")


def test_old_data_is_stale_and_no_closes_is_empty() -> None:
    stale = check_ticker(_asset(), FakeYahoo({"ONGC.NS": [_sample(10)]}), NOW, sleep=_no_sleep)
    assert stale.status == "stale" and "10 days old" in (stale.error or "")
    empty = PriceSample([], ONGC_META)
    result = check_ticker(_asset(), FakeYahoo({"ONGC.NS": [empty]}), NOW, sleep=_no_sleep)
    assert result.status == "empty"


def test_suspicious_metadata_is_flagged_for_review_not_failed() -> None:
    wrong = ONGC_META | {"longName": "Oilfield Supplies Inc", "shortName": "OILFIELD SUP"}
    wrong |= {"instrumentType": "ETF", "currency": "USD", "exchangeName": "NYQ"}
    asset = _asset(currency="INR", exchange="NSI")
    result = check_ticker(asset, FakeYahoo({"ONGC.NS": [_sample(meta=wrong)]}), NOW)
    assert result.ok
    assert [flag.split(":")[0] for flag in result.flags] == [
        "name",
        "type",
        "currency",
        "exchange",
    ]


def test_validate_assets_keeps_universe_order() -> None:
    first, second = _asset(), _asset(symbol="OIL.NS", name="Oil India", display_name="Oil India")
    yahoo = FakeYahoo({"ONGC.NS": [_sample()], "OIL.NS": [NoPriceData("gone")] * 3})
    results = validate_assets([first, second], yahoo, NOW, sleep=_no_sleep)
    assert [(r.symbol, r.status) for r in results] == [("ONGC.NS", "ok"), ("OIL.NS", "empty")]
    report = markdown_report(results, NOW)
    assert report.index("OIL.NS") < report.index("ONGC.NS")  # failures listed first
    assert "1 ok, 1 failed" in report


# ---------------------------------------------------------------- stored checks and warnings


def test_gaps_cover_never_failed_and_outdated(session: Session) -> None:
    assets = [_asset(symbol=s, name=s, display_name=s) for s in ("A", "B", "C", "D")]
    old = NOW - timedelta(days=31)
    ok = check_ticker(assets[0], FakeYahoo({"A": [_sample()]}), NOW, sleep=_no_sleep)
    failed = check_ticker(assets[1], FakeYahoo({"B": [NoPriceData("x")] * 3}), NOW, sleep=_no_sleep)
    record_checks(session, [ok, failed], NOW)
    recent_before_old = check_ticker(assets[2], FakeYahoo({"C": [_sample()]}), old)
    record_checks(session, [recent_before_old], old)
    # An older failure of A must not count: only the latest check per symbol matters.
    record_checks(session, [failed.__class__("A", "error", error="old failure")], old)
    session.flush()

    gaps = validation_gaps(session, assets, NOW)
    assert (gaps.never, gaps.failed, gaps.outdated) == (["D"], ["B"], ["C"])
    warning = validation_warning(gaps, len(assets))
    assert warning is not None
    assert "3 of 4 symbols" in warning and "over 30 days ago: C" in warning
    assert "newsdesk validate-tickers" in warning
    assert validation_warning(validation_gaps(session, assets[:1], NOW), 1) is None


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    return make_session_factory(engine)


def test_run_validation_stores_checks_and_reports(
    factory: sessionmaker[Session], settings: Settings, tmp_path: Path
) -> None:
    assert "82 of 82 symbols" in (asset_warning(factory, NOW) or "")
    samples = {symbol: [_sample(meta={})] for symbol in ASSETS}
    report = tmp_path / "ticker_report.md"

    first = run_ticker_validation(settings, factory, FakeYahoo(samples), NOW, report)
    assert first.previous_check_at is None and first.failed == []
    assert "82 ok, 0 failed" in report.read_text("utf-8")
    assert asset_warning(factory, NOW) is None
    assert asset_warning(factory, NOW + timedelta(days=31)) is not None  # re-check due

    later = NOW + timedelta(days=40)
    samples = {symbol: [_sample(meta={})] for symbol in ASSETS}
    second = run_ticker_validation(settings, factory, FakeYahoo(samples), later, report)
    assert second.previous_check_at == NOW
    lines = ticker_report_lines(second, settings)
    assert lines[0].startswith("previous check:") and "40 days ago (older than 30 days)" in lines[0]


# ---------------------------------------------------------------- benchmarks


@pytest.mark.parametrize(
    ("symbol", "benchmark"),
    [
        ("TSM", "^GSPC"),  # Taiwanese company, NYSE-listed ADR: benchmark by exchange
        ("NVDA", "^GSPC"),
        ("ONGC.NS", "^NSEI"),
        ("GLD", None),  # ETF
        ("^NSEI", None),  # index
        ("BZ=F", None),  # commodity
        ("INR=X", None),  # FX
    ],
)
def test_benchmark_is_chosen_by_exchange(symbol: str, benchmark: str | None) -> None:
    assert benchmark_for(ASSETS[symbol]) == benchmark


def test_unknown_exchange_gets_no_benchmark_rather_than_a_guess() -> None:
    assert benchmark_for(_asset(country="India", exchange="BSE")) is None


def test_report_uses_the_exchange_local_date() -> None:
    """Yahoo stamps NSE daily bars at 00:00 IST; in UTC that's the previous day."""
    ist = ZoneInfo("Asia/Kolkata")
    friday = datetime(2026, 9, 18, tzinfo=ist)
    sample = PriceSample([(friday - timedelta(days=1), 1243.9), (friday, 1226.4)], ONGC_META)
    result = check_ticker(_asset(), FakeYahoo({"ONGC.NS": [sample]}), NOW, sleep=_no_sleep)
    assert "| 2026-09-18 | 1,226.40 |" in markdown_report([result], NOW)


def test_approved_yahoo_name_is_not_flagged() -> None:
    meta = {"longName": "CBOE Interest Rate 10 Year T No", "currency": "USD"}
    meta |= {"exchangeName": "CGI", "instrumentType": "INDEX"}
    tnx = ASSETS["^TNX"]
    unapproved = tnx.model_copy(update={"approved_yahoo_names": []})
    sample = PriceSample([(NOW - timedelta(days=1), 5.0)], meta)
    flagged = check_ticker(unapproved, FakeYahoo({"^TNX": [sample]}), NOW, sleep=_no_sleep)
    assert [f.split(":")[0] for f in flagged.flags] == ["name"]
    approved = check_ticker(tnx, FakeYahoo({"^TNX": [sample]}), NOW, sleep=_no_sleep)
    assert approved.flags == []
