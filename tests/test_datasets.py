"""Offline, deterministic tests for dataset ingestion and quality reporting.

Nothing here touches the network: the exchange path is exercised through a stub
gate object, and every timestamp is either fixed or derived from the clock in a
way that stays valid while the test runs.
"""

from __future__ import annotations

import csv
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cat.data import DataUnavailable, Kline
from cat.datasets import (
    DatasetError,
    compare_sources,
    ingest_csv,
    ingest_exchange,
    load_klines,
    parse_csv,
    quality_report,
    resample,
)
from cat.store import open_store

UTC = timezone.utc
HOUR = 3600
DAY = 86400
ALIGNED = 1_699_992_000  # divisible by 14400, so 4h buckets are predictable
BASE = 1_700_000_000
HEADER = ["timestamp", "open", "high", "low", "close", "volume", "quote_volume"]


def write_csv(path: Path, header: list, rows: list[list]) -> Path:
    """Write a real CSV (quoting cells that contain commas) without pandas."""
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([str(cell) for cell in header])
        writer.writerows(rows)
    return path


def hourly_rows(n: int, *, start: int = BASE, scale: str = "seconds") -> list[list]:
    prices = [100.0 + index for index in range(n)]
    rows = []
    for index, close in enumerate(prices):
        open_ = prices[index - 1] if index else close
        stamp = (start + index * HOUR) * 1000 if scale == "millis" else start + index * HOUR
        rows.append([stamp, open_, max(open_, close) * 1.001, min(open_, close) * 0.999, close,
                     10.0 + index, (10.0 + index) * close])
    return rows


def bars(n: int = 6, *, start: int = ALIGNED, closed: bool = True) -> list[Kline]:
    prices = [100.0 + index for index in range(n)]
    out = []
    for index, close in enumerate(prices):
        open_ = prices[index - 1] if index else close
        out.append(
            Kline(open_time=start + index * HOUR, open=open_, high=max(open_, close) * 1.004,
                  low=min(open_, close) * 0.996, close=close, base_volume=100.0,
                  quote_volume=100.0 * close, closed=closed)
        )
    return out


class StubGate:
    """Stand-in for GateProvider: records calls, never performs I/O."""

    name = "gate"

    def __init__(self, bars_list: list[Kline], *, fail: bool = False):
        self.bars = bars_list
        self.fail = fail
        self.calls: list[tuple] = []

    def klines_range(self, symbol: str, timeframe: str, start_ts: int, end_ts: int, page: int = 800) -> list[Kline]:
        self.calls.append((symbol, timeframe, start_ts, end_ts, page))
        if self.fail:
            raise DataUnavailable("simulated provider outage")
        return list(self.bars)


# ------------------------------------------------------------------ parsing


def test_parse_csv_accepts_epoch_seconds_and_milliseconds(tmp_path: Path):
    seconds_file = write_csv(tmp_path / "sec.csv", HEADER, hourly_rows(4))
    millis_file = write_csv(tmp_path / "ms.csv", HEADER, hourly_rows(4, scale="millis"))

    bars_s, meta_s = parse_csv(seconds_file)
    bars_m, meta_m = parse_csv(millis_file)

    assert [b.open_time for b in bars_s] == [BASE + i * HOUR for i in range(4)]
    assert bars_s == bars_m, "millisecond epochs must normalise to the same UTC seconds"
    assert meta_s["timeframe"] == "1h" and meta_m["timeframe"] == "1h"
    assert meta_s["spacing_seconds"] == HOUR
    assert meta_s["dropped_rows"] == 0 and meta_s["rows_read"] == 4
    assert bars_s[0].base_volume == 10.0 and bars_s[0].quote_volume == pytest.approx(10.0 * 100.0)
    assert all(b.closed for b in bars_s)


