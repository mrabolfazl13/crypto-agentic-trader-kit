"""Offline tests for the daily-archive ingest path (the third-window unlock).

The point of this file is the *isolation*: a daily month must become readable by the
shipped research chain without changing what the monthly lake reports. So the
assertions that matter are not "October was stored" but "manifest.json and
current_data_version() still say exactly what they said before", plus one behaviour
lock for the refactor the daily parser shares with the monthly one.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from cat.quant import ingest_daily  # noqa: E402
from cat.quant.engine import load_bars  # noqa: E402
from cat.quant.ingest import (  # noqa: E402
    IngestError,
    daily_source_url,
    day_window,
    month_window,
    parse_day_archive,
    parse_month_archive,
    source_url,
)
from cat.quant.settings import QuantSettings, StorageBudget as BudgetLimits  # noqa: E402
from cat.quant.storage import (  # noqa: E402
    StorageBudget,
    StorageError,
    current_data_version,
    daily_data_version,
    day_path,
    empty_manifest,
    file_sha256,
    load_manifest,
    month_path,
    read_symbol,
    save_manifest,
    summarize_symbol,
    validate_coverage,
    write_json_atomic,
    write_parquet_atomic,
)

MONTHLY = "2026-09"
MONTHLY_START = 1788220800  # 2026-09-01T00:00:00Z
DAY = "2026-10-01"
DAY_START = 1790812800  # 2026-10-01T00:00:00Z
DAY_B = "2026-10-02"
DAY_B_START = DAY_START + 86400
DAILY_MONTH = "2026-10"


def make_settings(root: Path) -> QuantSettings:
    return QuantSettings(root=root, data_dir=root / "quant", universe_path=root / "quant" / "universe.json",
                         months=(MONTHLY,), signal_timeframes=("1h",),
                         storage=BudgetLimits(max_total_gb=1.0, raw_gb=1.0, features_gb=1.0,
                                              models_gb=1.0, results_gb=1.0, reserve_gb=0.0))


def minute_rows(start_ts: int, count: int, *, step: int = 60, price: float = 100.0,
                offset: int = 0) -> list[tuple]:
    """Twelve-column Binance kline rows: open_time, OHLC, volume, then the fields we drop."""
    rows = []
    for index in range(count):
        ts = start_ts + step * index
        close = price + (index + offset) * 0.01
        rows.append((ts * 1_000_000, close - 0.01, close + 0.5, close - 0.5, close,
                     10.0, (ts + step - 1) * 1_000_000, close * 10.0, 5, 5.0, close * 5.0, 0))
    return rows


def zip_bytes(rows: list[tuple], name: str) -> bytes:
    csv = "\n".join(",".join(str(value) for value in row) for row in rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, csv)
    return buffer.getvalue()


def day_zip(symbol: str, date: str, *, count: int = 1440, price: float = 100.0,
            extra_rows: list[tuple] | None = None) -> bytes:
    start = DAY_START if date == DAY else DAY_B_START
    rows = minute_rows(start, count, price=price)
    if extra_rows:
        rows = rows + extra_rows
    return zip_bytes(rows, f"{symbol}-1m-{date}.csv")


def bars_frame(rows: list[tuple]) -> pd.DataFrame:
    """The six stored columns, derived from a full twelve-column archive row."""
    frame = pd.DataFrame.from_records(
        rows, columns=["raw_ts", "open", "high", "low", "close", "volume", "close_ts",
                       "quote_volume", "trades", "taker_base", "taker_quote", "ignore"])
    frame["ts"] = frame["raw_ts"].astype("int64") // 1_000_000
    return frame[["ts", "open", "high", "low", "close", "volume"]]


def import_monthly_lake(settings: QuantSettings, symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")) -> bytes:
    """One stored monthly month + manifest + universe, exactly as the monthly ingest leaves them."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    budget = StorageBudget(settings)
    manifest = empty_manifest(settings)
    for offset, symbol in enumerate(symbols):
        frame = bars_frame(minute_rows(MONTHLY_START, 300, price=100.0 + offset))
        path = month_path(settings, symbol, MONTHLY)
        size = write_parquet_atomic(frame, path, budget, category="raw")
        coverage = validate_coverage(frame, MONTHLY)
        manifest["symbols"][symbol] = summarize_symbol(
            {"symbol": symbol, "months": {MONTHLY: {**coverage, "bytes": size,
                                                    "expected_minutes": 300,
                                                    "source_sha256": "monthly-sha"}}})
    save_manifest(settings, manifest)
    write_json_atomic(settings.universe_path, {
        "schema_version": 1, "selection": {"months": [MONTHLY]},
        "entries": [{"binance_symbol": symbol} for symbol in symbols],
    })
    return settings.manifest_path.read_bytes()


