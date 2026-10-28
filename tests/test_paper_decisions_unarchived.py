"""The un-replayable symbols can only be judged by what the loop itself recorded.

These tests lock the parts of `paper_decisions_unarchived.py` that could quietly lie:
symbol-name matching between the universe file (`AEROUSDT`) and the store (`AERO_USDT`),
a symbol silently vanishing from a group instead of being reported as unseen, the regime
bucket being the only thing counted as "blocked", and the pre-registered 0.5x-2x band that
D-027 promised to test rather than re-define after seeing the number.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import paper_decisions_unarchived as tool  # noqa: E402


def _decision(ts: str, symbol: str, reason: str, action: str = "NO_TRADE") -> dict:
    return {"ts": ts, "symbol": symbol, "action": action, "mode": "PAPER",
            "rationale_json": json.dumps([f"no order for action {action}: {reason}"])}


def _universe() -> dict:
    return {"entries": [{"binance_symbol": "BTCUSDT"}, {"binance_symbol": "ETHUSDT"}],
            "probe_errors": [{"symbol": "AEROUSDT"}, {"symbol": "AKEUSDT"}]}


def test_groups_come_from_the_file_and_must_not_overlap():
    groups = tool.groups_from_universe(_universe())
    assert groups["unarchived"] == ["AEROUSDT", "AKEUSDT"]
    assert groups["archived"] == ["BTCUSDT", "ETHUSDT"]
    broken = {"entries": [{"binance_symbol": "AERO_USDT"}], "probe_errors": [{"symbol": "AEROUSDT"}]}
    try:
        tool.groups_from_universe(broken)
    except SystemExit as exc:
        assert "both groups" in str(exc)
    else:
        raise AssertionError("an inconsistent universe file must be refused")


def test_underscore_and_plain_spellings_are_the_same_symbol():
    rows = [_decision("2026-10-26T06:27:57+00:00", "AERO_USDT", "regime_filter")]
    out = tool.measure(rows, [], [], tool.groups_from_universe(_universe()))
    group = out["groups"]["unarchived"]
    assert group["symbols_seen_by_loop"] == 1
    assert group["not_seen_by_loop"] == ["AKEUSDT"]
    assert group["per_symbol"]["AEROUSDT"]["cycles"] == 1


def test_a_symbol_the_engine_could_not_evaluate_is_not_an_opportunity():
    ts = "2026-10-26T06:27:57+00:00"
    rows = [
        _decision(ts, "AEROUSDT", "regime_filter"),
        _decision(ts, "AEROUSDT", "insufficient_history"),
        _decision(ts, "AEROUSDT", "no_atr"),
        _decision(ts, "AEROUSDT", "source_price_disagreement"),
        _decision(ts, "AKEUSDT", "regime_filter"),
        _decision(ts, "BTCUSDT", "regime_filter"),
    ]
    out = tool.measure(rows, [], [], tool.groups_from_universe(_universe()))
    un = out["groups"]["unarchived"]
    assert un["symbol_cycles"] == 5
    assert un["cleared_regime_gate"] == 0
    assert un["clear_rate_pct"] == 0.0
    assert un["not_evaluable"] == 3 and un["not_evaluable_pct"] == 60.0
    assert un["blocked_by_regime"] == 2
    assert un["class_counts"]["blocked_by_regime"] == 2
    # a group that only ever fails to be evaluated leaves the band check undefined, not zero
    assert out["prediction"]["inside_band"] is None


def test_only_a_real_pass_counts_as_clearing_the_gate():
    ts = "2026-10-26T06:27:57+00:00"
    assert tool.classify("regime_filter") == "blocked_by_regime"
    assert tool.classify("no_atr") == "not_evaluable"
    assert tool.classify("insufficient_history") == "not_evaluable"
    assert tool.classify("source_price_disagreement") == "not_evaluable"
    assert tool.classify("hold_position") == "cleared_regime_gate"
    assert tool.classify("executed:buy") == "cleared_regime_gate"
    assert tool.classify("risk_rejected") == "other:risk_rejected"
    rows = [_decision(ts, "AEROUSDT", "hold_position"),
            _decision(ts, "AEROUSDT", "regime_filter")]
    out = tool.measure(rows, [], [], tool.groups_from_universe(_universe()))
    assert out["groups"]["unarchived"]["cleared_regime_gate"] == 1


def test_orders_and_closed_trades_are_split_by_group():
    rows = [_decision("2026-10-26T06:27:57+00:00", "BTCUSDT", "regime_filter")]
    orders = [{"symbol": "BTC_USDT"}, {"symbol": "AERO_USDT"}, {"symbol": "AERO_USDT"}]
    trades = [{"symbol": "AEROUSDT"}]
    out = tool.measure(rows, orders, trades, tool.groups_from_universe(_universe()))
    assert out["groups"]["archived"]["orders"] == 1
    assert out["groups"]["unarchived"]["orders"] == 2
    assert out["groups"]["unarchived"]["closed_trades"] == 1


def test_prediction_band_is_evaluated_on_the_measured_ratio():
    ts = "2026-10-26T06:27:57+00:00"
    # unarchived clears 1/4 = 25%, archived clears 1/4 = 25% => ratio 1.0, inside the band
    rows = [
        _decision(ts, "AEROUSDT", "hold_position"), _decision(ts, "AEROUSDT", "regime_filter"),
        _decision(ts, "AKEUSDT", "regime_filter"), _decision(ts, "AKEUSDT", "regime_filter"),
        _decision(ts, "BTCUSDT", "", "BUY"), _decision(ts, "BTCUSDT", "regime_filter"),
        _decision(ts, "ETHUSDT", "regime_filter"), _decision(ts, "ETHUSDT", "regime_filter"),
    ]
    out = tool.measure(rows, [], [], tool.groups_from_universe(_universe()))
    assert out["prediction"]["ratio_unarchived_over_archived"] == 1.0
    assert out["prediction"]["inside_band"] is True


def test_the_not_evaluable_gap_is_a_number_of_its_own():
    ts = "2026-10-26T06:27:57+00:00"
    rows = [
        _decision(ts, "AEROUSDT", "no_atr"), _decision(ts, "AEROUSDT", "hold_position"),
        _decision(ts, "BTCUSDT", "regime_filter"), _decision(ts, "BTCUSDT", "no_atr"),
    ]
    out = tool.measure(rows, [], [], tool.groups_from_universe(_universe()))
    # unarchived fails to evaluate 1/2 = 50%, archived 1/2 = 50% => ratio 1.0
    assert out["not_evaluable_ratio_unarchived_over_archived"] == 1.0


def test_main_refuses_a_store_that_is_not_paper_only(tmp_path, monkeypatch, capsys):
    db = tmp_path / "snap.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE decisions (ts TEXT, symbol TEXT, action TEXT, "
                       "rationale_json TEXT, mode TEXT)")
    connection.execute("CREATE TABLE orders (symbol TEXT)")
    connection.execute("CREATE TABLE trades (symbol TEXT)")
    connection.execute("INSERT INTO decisions VALUES ('2026-10-26T06:27:57+00:00', 'AEROUSDT', "
                       "'BUY', '[]', 'LIVE')")
    connection.commit()
    connection.close()
    universe = tmp_path / "universe.json"
    universe.write_text(json.dumps(_universe()), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["paper_decisions_unarchived.py", "--db", str(db),
                                      "--universe", str(universe),
                                      "--out", str(tmp_path / "out.json")])
    assert tool.main() == tool.EXIT["FAIL"]
    assert "non-PAPER" in capsys.readouterr().out
    assert not (tmp_path / "out.json").exists()


def test_main_writes_the_census(tmp_path, monkeypatch, capsys):
    db = tmp_path / "snap.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE decisions (ts TEXT, symbol TEXT, action TEXT, "
                       "rationale_json TEXT, mode TEXT)")
    connection.execute("CREATE TABLE orders (symbol TEXT)")
    connection.execute("CREATE TABLE trades (symbol TEXT)")
    connection.executemany("INSERT INTO decisions VALUES (?, ?, ?, ?, 'PAPER')", [
        ("2026-10-26T06:27:57+00:00", "AERO_USDT", "NO_TRADE",
         json.dumps(["no order for action NO_TRADE: regime_filter"])),
        ("2026-10-26T09:27:57+00:00", "BTC_USDT", "NO_TRADE",
         json.dumps(["no order for action NO_TRADE: no_atr"])),
    ])
    connection.commit()
    connection.close()
    universe = tmp_path / "universe.json"
    universe.write_text(json.dumps(_universe()), encoding="utf-8")
    out = tmp_path / "nested" / "census.json"
    monkeypatch.setattr(sys, "argv", ["paper_decisions_unarchived.py", "--db", str(db),
                                      "--universe", str(universe), "--out", str(out)])
    assert tool.main() == tool.EXIT["MEASURED"]
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["window"]["distinct_cycles"] == 2
    assert payload["groups"]["unarchived"]["per_symbol"]["AEROUSDT"]["cycles"] == 1
    assert payload["groups"]["archived"]["cleared_regime_gate"] == 0
    assert payload["groups"]["archived"]["not_evaluable"] == 1
    assert "opportunity census" in payload["kind"]
    assert "prediction check" in capsys.readouterr().out
