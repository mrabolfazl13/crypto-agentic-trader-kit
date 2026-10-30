"""Offline tests for the TSE daily ingest (D-040).

Nothing here reaches the network: the board and the daily rows come from the recorded
fixtures in ``tests/fixtures``, and the tool's three fetchers are monkeypatched.
"""

from __future__ import annotations

import copy
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))

import tse_ingest_daily as ingest  # noqa: E402
from tse_helpers import FOLD_CODE, load_fixture, marketwatch_rows  # noqa: E402

BOARD_DATE = 20261021


def row(**overrides):
    base = {
        "dEven": 20261021, "hEven": 123002,
        "priceFirst": 4340.0, "priceMax": 4340.0, "priceMin": 4210.0, "pClosing": 4220.0,
        "qTotTran5J": 4864252101.0, "qTotCap": 20519267355350.0, "zTotTran": 48279.0,
    }
    base.update(overrides)
    return base


def test_session_date_reads_the_venues_gregorian_stamp():
    assert ingest.session_date(20261021).isoformat() == "2026-10-21"
    assert ingest.session_date(0) is None
    assert ingest.session_date(2026102) is None
    assert ingest.session_date(20261345) is None
    assert ingest.session_date(None) is None


def test_bar_maps_the_venue_fields_without_inventing_any():
    bar, reason = ingest.bar_from_row(row(), ins_code=FOLD_CODE, board_date=BOARD_DATE)
    assert reason is None
    assert bar["date"] == "2026-10-21"
    assert bar["open"] == 4340.0 and bar["high"] == 4340.0
    assert bar["low"] == 4210.0 and bar["close"] == 4220.0
    assert bar["volume_shares"] == 4864252101.0
    assert bar["traded_value_rial"] == 20519267355350.0


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"pClosing": 0.0}, "no_close"),
        ({"priceFirst": 0.0}, "no_open"),
        ({"priceMax": 0.0}, "no_range"),
        ({"priceMin": 0.0}, "no_range"),
        ({"qTotTran5J": 0.0, "qTotCap": 0.0}, "no_trading_session"),
        # the live pattern: no trade at all, so every OHLC field is 0 while pClosing is carried
        # over. Labelling it a defect would be wrong; filling it would be worse.
        ({"priceFirst": 0.0, "priceMax": 0.0, "priceMin": 0.0, "qTotTran5J": 0.0, "qTotCap": 0.0},
         "no_trading_session"),
        ({"dEven": 0}, "invalid_session_date"),
        ({"dEven": 20261015}, "non_session_weekday"),   # Thursday
        ({"dEven": 20261016}, "non_session_weekday"),   # Friday
        ({"dEven": 20261031}, "session_after_board_date"),
    ],
)
def test_unusable_rows_are_dropped_with_a_reason_never_repaired(overrides, reason):
    bar, why = ingest.bar_from_row(row(**overrides), ins_code=FOLD_CODE, board_date=BOARD_DATE)
    assert bar is None
    assert why == reason


def test_a_zero_volume_day_is_not_counted_as_a_broken_row():
    """Dropping is the same either way; the label decides whether the lake looks corrupted."""
    dead = row(priceFirst=0.0, priceMax=0.0, priceMin=0.0, qTotTran5J=0.0, qTotCap=0.0)
    broken = row(priceFirst=0.0, qTotTran5J=1_000.0, qTotCap=1_000.0)
    assert ingest.bar_from_row(dead, ins_code=FOLD_CODE, board_date=BOARD_DATE)[1] == "no_trading_session"
    assert ingest.bar_from_row(broken, ins_code=FOLD_CODE, board_date=BOARD_DATE)[1] == "no_open"


def test_ohlc_inconsistency_is_a_drop_not_a_correction():
    bar, why = ingest.bar_from_row(row(priceMax=4100.0), ins_code=FOLD_CODE, board_date=BOARD_DATE)
    assert bar is None and why == "ohlc_inconsistent"
    bar, why = ingest.bar_from_row(row(priceMin=4400.0), ins_code=FOLD_CODE, board_date=BOARD_DATE)
    assert bar is None and why == "ohlc_inconsistent"


def test_bars_from_rows_dedupes_the_same_session_and_counts_it():
    bars, dropped = ingest.bars_from_rows([row(), row()], ins_code=FOLD_CODE, board_date=BOARD_DATE)
    assert len(bars) == 1
    assert dropped["duplicate_session"] == 1


def test_bars_from_rows_sorts_by_session():
    rows = [row(dEven=20261019), row(dEven=20261021), row(dEven=20261018)]
    bars, dropped = ingest.bars_from_rows(rows, ins_code=FOLD_CODE, board_date=BOARD_DATE)
    assert [bar["session"] for bar in bars] == [20261018, 20261019, 20261021]
    assert not dropped


def test_audit_flags_depth_capped_when_the_source_stops_short_of_the_target():
    bars = [{"session": 20261018, "date": "2026-10-18"}, {"session": 20261021, "date": "2026-10-21"}]
    audit = ingest.audit_symbol(bars, rows_requested=200, rows_returned=200, target_start=date(2025, 10, 21))
    assert audit["depth_capped_by_source"] is True
    assert audit["shortfall_days"] > 300
    assert audit["sessions"] == 2


def test_audit_does_not_call_a_short_history_capped():
    bars = [{"session": 20251021, "date": "2025-10-21"}, {"session": 20261021, "date": "2026-10-21"}]
    audit = ingest.audit_symbol(bars, rows_requested=200, rows_returned=60, target_start=date(2025, 10, 21))
    assert audit["depth_capped_by_source"] is False
    assert audit["shortfall_days"] == 0