@pytest.fixture()
def lake(tmp_path: Path) -> QuantSettings:
    settings = make_settings(tmp_path)
    import_monthly_lake(settings)
    return settings


# ------------------------------------------------------------------ shared parser

def test_monthly_parse_keeps_its_record_shape_after_the_daily_refactor():
    raw = zip_bytes(minute_rows(MONTHLY_START, 100), "BTCUSDT-1m-2026-09.csv")
    frame, record = parse_month_archive(raw, "BTCUSDT", MONTHLY)
    assert len(frame) == 100
    assert record["rows_read"] == 100 and record["invalid_rows"] == 0 and record["duplicates"] == 0
    assert record["expected_minutes"] == month_window(MONTHLY).expected_minutes == 30 * 1440
    assert record["coverage_pct"] == 100.0 and record["status"] == "ok"
    assert record["source_url"] == source_url("BTCUSDT", MONTHLY)
    assert record["first_ts"] == MONTHLY_START
    assert set(record) >= {"rows", "missing_minutes", "gaps", "gap_starts", "coverage_pct",
                           "first_iso", "last_iso", "month", "source_bytes", "source_sha256",
                           "stored_at", "expected_minutes"}


def test_monthly_parse_error_text_is_still_the_monthly_one():
    with pytest.raises(IngestError, match=r"BTCUSDT 2026-09: archive is not a valid zip"):
        parse_month_archive(b"not a zip", "BTCUSDT", MONTHLY)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.csv", "x")
        archive.writestr("b.csv", "y")
    with pytest.raises(IngestError, match=r"BTCUSDT 2026-09: expected one CSV in the archive"):
        parse_month_archive(buffer.getvalue(), "BTCUSDT", MONTHLY)
    with pytest.raises(IngestError, match=r"BTCUSDT 2026-09: archive CSV is empty"):
        parse_month_archive(zip_bytes([], "BTCUSDT-1m-2026-09.csv"), "BTCUSDT", MONTHLY)


def test_day_parse_uses_the_same_validation_and_names_the_day():
    outside = [tuple(list(minute_rows(DAY_START - 3600, 1)[0]))]  # one row before the day
    raw = day_zip("BTCUSDT", DAY, count=10, extra_rows=outside)
    frame, record = parse_day_archive(raw, "BTCUSDT", DAY)
    assert len(frame) == 10
    assert record["rows_read"] == 11 and record["invalid_rows"] == 1
    assert record["day"] == DAY and record["month"] == DAILY_MONTH
    assert record["expected_minutes"] == day_window(DAY).expected_minutes == 1440
    assert record["source_url"] == daily_source_url("BTCUSDT", DAY)
    assert record["coverage_pct"] == pytest.approx(10 / 10 * 100.0)


def test_day_window_and_range_are_utc_calendar_days():
    assert day_window(DAY).start_ts == DAY_START and day_window(DAY).end_ts == DAY_START + 86400
    assert ingest_daily.dates_in_range("2026-10-01", "2026-10-03") == \
        ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert ingest_daily.dates_in_range("2026-10-01", "2026-10-01") == ["2026-10-01"]
    with pytest.raises(IngestError, match="ends before it starts"):
        ingest_daily.dates_in_range("2026-10-05", "2026-10-01")
    with pytest.raises(IngestError, match="expected YYYY-MM-DD"):
        ingest_daily.dates_in_range("2026-10", "2026-11")


# ------------------------------------------------------------------------ store_day