def test_parse_csv_accepts_iso8601_and_assumes_utc_for_naive_dates(tmp_path: Path):
    aware_rows = [[datetime.fromtimestamp(BASE + i * HOUR, UTC).isoformat(), 100.0, 101.0, 99.0, 100.5, 5.0, 502.5]
                  for i in range(3)]
    aware, _ = parse_csv(write_csv(tmp_path / "iso.csv", HEADER, aware_rows))
    assert [b.open_time for b in aware] == [BASE + i * HOUR for i in range(3)]

    naive_rows = [[datetime.fromtimestamp(BASE + i * HOUR, UTC).strftime("%Y-%m-%d %H:%M:%S"),
                   100.0, 101.0, 99.0, 100.5, 5.0, 502.5] for i in range(3)]
    naive, meta = parse_csv(write_csv(tmp_path / "iso_naive.csv", HEADER, naive_rows))
    assert [b.open_time for b in naive] == [BASE + i * HOUR for i in range(3)]
    assert "naive_timestamps_assumed_utc" in meta["warnings"]


def test_parse_csv_matches_header_variants_and_short_aliases(tmp_path: Path):
    rows = hourly_rows(3)
    verbose = ["Date", "Open Price", "High", "Low", "Close", "Base Volume", "Quote Volume"]
    short = ["time", "o", "h", "l", "c", "v", "qv"]

    bars_a, meta_a = parse_csv(write_csv(tmp_path / "verbose.csv", verbose, rows))
    bars_b, _ = parse_csv(write_csv(tmp_path / "short.csv", short, rows))
    bars_c, _ = parse_csv(write_csv(tmp_path / "space.csv",
                                    [" Timestamp ", "OPEN", "high", "low", "Close", "base volume", "QV"], rows))

    assert bars_a == bars_b == bars_c
    assert meta_a["timeframe"] == "1h"


def test_parse_csv_strips_thousands_separators_and_currency(tmp_path: Path):
    rows = [
        [BASE, "1,234.50", "$1,300.25", "1 100", "€1,299.99", "10,500", "12,900,000.50"],
        [BASE + HOUR, "1,299.99", "1,350.00", "1,250.00", "1,320.00", "9,000", "11,880,000"],
    ]
    parsed, meta = parse_csv(write_csv(tmp_path / "money.csv", HEADER, rows))
    assert parsed[0].open == pytest.approx(1234.50)
    assert parsed[0].high == pytest.approx(1300.25)
    assert parsed[0].low == pytest.approx(1100.00)
    assert parsed[0].close == pytest.approx(1299.99)
    assert parsed[0].base_volume == pytest.approx(10500.0)
    assert parsed[1].close == pytest.approx(1320.00)
    assert meta["dropped_rows"] == 0


def test_parse_csv_without_volume_warns_instead_of_looking_complete(tmp_path: Path):
    rows = [[BASE + i * HOUR, 100.0, 101.0, 99.0, 100.5] for i in range(4)]
    parsed, meta = parse_csv(write_csv(tmp_path / "novol.csv",
                                       ["timestamp", "open", "high", "low", "close"], rows))
    assert meta["missing_volume"] is True and "missing_volume" in meta["warnings"]
    assert all(b.base_volume == 0.0 for b in parsed)
    quality = quality_report(parsed, timeframe="1h", source="novol.csv", origin="csv", now_ts=BASE + 4 * HOUR)
    assert "missing_volume" in quality["warnings"]


def test_parse_csv_derives_missing_high_low(tmp_path: Path):
    rows = [[BASE + i * HOUR, 100.0 + i, 105.0 + i, 2.0] for i in range(3)]
    parsed, meta = parse_csv(write_csv(tmp_path / "hl.csv", ["timestamp", "open", "close", "volume"], rows))
    assert "synthetic_hl" in meta["warnings"]
    assert parsed[1].high == 106.0 and parsed[1].low == 101.0
    assert parsed[0].high == parsed[0].close == 105.0 and parsed[0].low == parsed[0].open == 100.0


def test_parse_csv_counts_unparseable_rows_without_crashing(tmp_path: Path):
    rows = hourly_rows(4)
    rows[1][4] = "N/A"
    rows[2][1] = "-500"
    rows.append([BASE + 9 * HOUR, "abc", "abc", "abc", "abc", "abc", "abc"])
    parsed, meta = parse_csv(write_csv(tmp_path / "dirty.csv", HEADER, rows))
    assert meta["rows_read"] == 5
    assert meta["dropped_rows"] == 3 and meta["parsed_rows"] == 2
    assert sum(meta["drop_reasons"].values()) == 3
    assert [b.open_time for b in parsed] == [BASE, BASE + 3 * HOUR]