def test_aggregate_verdict_is_degraded_when_integrity_breaks():
    per_symbol = {"1": {"sessions": 3, "usable": True, "oldest_session": 20261018, "newest_session": 20261021,
                        "calendar_days": 4, "depth_capped_by_source": True, "shortfall_days": 300,
                        "rows_requested": 200, "rows_returned": 200}}
    target = date(2025, 10, 21)
    clean = ingest.aggregate_audit(per_symbol, Counter(), universe_count=1, fetch_errors=Counter(),
                                   target_start=target, board_date=BOARD_DATE)
    assert clean["verdict"] == "usable"
    assert clean["usable_symbols"] == 1
    assert clean["symbols_depth_capped_by_source"] == 1
    assert clean["bars_total"] == 3 and clean["rows_returned_total"] == 200
    assert clean["traded_session_share"] == pytest.approx(0.015)
    dirty = ingest.aggregate_audit(per_symbol, Counter({"duplicate_session": 2}), universe_count=1,
                                   fetch_errors=Counter(), target_start=target, board_date=BOARD_DATE)
    assert dirty["verdict"] == "degraded"
    assert dirty["integrity_violations"] == 2
    with_errors = ingest.aggregate_audit(per_symbol, Counter(), universe_count=1,
                                         fetch_errors=Counter({"FetchError": 1}),
                                         target_start=target, board_date=BOARD_DATE)
    assert with_errors["verdict"] == "degraded"


def test_bars_roundtrip_through_jsonl(tmp_path: Path):
    bars, _ = ingest.bars_from_rows(load_fixture("tse_daily_foolad.json")["closingPriceDaily"],
                                    ins_code=FOLD_CODE, board_date=BOARD_DATE)
    path = ingest.write_bars(tmp_path, FOLD_CODE, bars)
    assert path.read_text(encoding="utf-8").count("\n") == len(bars)
    assert ingest.read_bars(path) == bars


def test_digest_changes_only_with_the_session_sets():
    bars_a = {"1": [{"session": 20261021}], "2": [{"session": 20261020}]}
    bars_b = {"2": [{"session": 20261020}], "1": [{"session": 20261021}]}
    bars_c = {"1": [{"session": 20261022}], "2": [{"session": 20261020}]}
    assert ingest.bars_digest(bars_a) == ingest.bars_digest(bars_b)
    assert ingest.bars_digest(bars_a) != ingest.bars_digest(bars_c)


def test_daily_response_without_the_expected_key_is_refused(monkeypatch):
    """A missing endpoint on this CDN answers 200 with an HTML shell — never treat that as data."""
    monkeypatch.setattr(ingest, "fetch_json", lambda *a, **k: {"marketwatch": []})
    with pytest.raises(ingest.FetchError, match="not a closingPriceDaily payload"):
        ingest.fetch_daily_rows(None, FOLD_CODE, 200)


@pytest.fixture
def offline_source(monkeypatch):
    rows = load_fixture("tse_daily_foolad.json")["closingPriceDaily"]

    def fake_board_date(_proxy):
        return BOARD_DATE

    monkeypatch.setattr(ingest, "fetch_board", lambda _proxy: marketwatch_rows())
    monkeypatch.setattr(ingest, "fetch_board_date", fake_board_date)
    monkeypatch.setattr(ingest, "fetch_daily_rows", lambda _p, _c, _r: copy.deepcopy(rows))
    return rows


def test_main_writes_a_manifest_from_the_offline_board(tmp_path: Path, offline_source):
    code = ingest.main(["--out-dir", str(tmp_path), "--max-symbols", "3", "--delay", "0"])
    assert code == 0
    manifest = json.loads((tmp_path / "tse_daily_manifest.json").read_text(encoding="utf-8"))
    assert manifest["audit"]["usable_symbols"] == 3
    assert manifest["audit"]["bars_total"] == 3 * len(offline_source)
    assert manifest["audit"]["verdict"] == "usable"
    assert manifest["safety"] == {"mode": "read-only ingest", "orders_placed": 0, "live": False,
                                  "credentials_used": False}
    assert manifest["source_contract"]["paging_available"] is False
    assert manifest["source_contract"]["source_row_cap"] == ingest.SOURCE_MAX_DAILY_ROWS
    assert manifest["universe"]["requested_top_k"] == ingest.DEFAULT_TOP_K
    assert len(list((tmp_path / "bars").glob("*.jsonl"))) == 3
    assert manifest["audit"]["dropped_rows"] == {}


def test_main_reuses_bars_already_on_disk(tmp_path: Path, offline_source):
    first = ingest.main(["--out-dir", str(tmp_path), "--max-symbols", "2", "--delay", "0"])
    assert first == 0
    digest_before = json.loads((tmp_path / "tse_daily_manifest.json").read_text(encoding="utf-8"))["bars_digest"]
    second = ingest.main(["--out-dir", str(tmp_path), "--max-symbols", "2", "--delay", "0"])
    manifest = json.loads((tmp_path / "tse_daily_manifest.json").read_text(encoding="utf-8"))
    assert second == 0
    assert manifest["run"]["reused_from_disk"] == 2
    assert manifest["bars_digest"] == digest_before


def test_dry_run_writes_nothing(tmp_path: Path, offline_source):
    assert ingest.main(["--out-dir", str(tmp_path / "nope"), "--max-symbols", "2", "--no-write", "--delay", "0"]) == 0
    assert not (tmp_path / "nope").exists()