def test_store_day_downloads_once_and_resumes_without_network(lake: QuantSettings, monkeypatch):
    calls: list[str] = []

    def fake_request(url, **kwargs):
        calls.append(url)
        date = url.split("-1m-")[1].replace(".zip", "")
        return day_zip("BTCUSDT", date)

    monkeypatch.setattr(ingest_daily, "_request_bytes", fake_request)
    budget = StorageBudget(lake)
    record = ingest_daily.store_day(lake, budget, "BTCUSDT", DAY)
    assert record["rows"] == 1440 and record["resumed"] is False
    assert day_path(lake, "BTCUSDT", DAY).exists()
    assert record["file_sha256"]

    again = ingest_daily.store_day(lake, budget, "BTCUSDT", DAY)
    assert again["resumed"] is True and again["rows"] == 1440
    assert len(calls) == 1, "a stored day must not be downloaded again"


def test_store_day_honours_the_storage_budget(lake: QuantSettings, monkeypatch):
    from dataclasses import replace
    tight = replace(lake, storage=replace(lake.storage, raw_gb=0.0))

    def fake_request(url, **kwargs):
        return day_zip("BTCUSDT", url.split("-1m-")[1].replace(".zip", ""))

    monkeypatch.setattr(ingest_daily, "_request_bytes", fake_request)
    with pytest.raises(StorageError, match="budget"):
        ingest_daily.store_day(tight, StorageBudget(tight), "BTCUSDT", DAY)


# -------------------------------------------------------------------- compose_month

def store_two_days(settings: QuantSettings, monkeypatch) -> StorageBudget:
    def fake_request(url, **kwargs):
        return day_zip("BTCUSDT", url.split("-1m-")[1].replace(".zip", ""))

    monkeypatch.setattr(ingest_daily, "_request_bytes", fake_request)
    budget = StorageBudget(settings)
    for date in (DAY, DAY_B):
        ingest_daily.store_day(settings, budget, "BTCUSDT", date)
    return budget


def record_days(settings: QuantSettings, dates: list[str]) -> dict:
    """A daily-manifest `days` block written by hand, for compose-only tests."""
    entries = {}
    for date in dates:
        path = day_path(settings, "BTCUSDT", date)
        coverage = validate_coverage(pd.read_parquet(path, engine="pyarrow"), date)
        entries[date] = {**coverage, "month": DAILY_MONTH, "day": date,
                         "source_bytes": 1024, "file_sha256": file_sha256(path),
                         "file": str(path.relative_to(settings.data_dir)), "bytes": path.stat().st_size}
    return entries


def seed_daily_manifest(settings: QuantSettings, dates: list[str]) -> None:
    manifest = ingest_daily.load_daily_manifest(settings)
    manifest["months"] = [DAILY_MONTH]
    manifest["symbols"]["BTCUSDT"] = {"rows": 0, "bytes": 0, "missing_minutes": 0, "gaps": 0,
                                      "first_ts": None, "last_ts": None,
                                      "days": record_days(settings, dates), "months": {}}
    save_manifest(settings, manifest, settings.manifest_daily_path)


def test_compose_month_writes_the_usual_month_file(lake: QuantSettings, monkeypatch):
    budget = store_two_days(lake, monkeypatch)
    seed_daily_manifest(lake, [DAY, DAY_B])
    record = ingest_daily.compose_month(lake, budget, "BTCUSDT", DAILY_MONTH)
    assert record["rows"] == 2880 and record["days"] == 2
    assert record["first_ts"] == DAY_START and record["last_ts"] == DAY_B_START + 86340
    assert record["source"] == "binance_vision_1m_daily"
    assert record["coverage_pct"] == 100.0
    target = month_path(lake, "BTCUSDT", DAILY_MONTH)
    assert target.exists()
    frame = read_symbol(lake, "BTCUSDT", months=[DAILY_MONTH])
    assert len(frame) == 2880