def test_parse_csv_rejects_unrecognisable_header(tmp_path: Path):
    junk = write_csv(tmp_path / "junk.csv", ["foo", "bar", "baz", "qux", "quux", "corge"], [[1, 2, 3, 4, 5, 6]] * 3)
    with pytest.raises(DatasetError, match="not recognisable"):
        parse_csv(junk)

    blank = tmp_path / "blank.csv"
    blank.write_text("", encoding="utf-8")
    with pytest.raises(DatasetError, match="empty"):
        parse_csv(blank)
    with pytest.raises(DatasetError, match="not found"):
        parse_csv(tmp_path / "missing.csv")


def test_parse_csv_reads_headerless_positional_export(tmp_path: Path):
    rows = hourly_rows(3)
    parsed, meta = parse_csv(write_csv(tmp_path / "raw.csv", rows[0], rows[1:]))
    assert meta["warnings"].count("headerless_positional_columns") == 1
    assert len(parsed) == 3
    assert parsed[0].open_time == int(rows[0][0]) and parsed[0].base_volume == rows[0][5]


def test_parse_csv_marks_the_in_progress_bar_unclosed(tmp_path: Path):
    now = int(time.time())
    start = now // HOUR * HOUR - 4 * HOUR
    rows = [[start + i * HOUR, 100.0, 101.0, 99.0, 100.5, 1.0, 100.5] for i in range(5)]
    parsed, _ = parse_csv(write_csv(tmp_path / "live.csv", HEADER, rows))
    assert [b.closed for b in parsed] == [True, True, True, True, False]
    quality = quality_report(parsed, timeframe="1h", source="live.csv", origin="csv", now_ts=now)
    assert quality["dropped_unclosed"] == 1 and quality["closed_bars"] == 4


# ------------------------------------------------------------------ quality


def test_quality_report_counts_duplicates_gaps_and_coverage():
    source = bars(20)
    with_gap = source[:5] + source[7:] + [source[10]]
    quality = quality_report(with_gap, timeframe="1h", source="x.csv", origin="csv",
                             now_ts=source[-1].open_time + HOUR + 60)
    assert quality["bars"] == 19 and quality["closed_bars"] == 19 and quality["dropped_unclosed"] == 0
    assert quality["duplicates"] == 1
    assert quality["gaps"]["count"] == 1 and quality["gaps"]["missing_bars"] == 2
    assert quality["gaps"]["starts"] == [source[4].open_time + HOUR]
    assert quality["coverage_pct"] == pytest.approx(18 / 20 * 100.0)
    assert quality["stale_seconds"] == 60 and quality["is_stale"] is False
    assert quality["median_spacing"] == HOUR
    assert quality["first_iso"].endswith("+00:00") and quality["last_ts"] == source[-1].open_time
    assert quality["source"] == "x.csv" and quality["origin"] == "csv"
    assert quality["incomplete"] is True, "two missing bars is a hole in the window"
    assert "duplicate_timestamps" in quality["warnings"] and "time_gap" in quality["warnings"]


def test_quality_report_flags_impossible_ohlc_and_staleness():
    good = bars(5)
    broken = replace(good[2], high=90.0, low=110.0)
    quality = quality_report([*good[:2], broken, *good[3:]], timeframe="1h", source="s", origin="csv",
                             now_ts=good[-1].open_time + HOUR)
    assert quality["impossible_ohlc"] == 1 and "impossible_ohlc" in quality["warnings"]

    ancient = quality_report(good, timeframe="1h", source="s", origin="csv", now_ts=good[-1].open_time + HOUR + 50 * DAY)
    assert ancient["is_stale"] is True and ancient["stale_seconds"] == 50 * DAY
    assert "stale_klines" in ancient["warnings"]
    assert ancient["incomplete"] is False, "old but whole: staleness is reported separately"

    empty = quality_report([], timeframe="1h", source="s", origin="csv", now_ts=BASE)
    assert empty["bars"] == 0 and empty["warnings"] == ["no_data"] and empty["coverage_pct"] == 0.0


