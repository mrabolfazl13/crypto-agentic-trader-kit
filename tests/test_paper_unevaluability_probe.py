"""Locks for the D-028 probe: the rounding rule, the cadence of the scan, and the ledger buckets.

The tool exists to separate "the market did not move" from "our arithmetic deleted the move", so the
tests below pin exactly that seam — a misclassified decision point would turn a data artifact into a
supposed signal, which is the F-030 failure this project has now hit three times.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import cat.strategy as strategy_module  # noqa: E402
import pytest  # noqa: E402
from paper_decisions_unarchived import classify  # noqa: E402
from paper_unevaluability_probe import (  # noqa: E402
    WINDOW_DAYS, _read_only, atr_facts, bars_at, counterfactual_reason, ledger_census, main, scan_history,
)
from cat.backtest import _snapshot  # noqa: E402
from cat.data import Kline, SymbolFilter  # noqa: E402
from cat.strategy import default_params  # noqa: E402

PEPE_TICK = 1e-9  # Gate gives PEPE_USDT/SHIB_USDT price_precision=9, BTT_USDT/HTX_USDT=10
PARAMS = default_params()


def _bars(count: int, price: float, wiggle: float) -> list[Kline]:
    start = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    step = 4 * 3600
    return [
        Kline(open_time=start + index * step, open=price, high=price + wiggle, low=price - wiggle,
              close=price, base_volume=1000.0, quote_volume=1000.0 * price, closed=True)
        for index in range(count)
    ]


def _snapshot_of(price: float, wiggle: float) -> object:
    bars = _bars(120, price, wiggle)
    filters = SymbolFilter(symbol="PEPE_USDT", price_precision=9, qty_precision=0, min_qty=1.0,
                           min_quote=3.0, max_quote=5_000_000.0, tradable=True, fee_pct=0.2)
    return _snapshot("PEPE_USDT", "4h", bars, None, bars[-1], filters, 0.05, 120.0)


def test_positive_atr_that_rounds_to_zero_is_a_precision_block_not_a_market_fact():
    facts = atr_facts(1.53e-7, 3.11, PARAMS.atr_pct_threshold, PEPE_TICK)
    assert facts["killed_by_rounding"] is True
    assert facts["atr_zero_in_market"] is False
    assert facts["atr_below_venue_tick"] is False  # the venue can express 1.53e-7 at a 1e-9 tick
    assert facts["regime_would_block_after_fix"] is True  # a 3.11% ATR still fails the 1.5% ceiling


def test_atr_smaller_than_the_venue_tick_is_flagged_separately():
    facts = atr_facts(1e-11, 0.3, PARAMS.atr_pct_threshold, PEPE_TICK)
    assert facts["killed_by_rounding"] and facts["atr_below_venue_tick"]


def test_a_truly_flat_series_is_reported_as_the_market_not_as_our_bug():
    facts = atr_facts(0.0, 0.0, PARAMS.atr_pct_threshold, PEPE_TICK)
    assert facts["atr_zero_in_market"] is True
    assert facts["killed_by_rounding"] is False


def test_normal_price_scale_is_untouched_by_the_rule():
    assert not any(atr_facts(703.2331898, 0.87, PARAMS.atr_pct_threshold, 0.1).values())
    assert atr_facts(None, None, PARAMS.atr_pct_threshold, PEPE_TICK)["atr_absent"] is True


def test_scan_counts_one_decision_per_closed_bar_after_the_warmup():
    result = scan_history(_bars(200, 80000.0, 400.0), PARAMS, PEPE_TICK)
    assert result["decision_points"] == 200 - PARAMS.min_bars
    assert result["precision_block_share"] == 0.0
    assert result["counts"].get("killed_by_rounding", 0) == 0


def test_sub_cent_basket_is_blocked_at_every_decision_point():
    result = scan_history(_bars(200, 4.92e-06, 8e-08), PARAMS, PEPE_TICK)
    assert result["precision_block_share"] == 1.0
    assert result["counts"]["killed_by_rounding"] == result["decision_points"]
    # the venue itself could express this ATR, so nothing about the block is a market limit
    assert result["below_venue_tick_share_of_killed"] == 0.0
    assert result["still_regime_blocked_share_of_killed"] == 1.0


def test_counterfactual_uses_the_shipped_gate_and_leaves_no_patch_behind():
    original = strategy_module.analyze
    snapshot = _snapshot_of(4.92e-06, 8e-08)
    with pytest.raises(TypeError):  # the proposal kwargs are deliberately incomplete
        counterfactual_reason(snapshot, PARAMS, {}, 1.53e-7)
    assert strategy_module.analyze is original
    # and the same snapshot through the real path does report the rounding-destroyed ATR
    assert strategy_module.analyze(snapshot, PARAMS).indicators["atr"] == 0.0


def _decision(ts: str, symbol: str, text: str, action: str = "NO_TRADE", approved=None) -> dict:
    row = {"ts": ts, "symbol": symbol, "action": action, "mode": "PAPER",
           "rationale_json": json.dumps([text])}
    if approved is not None:
        row["approved"] = approved
    return row


def test_ledger_census_keeps_the_rounding_bucket_and_the_data_bucket_apart():
    rows = [
        _decision("2026-10-27T00:00:00+00:00", "PEPE_USDT", "no order for action NO_TRADE: no_atr"),
        _decision("2026-10-27T03:00:00+00:00", "HTX_USDT", "untrusted data: source_price_disagreement:1.013%"),
        _decision("2026-10-27T06:00:00+00:00", "HTX_USDT", "untrusted data: source_price_disagreement:0.612%"),
        _decision("2026-10-27T09:00:00+00:00", "BTC_USDT", "no order for action NO_TRADE: regime_filter"),
        _decision("2026-10-27T12:00:00+00:00", "ASTER_USDT", "executed: buy", action="BUY", approved=1),
        # D-035: a row that carries no approval column is not a rejection and not an execution.
        _decision("2026-10-27T15:00:00+00:00", "BNB_USDT", "computed position size rounds to zero",
                  action="BUY"),
    ]
    census = ledger_census(rows)
    assert census["not_evaluable_totals"] == {"no_atr": 1, "source_price_disagreement": 2}
    assert census["not_evaluable_rows_total"] == 3
    assert census["reason_counts_by_symbol"]["BTC_USDT"] == {"regime_filter": 1}
    assert census["reason_counts_by_symbol"]["ASTER_USDT"] == {"executed:buy": 1}
    assert census["reason_counts_by_symbol"]["BNB_USDT"] == {"unverified_action:buy": 1}
    # Neither order-vocabulary label is "not evaluable", so the D-028 verdicts cannot move with it.
    assert classify("unverified_action:buy") == classify("executed:buy") == "cleared_regime_gate"
    assert census["disagreement_spread_by_symbol"]["HTX_USDT"] == {
        "rows": 2, "cycles": 2, "min_pct": 0.612, "median_pct": 0.812, "max_pct": 1.013}
    assert census["cycles_in_ledger"] == 6
    assert census["cycles_with_any_disagreement"] == 2
    assert census["max_disagreeing_symbols_in_one_cycle"] == 1
    assert census["disagreement_by_cycle"]["2026-10-27T03:00:00+00:00"] == {"symbols": 1, "max_pct": 1.013}
    assert census["last_row_ts_by_symbol"]["PEPE_USDT"] == "2026-10-27T00:00:00+00:00"


class _RecordingClient:
    def __init__(self, rows):
        self.rows = rows
        self.params = None

    def get(self, url, params):
        self.params = params
        return self.rows


class _FakeGate:
    base = "https://api.gateio.ws"

    def __init__(self, rows):
        self.client = _RecordingClient(rows)


def test_history_at_a_past_decision_uses_a_bounded_window_and_only_closed_bars():
    at = int(datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc).timestamp())
    step = 4 * 3600
    rows = [[str(at - index * step), "1", "1", "1", "1", "1", "1", "true"] for index in range(60, 0, -1)]
    rows.append([str(at), "1", "1", "1", "1", "1", "1", "false"])  # the in-progress bar
    gate = _FakeGate(rows)
    result = bars_at(gate, "PEPE_USDT", "4h", at)  # type: ignore[arg-type]
    assert result["closed_bars_at_decision"] == 60  # 60 closed rows, the live bar excluded
    assert result["window_days"] == WINDOW_DAYS
    assert gate.client.params == {"currency_pair": "PEPE_USDT", "interval": "4h",
                                  "from": at - WINDOW_DAYS * 86400, "to": at}


class _FakePairGate:
    """Klines and filters only — the basket sweep must not need a quote to answer its question."""

    def __init__(self, prices: dict[str, tuple[float, float, int]]):
        self.prices = prices  # symbol -> (close, wiggle, price_precision)

    def klines(self, symbol: str, timeframe: str, limit: int = 300) -> list[Kline]:
        entry = self.prices[symbol]
        if entry is None:
            raise ValueError(f"no klines for {symbol}")
        close, wiggle, _ = entry
        return _bars(200, close, wiggle)

    def filters(self, symbol: str) -> SymbolFilter:
        if symbol == "GONE_USDT":
            raise ValueError(f"no currency pair for {symbol}")
        precision = self.prices[symbol][2]
        return SymbolFilter(symbol=symbol, price_precision=precision, qty_precision=0, min_qty=1.0,
                            min_quote=3.0, max_quote=5_000_000.0, tradable=True, fee_pct=0.2)


def _fake_cfg():
    cfg = type("Cfg", (), {})()
    cfg.timeframe = "4h"
    cfg.strategy_params = PARAMS
    cfg.symbols = ["PEPE_USDT", "BTC_USDT", "GONE_USDT"]
    return cfg


def test_basket_sweep_counts_the_symbols_the_rounding_rule_blinds():
    from paper_unevaluability_probe import basket_census

    gate = _FakePairGate({"PEPE_USDT": (4.92e-06, 8e-08, 9), "BTC_USDT": (80000.0, 400.0, 1),
                          "GONE_USDT": (1.0, 0.1, 8)})
    gate.prices["GONE_USDT"] = None  # unreachable pair
    result = basket_census(gate, _fake_cfg(), workers=1)
    assert result["symbols"] == 3
    assert result["sizable_count"] == 1
    assert result["unsizable_by_rounding_count"] == 1
    assert [row["symbol"] for row in result["unsizable_symbols"]] == ["PEPE_USDT"]
    assert list(result["unresolved"]) == ["GONE_USDT"]


def _write_store(path: Path, modes: list[str]) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE decisions (ts TEXT, symbol TEXT, action TEXT, "
                       "rationale_json TEXT, mode TEXT)")
    for index, mode in enumerate(modes):
        connection.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?)",
                           (f"2026-10-27T{index:02d}:00:00+00:00", "PEPE_USDT", "NO_TRADE",
                            json.dumps(["no order for action NO_TRADE: no_atr"]), mode))
    connection.commit()
    connection.close()


def test_main_refuses_a_store_that_is_not_paper_only(tmp_path, capsys):
    db = tmp_path / "platform.sqlite3"
    _write_store(db, ["PAPER", "LIVE"])
    code = main(["--db", str(db), "--out", str(tmp_path / "out.json"), "--symbols", "PEPE_USDT"])
    assert code == 3
    assert "non-PAPER" in capsys.readouterr().out
    assert not (tmp_path / "out.json").exists()


def test_the_store_is_opened_read_only(tmp_path):
    db = tmp_path / "platform.sqlite3"
    _write_store(db, ["PAPER"])
    connection = _read_only(db)
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("INSERT INTO decisions VALUES ('x','y','z','[]','PAPER')")
    connection.close()


def test_missing_database_is_a_hard_exit(tmp_path):
    with pytest.raises(SystemExit):
        __import__("paper_unevaluability_probe")._read_only(tmp_path / "absent.sqlite3")