def test_a_second_daily_run_is_idempotent_and_leaves_the_monthly_record_alone(
        lake: QuantSettings, monkeypatch):
    """Re-running the whole entry point changes no byte the older windows depend on."""
    def fake_request(url, **kwargs):
        return day_zip("BTCUSDT", url.split("-1m-")[1].replace(".zip", ""))

    monkeypatch.setattr(ingest_daily, "_request_bytes", fake_request)
    monthly_bytes = lake.manifest_path.read_bytes()
    version_before = current_data_version(lake)

    first = ingest_daily.ingest_daily(lake, symbols=["BTCUSDT"], dates=[DAY, DAY_B])
    month_file = month_path(lake, "BTCUSDT", DAILY_MONTH)
    bytes_after_first = month_file.read_bytes()
    second = ingest_daily.ingest_daily(lake, symbols=["BTCUSDT"], dates=[DAY, DAY_B])

    assert first["status"] == second["status"] == "complete", (first["errors"], second["errors"])
    assert second["digest"] == first["digest"], "recomposing the same days must not drift the digest"
    assert month_file.read_bytes() == bytes_after_first, "a composed month is not rewritten"
    assert lake.manifest_path.read_bytes() == monthly_bytes, "manifest.json stays byte-identical"
    assert current_data_version(lake) == version_before

    version = daily_data_version(lake)
    assert version["daily_months"] == [DAILY_MONTH]
    assert version["months"] == sorted({MONTHLY, DAILY_MONTH})
    assert version["manifest"] == version_before["manifest"], "the monthly digest is still the monthly digest"
    assert version["daily_manifest"]


def test_compose_refuses_a_month_file_it_did_not_build(lake: QuantSettings, monkeypatch):
    budget = store_two_days(lake, monkeypatch)
    seed_daily_manifest(lake, [DAY, DAY_B])
    write_parquet_atomic(bars_frame(minute_rows(DAY_START, 5)),
                         month_path(lake, "BTCUSDT", DAILY_MONTH), budget, category="raw")
    with pytest.raises(StorageError, match="authoritative"):
        ingest_daily.compose_month(lake, budget, "BTCUSDT", DAILY_MONTH)


def test_compose_refuses_a_month_the_monthly_archive_already_claims(lake: QuantSettings, monkeypatch):
    budget = store_two_days(lake, monkeypatch)
    seed_daily_manifest(lake, [DAY, DAY_B])
    manifest = load_manifest(lake)
    manifest["symbols"]["BTCUSDT"]["months"][DAILY_MONTH] = {"rows": 44640, "first_ts": DAY_START,
                                                             "last_ts": DAY_START + 100, "source_sha256": "zip"}
    save_manifest(lake, manifest)
    with pytest.raises(StorageError, match="never shadows it"):
        ingest_daily.compose_month(lake, budget, "BTCUSDT", DAILY_MONTH)


def test_compose_needs_stored_days(lake: QuantSettings):
    with pytest.raises(StorageError, match="no daily bars stored"):
        ingest_daily.compose_month(lake, StorageBudget(lake), "BTCUSDT", DAILY_MONTH)


# --------------------------------------------------------------------- ingest_daily

def test_ingest_daily_refuses_a_published_month_before_downloading(lake: QuantSettings, monkeypatch):
    calls: list[str] = []

    def fake_request(url, **kwargs):
        calls.append(url)
        return day_zip("BTCUSDT", url.split("-1m-")[1].replace(".zip", ""))

    monkeypatch.setattr(ingest_daily, "_request_bytes", fake_request)
    manifest = load_manifest(lake)
    manifest["symbols"]["BTCUSDT"]["months"][DAILY_MONTH] = {"rows": 1, "source_sha256": "zip"}
    save_manifest(lake, manifest)
    with pytest.raises(IngestError, match="already stored from the monthly archive"):
        ingest_daily.ingest_daily(lake, symbols=["BTCUSDT"], dates=[DAY])
    assert calls == []


def test_ingest_daily_rejects_unknown_symbols_and_empty_dates(lake: QuantSettings):
    with pytest.raises(IngestError, match="outside the recorded universe"):
        ingest_daily.ingest_daily(lake, symbols=["NOPEUSDT"], dates=[DAY])
    with pytest.raises(IngestError, match="at least one date"):
        ingest_daily.ingest_daily(lake, symbols=["BTCUSDT"], dates=[])