def test_quality_report_non_positive_prices_and_gap_start_cap():
    with_zero = bars(4)
    with_zero[1] = replace(with_zero[1], low=0.0)
    quality = quality_report(with_zero, timeframe="1h", source="s", origin="csv",
                             now_ts=with_zero[-1].open_time + HOUR)
    assert quality["non_positive_prices"] == 1 and "non_positive_prices" in quality["warnings"]

    sparse = [replace(bar, open_time=ALIGNED + index * 20 * HOUR) for index, bar in enumerate(bars(12))]
    report = quality_report(sparse, timeframe="1h", source="s", origin="csv", now_ts=sparse[-1].open_time + HOUR)
    assert report["gaps"]["count"] == 11 and len(report["gaps"]["starts"]) == 10
    assert report["gaps"]["missing_bars"] == 11 * 19
    assert report["coverage_pct"] == pytest.approx(round(12 / 221 * 100.0, 2))


# ------------------------------------------------------------------- ingest


def test_ingest_csv_is_idempotent_and_persists_quality(tmp_path: Path):
    store = open_store(tmp_path)
    path = write_csv(tmp_path / "btc.csv", HEADER, hourly_rows(10))

    first = ingest_csv(store, path, symbol="btc/usdt", note="fixture import")
    second = ingest_csv(store, path, symbol="btc/usdt", note="fixture import")

    assert first["dataset_id"] == second["dataset_id"]
    assert first["dataset_id"].startswith("ds-csv-") and len(first["dataset_id"]) == len("ds-csv-") + 10
    assert first["timeframe"] == "1h" and first["bars"] == 10 and second["bars"] == 10
    datasets = store.list_datasets()
    assert len(datasets) == 1 and datasets[0]["origin"] == "csv" and datasets[0]["source"] == "btc.csv"
    assert datasets[0]["symbol"] == "BTC_USDT" and datasets[0]["note"] == "fixture import"
    assert len(store.load_bars(first["dataset_id"])) == 10

    stored = store.dataset_quality(first["dataset_id"])
    assert stored["coverage_pct"] == 100.0 and stored["dropped_rows"] == 0
    assert stored["incomplete"] is False
    assert stored["is_stale"] is True, "a 2023 fixture is old versus the wall clock and says so"
    assert any(row["source"] == "datasets" for row in store.list_logs())
    store.close()


def test_ingest_csv_requires_a_usable_timeframe_and_symbol(tmp_path: Path):
    store = open_store(tmp_path)
    single = write_csv(tmp_path / "one.csv", HEADER, hourly_rows(1))
    with pytest.raises(DatasetError, match="timeframe"):
        ingest_csv(store, single, symbol="BTC_USDT")
    pair = write_csv(tmp_path / "two.csv", HEADER, hourly_rows(2))
    with pytest.raises(DatasetError, match="symbol"):
        ingest_csv(store, pair, symbol="NOT_A_PAIR")
    assert store.list_datasets() == []
    store.close()


def test_load_klines_round_trips_exactly(tmp_path: Path):
    store = open_store(tmp_path)
    path = write_csv(tmp_path / "rt.csv", HEADER, hourly_rows(12))
    parsed, _ = parse_csv(path)
    result = ingest_csv(store, path, symbol="ETH_USDT", timeframe="1h")

    reloaded = load_klines(store, result["dataset_id"])
    assert reloaded == parsed
    assert reloaded[0].quote_volume == pytest.approx(10.0 * 100.0)
    with pytest.raises(DatasetError, match="no stored bars"):
        load_klines(store, "ds-csv-doesnotexist")
    store.close()


# ----------------------------------------------------------------- resample


