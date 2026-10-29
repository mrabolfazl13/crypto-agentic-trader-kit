"""Locks for the D-033 deployed-path ceiling probe (read-only analysis of existing replays)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from paper_deployed_ceiling_probe import (  # noqa: E402
    cap_comparison,
    cell_from,
    gap_attribution,
    h1_verdict,
    h2_verdict,
    hold_stats,
    main,
    material_shift,
    protocol_guard,
    reason_census,
    timeline_of,
    verdict,
)

from cat.strategies import default_params  # noqa: E402

UNIVERSE = "cf828076fde8f347"
H0 = default_params().as_dict()
H6 = {**H0, "max_hold_bars": 6, "digest": "fb682ae90697", "label": "trend-pullback:v1@fb682ae90697"}


def _cell_dict(*, timeframe="4h", classifier=None, cadence="production", window=300,
               digest=UNIVERSE, trades=None, schedule=None):
    return {"summary": {"timeframe": timeframe, "cadence": cadence, "window_bars": window,
                        "counted_from": "2026-05-01", "counted_to": "2026-08-01",
                        "symbols_replayed": 2, "classifier": classifier or H0,
                        "lake_version_read": {"universe": digest},
                        "single_account_schedule": schedule or {"trades_per_day": 0.18, "taken": 17,
                                                                "profit_factor": 1.17,
                                                                "net_pnl_per_1000": 1.9,
                                                                "max_drawdown_per_1000": -4.3,
                                                                "max_concurrent": 1, "skipped": {}},
                        "pooled_trades_per_day_upper_bound": 0.45},
            "per_symbol": trades or []}


def _trade(entry: int, hours: float, reason="stop", symbol="AAAUSDT") -> dict:
    return {"symbol": symbol, "entry_time": entry, "exit_time": entry + int(hours * 3600),
            "pnl": 1.0, "fees": 0.05, "reason": reason}


# --- the estimator: one trade is one observation, never one symbol -----------------------------

def test_hold_stats_is_trade_weighted_not_symbol_weighted():
    trades = [_trade(0, 4.0), _trade(0, 4.0), _trade(0, 4.0), _trade(0, 28.0)]
    stats = hold_stats(trades)
    assert stats["trades"] == 4
    assert stats["mean_hours"] == 10.0          # a symbol-weighted mean would say 16.0
    assert stats["ceiling_per_day_at_mean"] == 2.4
    assert stats["share_over_24h_pct"] == 25.0


def test_empty_cell_has_no_ceiling_instead_of_a_zero():
    stats = hold_stats([])
    assert stats["trades"] == 0
    assert stats["mean_hours"] is None
    assert stats["ceiling_per_day_at_mean"] is None


def test_timeline_pools_every_symbol_once():
    per_symbol = [{"symbol": "AAAUSDT", "trade_timeline": [_trade(0, 2.0)]},
                  {"symbol": "BBBUSDT", "trade_timeline": [_trade(0, 6.0), _trade(0, 6.0)]}]
    pooled = timeline_of(per_symbol)
    assert len(pooled) == 3
    assert {row["symbol"] for row in pooled} == {"AAAUSDT", "BBBUSDT"}


def test_truncation_is_counted_separately():
    census = reason_census([_trade(0, 2.0, "stop"), _trade(0, 9.0, "end_of_test")])
    assert census["counts"] == {"stop": 1, "end_of_test": 1}
    assert census["truncation_share_pct"] == 50.0


# --- protocol guard: refuse to reuse a cell that is not the pre-registered one -----------------

def test_guard_accepts_the_shipped_cell():
    assert protocol_guard(_cell_dict()["summary"], expect_hold_bars=0, universe_digest=UNIVERSE) == []


def test_guard_rejects_changed_threshold_wrong_cadence_wrong_warmup_and_wrong_universe():
    for label, summary in (
        ("classifier", _cell_dict(classifier={**H0, "atr_pct_threshold": 3.0})["summary"]),
        ("cadence", _cell_dict(cadence="replay")["summary"]),
        ("window", _cell_dict(window=120)["summary"]),
        ("digest", _cell_dict(digest="deadbeef")["summary"]),
    ):
        problems = protocol_guard(summary, expect_hold_bars=0, universe_digest=UNIVERSE)
        assert problems, label


def test_cell_from_refuses_before_computing_anything(tmp_path):
    path = tmp_path / "frequency-4h.json"
    path.write_text(json.dumps(_cell_dict(classifier={**H0, "vol_threshold": 0.5})), encoding="utf-8")
    try:
        cell_from(path, expect_hold_bars=0, universe_digest=UNIVERSE)
        raise AssertionError("expected SystemExit")
    except SystemExit as error:
        assert "not the pre-registered protocol" in str(error)


# --- verdicts ----------------------------------------------------------------------------------

def test_verdict_boundary_is_reachable_at_exactly_the_requirement():
    assert verdict(1.1768, 1.1768) == "reachable"
    assert verdict(1.1767, 1.1768) == "unreachable"
    assert verdict(None, 1.1768) == "no-observations"


def test_gap_attribution_separates_hold_bound_from_entry_scarce():
    assert gap_attribution({"ceiling_per_day_at_mean": 1.0}, 0.95) == "hold-bound"
    assert gap_attribution({"ceiling_per_day_at_mean": 1.0}, 0.2) == "entry-scarce"
    assert gap_attribution({"ceiling_per_day_at_mean": None}, 0.2) == "undetermined"


def test_material_shift_uses_the_pre_registered_20_percent_rule():
    assert material_shift(1.0, 0.75) is True
    assert material_shift(1.0, 0.85) is False
    assert material_shift(None, 0.85) is None


def _cell(timeframe, mean_hours, trades=10, measured=0.1, median_hours=None):
    median_hours = mean_hours if median_hours is None else median_hours
    stats = {"trades": trades, "mean_hours": mean_hours, "median_hours": median_hours,
             "p75_hours": mean_hours, "p90_hours": mean_hours, "max_hours": mean_hours,
             "share_over_24h_pct": 0.0, "ceiling_per_day_at_mean": 24.0 / mean_hours,
             "ceiling_per_day_at_median": 24.0 / median_hours}
    return {"timeframe": timeframe, "holding": stats,
            "holding_without_window_truncation": stats,
            "measured_trades_per_day_serial": measured,
            "gap_attribution": gap_attribution(stats, measured),
            "serial_schedule": {"trades_per_day": measured, "profit_factor": 0.9,
                                "net_pnl_per_1000": -1.0, "max_drawdown_per_1000": -2.0,
                                "taken": trades, "max_concurrent": 1, "skipped": {}},
            "exit_reasons": {"counts": {"stop": trades}, "truncation_share_pct": 0.0}}


def test_h1_ignores_other_timeframes_and_needs_every_4h_cell_over_a_day():
    cells = {"w1_4h": _cell("4h", 33.0), "w3_4h": _cell("4h", 62.0), "w1_1h": _cell("1h", 10.0)}
    assert h1_verdict(cells, 1.1768)["confirmed"] is True
    assert "w1_1h" not in h1_verdict(cells, 1.1768)["4h_cells"]
    assert h1_verdict({"w1_4h": _cell("4h", 33.0), "w3_4h": _cell("4h", 12.0)},
                      1.1768)["confirmed"] is False


def test_h2_reports_median_and_mean_separately():
    cells = {"w1_4h": _cell("4h", 33.143, median_hours=18.0),
             "w3_4h": _cell("4h", 62.286, median_hours=28.0),
             "w1_1h": _cell("1h", 10.4)}
    out = h2_verdict(cells, 1.1768)
    assert out["unreachable_in_every_4h_cell"] is True
    assert out["per_cell"]["w1_4h"]["verdict_at_median"] == "reachable"
    assert out["per_cell"]["w3_4h"]["verdict_at_median"] == "unreachable"
    assert "w1_1h" not in out["per_cell"]


def test_cap_is_recommended_only_when_every_clause_holds_in_every_cell():
    shipped = {"w1_4h": _cell("4h", 33.143, trades=42), "w3_4h": _cell("4h", 62.286, trades=7)}
    capped = {name: _cell("4h", 16.2, trades=stats["holding"]["trades"] + 26, measured=1.3)
              for name, stats in shipped.items()}
    for cell in capped.values():
        cell["serial_schedule"].update(profit_factor=1.2, net_pnl_per_1000=2.0)
        cell["exit_reasons"] = {"counts": {"exit_horizon": 32, "stop": 5}, "truncation_share_pct": 0.0}
    assert cap_comparison(shipped, capped, 1.1768)["recommend_cap"] is True

    capped["w3_4h"]["serial_schedule"]["net_pnl_per_1000"] = -2.2
    assert cap_comparison(shipped, capped, 1.1768)["recommend_cap"] is False


# --- main --------------------------------------------------------------------------------------

def _write_inputs(tmp_path):
    breadth = tmp_path / "breadth"
    for label, timeframe, mean_hours in (("alw_w1_4h", "4h", 33.143), ("alw_w3_1h", "1h", 10.4)):
        cell_dir = breadth / label
        cell_dir.mkdir(parents=True)
        trades = [_trade(0, mean_hours, symbol="AAAUSDT"),
                  _trade(10 ** 9, mean_hours * 0.2, symbol="BBBUSDT")]
        payload = _cell_dict(timeframe=timeframe, trades=[
            {"symbol": "AAAUSDT", "trade_timeline": trades[:1]},
            {"symbol": "BBBUSDT", "trade_timeline": trades[1:]}])
        (cell_dir / f"frequency-{timeframe}.json").write_text(json.dumps(payload), encoding="utf-8")
    gate_b = tmp_path / "gate_b.json"
    gate_b.write_text(json.dumps({"status": "INCOMPLETE", "evaluated_at": "x",
                                  "cadence": {"required_trades_per_day": 1.1768,
                                              "closed_trades": 0}}), encoding="utf-8")
    d030 = tmp_path / "d030.json"
    d030.write_text(json.dumps({"cadence_ceiling": {"max_hold_days_to_clear_with_one_slot": 0.8498}}),
                    encoding="utf-8")
    d031 = tmp_path / "d031.json"
    d031.write_text(json.dumps({"pooled": {"n": 1869, "mean_days": 0.6716},
                                "censoring": {"n_censored": 866, "n_uncensored": 1003},
                                "one_slot_ceiling": {"at_pooled_mean_per_day": 1.4891}}), encoding="utf-8")
    return breadth, gate_b, d030, d031


def test_main_measures_and_writes_only_the_out_path(tmp_path):
    breadth, gate_b, d030, d031 = _write_inputs(tmp_path)
    out = tmp_path / "results" / "deployed_ceiling.json"
    code = main(["--breadth-dir", str(breadth), "--gate-b", str(gate_b), "--d030", str(d030),
                 "--d031", str(d031), "--out", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["required_per_day"] == 1.1768
    assert payload["h2"]["per_cell"]["w1_4h"]["ceiling_per_day"] == round(24 / ((33.143 + 6.6286) / 2), 4)
    assert payload["sim_path_contrast"]["ceiling_per_day"] == 1.4891
    assert payload["sim_path_contrast"]["censored_share_pct"] == 46.335
    assert sorted(p.name for p in breadth.iterdir()) == ["alw_w1_4h", "alw_w3_1h"]


def test_main_refuses_a_capped_cell_without_a_shipped_twin(tmp_path):
    breadth, gate_b, d030, d031 = _write_inputs(tmp_path)
    capped = tmp_path / "capped" / "alw_w9_4h_hold6"
    capped.mkdir(parents=True)
    (capped / "frequency-4h.json").write_text(
        json.dumps(_cell_dict(classifier=H6, trades=[{"symbol": "AAAUSDT",
                                                      "trade_timeline": [_trade(0, 20.0)]}])),
        encoding="utf-8")
    try:
        main(["--breadth-dir", str(breadth), "--gate-b", str(gate_b), "--d030", str(d030),
              "--d031", str(d031), "--capped-dir", str(tmp_path / "capped"),
              "--out", str(tmp_path / "out.json")])
        raise AssertionError("expected SystemExit")
    except SystemExit as error:
        assert "no shipped twin" in str(error)
    assert not (tmp_path / "out.json").exists()