def test_ingest_daily_stores_composes_and_reports(lake: QuantSettings, monkeypatch):
    def fake_request(url, **kwargs):
        return day_zip("BTCUSDT", url.split("-1m-")[1].replace(".zip", ""))

    monkeypatch.setattr(ingest_daily, "_request_bytes", fake_request)
    result = ingest_daily.ingest_daily(lake, symbols=["BTCUSDT", "ETHUSDT"], dates=[DAY, DAY_B])
    assert result["status"] == "complete", result["errors"]
    assert result["days_stored"] == 4 and result["rows_composed"] == 2 * 2880
    assert result["composed_rows"] == {"BTCUSDT": {DAILY_MONTH: 2880}, "ETHUSDT": {DAILY_MONTH: 2880}}
    coverage = ingest_daily.daily_coverage(lake)
    month = coverage["months"][DAILY_MONTH]
    assert month["symbols"] == 2 and month["day_count"] == 2
    assert month["span"] == {"first": DAY, "last": DAY_B, "expected_days": 2}
    assert json.loads(lake.manifest_daily_path.read_text(encoding="utf-8"))["schema_version"] == \
        ingest_daily.MANIFEST_DAILY_SCHEMA


def test_ingest_daily_keeps_the_monthly_digest_after_a_full_run(lake: QuantSettings, monkeypatch):
    before = current_data_version(lake)
    monthly_bytes = lake.manifest_path.read_bytes()

    def fake_request(url, **kwargs):
        return day_zip("BTCUSDT", url.split("-1m-")[1].replace(".zip", ""),
                       price=float(url.count("ETH")))

    monkeypatch.setattr(ingest_daily, "_request_bytes", fake_request)
    ingest_daily.ingest_daily(lake, symbols=["BTCUSDT"], dates=[DAY])
    assert current_data_version(lake) == before
    assert lake.manifest_path.read_bytes() == monthly_bytes


# ------------------------------------------------------------------ load_bars gate

def test_compose_provenance_is_independent_of_download_order(lake: QuantSettings, monkeypatch):
    """Threads finish in any order; the month's provenance digest must not move with them."""
    def fake_request(url, **kwargs):
        return day_zip("BTCUSDT", url.split("-1m-")[1].replace(".zip", ""))

    monkeypatch.setattr(ingest_daily, "_request_bytes", fake_request)
    ingest_daily.ingest_daily(lake, symbols=["BTCUSDT"], dates=[DAY, DAY_B])
    days = ingest_daily.stored_days(lake, "BTCUSDT", DAILY_MONTH)
    budget = StorageBudget(lake)
    a = ingest_daily.compose_month(lake, budget, "BTCUSDT", DAILY_MONTH,
                                   days=dict(sorted(days.items())))
    b = ingest_daily.compose_month(lake, budget, "BTCUSDT", DAILY_MONTH,
                                   days=dict(sorted(days.items(), reverse=True)))
    assert a["source_sha256"] == b["source_sha256"]
    assert a["day_dates"] == b["day_dates"] == [DAY, DAY_B]


def test_extra_months_flag_is_opt_in_and_sorted(lake: QuantSettings):
    from paper_signal_census import months_with_extra

    assert months_with_extra(lake, "") == list(lake.months)
    assert months_with_extra(lake, "2026-10") == [MONTHLY, DAILY_MONTH]
    assert months_with_extra(lake, " 2026-10 ,, 2026-10 ") == [MONTHLY, DAILY_MONTH]
    assert months_with_extra(lake, MONTHLY) == [MONTHLY], "a configured month is never duplicated"


def test_load_bars_reads_a_daily_month_only_when_it_is_named(lake: QuantSettings, monkeypatch):
    budget = store_two_days(lake, monkeypatch)
    seed_daily_manifest(lake, [DAY, DAY_B])
    ingest_daily.compose_month(lake, budget, "BTCUSDT", DAILY_MONTH)

    default = load_bars(lake, "BTCUSDT")
    assert default["ts"].max() < DAY_START, "an unnamed run must not silently pick up October"

    named = load_bars(lake, "BTCUSDT", months=[MONTHLY, DAILY_MONTH])
    assert named["ts"].max() >= DAY_START
    assert len(named) == 300 + 2880

    with pytest.raises(StorageError, match="must look like"):
        load_bars(lake, "BTCUSDT", months=["202610"])