def test_resample_aggregates_six_hourly_bars_into_two_four_hour_bars():
    levels = [100.0, 105.0, 115.0, 120.0, 118.0, 135.0]
    source = [
        Kline(open_time=ALIGNED + index * HOUR, open=level, high=level + 10.0, low=level - 10.0, close=level,
              base_volume=float(index + 1), quote_volume=float(index + 1) * 100.0, closed=True)
        for index, level in enumerate(levels)
    ]
    out = resample(source, "4h")
    assert [bar.open_time for bar in out] == [ALIGNED, ALIGNED + 14400]
    assert (out[0].open, out[0].high, out[0].low, out[0].close) == (100.0, 130.0, 90.0, 120.0)
    assert out[0].base_volume == pytest.approx(10.0) and out[0].quote_volume == pytest.approx(1000.0)
    assert (out[1].open, out[1].high, out[1].low, out[1].close) == (118.0, 145.0, 108.0, 135.0)
    assert out[1].base_volume == pytest.approx(11.0) and out[1].quote_volume == pytest.approx(1100.0)

    mixed = resample([*source[:5], replace(source[5], closed=False)], "4h")
    assert mixed[0].closed is True and mixed[1].closed is False

    with pytest.raises(DatasetError, match="coarser"):
        resample(source, "15m")
    assert resample([], "1d") == []


# -------------------------------------------------------------- cross-check


def test_compare_sources_finds_a_one_percent_disagreement():
    a = bars(10)
    b = list(a)
    a[4] = replace(a[4], close=101.0)
    b[4] = replace(b[4], close=100.0)
    report = compare_sources(a, b)
    assert report["common_bars"] == 10
    assert report["mismatched_bars"] == 1
    assert report["max_abs_diff_pct"] == pytest.approx(1.0)
    assert report["mean_abs_diff_pct"] == pytest.approx(0.1)
    assert report["worst_open_time"] == a[4].open_time
    assert report["sources_agree"] is False

    identical = compare_sources(a, a)
    assert identical["sources_agree"] is True and identical["mismatched_bars"] == 0
    shifted = compare_sources(a, bars(9))
    assert shifted["only_in_a"] == 1 and shifted["only_in_b"] == 0


# ---------------------------------------------------------------- exchange


def test_ingest_exchange_marks_a_short_history_incomplete_without_network(tmp_path: Path):
    store = open_store(tmp_path)
    now = int(time.time())
    last_open = now // HOUR * HOUR - HOUR
    gate = StubGate(bars(10, start=last_open - 9 * HOUR))

    result = ingest_exchange(store, gate, "BTC_USDT", "1h", 30)
    symbol, timeframe, start_ts, end_ts, _page = gate.calls[0]
    assert (symbol, timeframe) == ("BTC_USDT", "1h")
    assert end_ts - start_ts == 30 * DAY and abs(end_ts - now) <= 5

    quality = result["quality"]
    assert quality["origin"] == "exchange" and quality["source"] == "gate"
    assert quality["requested_bars"] == 720 and quality["closed_bars"] == 10
    assert quality["bar_ratio_pct"] == pytest.approx(round(10 / 720 * 100.0, 2))
    assert quality["incomplete"] is True and "incomplete_history" in quality["warnings"]
    assert result["dataset_id"].startswith("ds-ex-") and result["bars"] == 10

    full = StubGate(bars(720, start=last_open - 719 * HOUR))
    complete = ingest_exchange(store, full, "ETH_USDT", "1h", 30)
    assert complete["dataset_id"].startswith("ds-ex-")
    assert complete["dataset_id"] != result["dataset_id"]
    assert complete["quality"]["incomplete"] is False
    assert "incomplete_history" not in complete["quality"]["warnings"]
    assert len(store.list_datasets()) == 2
    assert len(store.load_bars(complete["dataset_id"])) == 720
    store.close()


def test_ingest_exchange_refuses_bad_window_and_propagates_provider_errors(tmp_path: Path):
    store = open_store(tmp_path)
    gate = StubGate(bars(5))
    for bad_days in (0, -3):
        with pytest.raises(DatasetError, match="positive"):
            ingest_exchange(store, gate, "BTC_USDT", "1h", bad_days)
    assert gate.calls == [] and store.list_datasets() == []

    with pytest.raises(DatasetError, match="unsupported timeframe"):
        ingest_exchange(store, gate, "BTC_USDT", "7m", 5)
    assert gate.calls == []

    failing = StubGate([], fail=True)
    with pytest.raises(DataUnavailable, match="outage"):
        ingest_exchange(store, failing, "BTC_USDT", "1h", 2)
    assert store.list_datasets() == [], "a failed fetch must never be stored as a dataset"
    store.close()
