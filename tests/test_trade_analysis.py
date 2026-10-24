"""Group analysis, trade explanation and hypothesis discovery on recorded evidence."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cat.trade_analysis import (  # noqa: E402
    analyze_groups, discover_hypotheses, explain_trade, group_value, overlap_report,
)

STEP = 3600
START = 1_700_000_000


def _row(pnl: float, *, symbol: str = "BTC/USDT", regime: str = "RANGING", reason: str = "target",
         entry_reason: str = "entry_signal", mfe: float | None = 2.0, mae: float | None = -1.0,
         stop: float | None = 99.0, exit_ts: int | None = None, entry: float = 100.0, qty: float = 0.1,
         holding: float | None = 2.0) -> dict:
    return {
        "symbol": symbol, "timeframe": "1h",
        "entry_time": (exit_ts - int((holding or 0) * STEP)) if exit_ts else START,
        "exit_time": exit_ts or START + int((holding or 0) * STEP),
        "entry": entry, "exit": entry * 1.01, "qty": qty, "pnl": pnl, "fees": 0.0, "reason": reason,
        "evidence": {
            "side": "long", "entry_reason": entry_reason, "decision_time": START, "confidence": 0.8,
            "regime": regime, "volume_state": "normal", "indicators": {}, "bullish": [], "bearish": [],
            "initial_stop": stop, "initial_target": entry * 1.05, "final_stop": stop,
            "final_target": entry * 1.05, "risk_pct": 1.0, "rr_ratio": 3.0, "capital_pct": 5.0,
            "mfe_pct": mfe, "mae_pct": mae, "holding_hours": holding,
        },
    }


def test_explain_trade_separates_code_reason_from_post_hoc_analysis():
    row = _row(-3.0, regime="TRENDING_UP", reason="stop", mfe=2.913, mae=-3.398, stop=99.0,
               entry=103.0, qty=0.75, holding=1.0)
    out = explain_trade(row)

    assert out["code_recorded"]["entry_reason"] == "entry_signal"
    assert out["code_recorded"]["exit_reason"] == "stop"
    assert out["code_recorded"]["regime"] == "TRENDING_UP"
    # pnl -3 on risk qty*(103-99) -> exactly -1R
    assert out["analysis"]["realized_r"] == pytest.approx(-1.0, abs=0.001)
    assert any("استاپ" in line or "R" in line for line in out["analysis"]["lines"])
    assert out["analysis"]["uncertainties"] == []
    assert "پس از معامله" in out["note"]


def test_explain_trade_flags_missing_evidence_instead_of_inventing_it():
    row = _row(1.5)
    row.pop("evidence")
    out = explain_trade(row)
    assert any("ذخیره نشده" in u for u in out["analysis"]["uncertainties"])
    assert out["code_recorded"]["entry_reason"] is None


def test_group_stats_are_hand_computed_and_losing_groups_stay_visible():
    rows = (
        [_row(10.0, regime="TRENDING_UP", exit_ts=START + i * STEP) for i in range(2)]
        + [_row(-5.0, regime="TRENDING_UP", exit_ts=START + 10 * STEP)]
        + [_row(-2.0, regime="TRENDING_DOWN", exit_ts=START + i * STEP) for i in range(6)]
        # 20 ranging trades: 12 wins of +3, 8 losses of -2 -> PF 2.25, expectancy +1.0
        + [_row(3.0 if i % 5 < 3 else -2.0, regime="RANGING", exit_ts=START + i * STEP) for i in range(20)]
    )
    groups = analyze_groups(rows, group_by="regime")
    by_key = {g["key"]: g for g in groups}

    assert set(by_key) == {"TRENDING_UP", "TRENDING_DOWN", "RANGING"}
    up, down, ranging = by_key["TRENDING_UP"], by_key["TRENDING_DOWN"], by_key["RANGING"]
    assert up["n"] == 3 and up["sample_quality"] == "insufficient"
    assert down["n"] == 6 and down["sample_quality"] == "weak"
    assert down["net_pnl"] == pytest.approx(-12.0)
    assert down["profit_factor"] == 0.0
    assert ranging["n"] == 20 and ranging["sample_quality"] == "sufficient"
    assert ranging["win_rate_pct"] == pytest.approx(60.0)
    assert ranging["profit_factor"] == pytest.approx(2.25)
    assert ranging["expectancy"] == pytest.approx(1.0)
    assert ranging["max_consecutive_losses"] == 2  # the win/loss pattern repeats in WWWLL pairs
    assert ranging["best_trade_share_pct"] == pytest.approx(8.33, abs=0.01)
    # ascending by net pnl: the loser is first, never dropped
    assert groups[0]["key"] == "TRENDING_DOWN"
    assert "تأیید شود" in down["guidance"]


def test_group_dimensions_cover_time_and_duration():
    # 1_700_000_000 is a Tuesday 22:13 UTC
    row = _row(1.0, exit_ts=START, holding=0.5)
    assert group_value(row, "weekday")["label"] == "سه‌شنبه"
    assert group_value(row, "hour")["label"] == "ساعت 22 UTC"
    assert group_value(row, "hold_bucket")["label"] == "زیر ۱ ساعت"
    long_row = _row(1.0, exit_ts=START, holding=96.0)
    assert group_value(long_row, "hold_bucket")["label"] == "بیش از ۳ روز"
    with pytest.raises(ValueError):
        analyze_groups([row], group_by="moon_phase")


def test_overlap_report_counts_simultaneous_positions_across_symbols():
    rows = [
        {"symbol": "BTC/USDT", "entry_time": START, "exit_time": START + 10 * STEP,
         "pnl": 1.0, "reason": "target", "evidence": {}},
        {"symbol": "ETH/USDT", "entry_time": START + 5 * STEP, "exit_time": START + 15 * STEP,
         "pnl": -1.0, "reason": "stop", "evidence": {}},
        {"symbol": "SOL/USDT", "entry_time": START + 40 * STEP, "exit_time": START + 41 * STEP,
         "pnl": 0.5, "reason": "target", "evidence": {}},
    ]
    out = overlap_report(rows)
    assert out["max_concurrent"] == 2
    assert out["pairs"] == [{"symbols": "BTC/USDT × ETH/USDT", "overlaps": 1, "overlap_hours": 5.0}]
    assert overlap_report([])["max_concurrent"] == 0


def test_discovery_refuses_to_hypothesize_on_a_tiny_sample():
    out = discover_hypotheses([_row(1.0), _row(-1.0), _row(1.0)])
    assert out["hypotheses"] == []
    assert "کمتر از حداقل نمونه" in out["note"]


def test_discovery_proposes_pre_registered_criteria_for_stop_and_regime_patterns():
    rows = (
        [_row(-2.0, regime="TRENDING_DOWN", reason="stop", mae=-m, exit_ts=START + i * STEP)
         for i, m in enumerate((3.0, 4.0, 3.5, 2.0, 5.0, 3.0))]
        + [_row(1.0, regime="RANGING", reason="target", exit_ts=START + i * STEP) for i in range(6)]
    )
    out = discover_hypotheses(rows)
    ids = {h["id"] for h in out["hypotheses"]}
    assert "h-wider-stop-bracket" in ids
    assert "h-regime-gate" in ids
    # A wider stop with the old 3.5 target can never clear the 1.5 net reward/risk
    # gate, so the paired target must keep the bracket tradeable by construction.
    wider = next(h for h in out["hypotheses"] if h["id"] == "h-wider-stop-bracket")
    stops = wider["grid"]["atr_stop_mult"]
    targets = wider["grid"]["atr_target_mult"]
    assert len(targets) == len(stops)
    for stop_mult, target_mult in zip(stops, targets):
        assert target_mult >= 1.5 * stop_mult + 1.5, (stop_mult, target_mult)
    for hypothesis in out["hypotheses"]:
        assert hypothesis["status"] == "untested"
        assert "بیرون از نمونه" in hypothesis["success_criterion"]
        assert "فرض" in hypothesis["mechanism"]
        assert hypothesis["related_trades"] >= 5


def test_discovery_surfaces_give_back_pattern_from_mfe():
    # pnl +1 on 10 notional = +10% realized, floating peak +20% -> half the peak handed back
    rows = [_row(1.0, mfe=20.0, exit_ts=START + i * STEP) for i in range(8)]
    out = discover_hypotheses(rows)
    assert any(h["id"] == "h-earlier-target" for h in out["hypotheses"])
    assert any("برگشتی" in line for h in out["hypotheses"] for line in h["supporting_evidence"])


def test_discovery_caps_at_three_hypotheses_per_cycle():
    rows = (
        [_row(-2.0, regime="TRENDING_DOWN", reason="stop", mae=-3.0, exit_ts=START + i * STEP) for i in range(8)]
        + [_row(1.0, regime="RANGING", reason="target", mfe=5.0, exit_ts=START + i * STEP) for i in range(8)]
        + [_row(-1.0, regime="TRENDING_UP", reason="stop", mae=-2.0, exit_ts=START + i * STEP) for i in range(8)]
    )
    out = discover_hypotheses(rows)
    assert len(out["hypotheses"]) <= 3
    assert "سه فرضیه" in out["note"]
