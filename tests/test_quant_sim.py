"""Hand-computed correctness tests for the quant execution model.

These do not need the data lake: every bar, signal and fill is chosen so the
expected price, timestamp and PnL can be derived on paper.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from cat.quant.features import (QuantParams, add_indicators, gate_funnel, params_from_dict, resample,
                                signal_frame)
from cat.quant.sim import (SimSettings, SignalTrade, equal_weight_buy_hold, generate_trades,
                           simulate_portfolio)


def bars_1m(closes: list[float], *, start: int = 0, highs: list[float] | None = None,
            lows: list[float] | None = None, opens: list[float] | None = None) -> pd.DataFrame:
    count = len(closes)
    opens = opens or [closes[0]] + closes[:-1]
    highs = highs or [max(o, c) for o, c in zip(opens, closes)]
    lows = lows or [min(o, c) for o, c in zip(opens, closes)]
    return pd.DataFrame({
        "ts": [start + 60 * index for index in range(count)],
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1.0] * count,
    })


def signal(bar_end: int, *, entry: bool = True, exit_: bool = False, atr_pct: float = 0.01,
           complete: bool = True) -> dict:
    return {"bar_end": bar_end, "entry": entry, "exit": exit_, "atr_pct": atr_pct,
            "complete": complete, "close": 100.0}


SETTINGS = SimSettings(initial_capital=1_000.0, fee_pct=0.10, slippage_pct=0.05,
                       position_pct=100.0, max_open_positions=1, max_hold_hours=1.0)


def test_resample_aggregates_closed_buckets_only():
    frame = bars_1m([100.0] * 16)
    frame["high"] = [101.0] * 16
    frame["low"] = [99.0] * 16
    out = resample(frame, "15m")
    assert len(out) == 2
    first = out.iloc[0]
    assert first["bar_end"] == 900 and first["bars"] == 15 and bool(first["complete"])
    assert first["open"] == 100.0 and first["close"] == 100.0 and first["volume"] == 15.0
    assert out.iloc[1]["bars"] == 1 and not bool(out.iloc[1]["complete"])


def test_gap_makes_bucket_incomplete():
    frame = bars_1m([100.0] * 16)
    frame = frame.drop(index=7).reset_index(drop=True)
    out = resample(frame, "15m")
    assert not bool(out.iloc[0]["complete"])


def test_entry_fills_at_open_of_bar_that_starts_when_signal_closes():
    frame = bars_1m([100.0] * 10)
    signals = pd.DataFrame([signal(300)])
    trades, _ = generate_trades(frame, signals, symbol="TESTUSDT", strategy="ema_trend",
                                params=QuantParams(), settings=SETTINGS)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.entry_ts == 300, "a signal at 300 can only fill from 300 onwards"
    assert trade.entry == pytest.approx(100.0 * 1.0005)
    assert trade.exit_ts == 540 and trade.reason == "time"
    assert trade.exit == pytest.approx(100.0 * 0.9995)


def test_never_fills_on_a_missing_execution_bar():
    frame = bars_1m([100.0] * 10)
    frame = frame[frame["ts"] != 300].reset_index(drop=True)
    signals = pd.DataFrame([signal(300)])
    trades, rejections = generate_trades(frame, signals, symbol="TESTUSDT", strategy="ema_trend",
                                         params=QuantParams(), settings=SETTINGS)
    assert trades == [] and rejections.get("missing_execution_bar") == 1


def test_bar_touching_stop_and_target_is_booked_as_stop():
    frame = bars_1m([100.0] * 10)
    frame.loc[2, "high"] = 120.0
    frame.loc[2, "low"] = 80.0
    signals = pd.DataFrame([signal(60), signal(300, entry=False, exit_=True)])
    trades, _ = generate_trades(frame, signals, symbol="TESTUSDT", strategy="ema_trend",
                                params=QuantParams(atr_stop_mult=2.0, atr_target_mult=3.0),
                                settings=SETTINGS)
    trade = trades[0]
    assert trade.reason == "stop"
    assert trade.exit == pytest.approx(trade.stop * 0.9995)


def test_signal_exit_uses_next_bar_open_before_time_exit():
    frame = bars_1m([100.0] * 20)
    signals = pd.DataFrame([signal(60), signal(300, entry=False, exit_=True)])
    trades, _ = generate_trades(frame, signals, symbol="TESTUSDT", strategy="ema_trend",
                                params=QuantParams(), settings=SETTINGS)
    trade = trades[0]
    assert trade.reason == "signal"
    assert trade.exit_ts == 300
    assert trade.exit == pytest.approx(100.0 * 0.9995)


def test_trades_do_not_overlap_on_one_symbol():
    frame = bars_1m([100.0] * 30)
    signals = pd.DataFrame([signal(60), signal(120), signal(900)])
    trades, rejections = generate_trades(frame, signals, symbol="TESTUSDT", strategy="ema_trend",
                                         params=QuantParams(),
                                         settings=SimSettings(max_hold_hours=0.1))
    assert [trade.entry_ts for trade in trades] == [60, 900]
    assert rejections.get("overlapping_position") == 1


def test_portfolio_fee_and_sizing_math():
    candidate = SignalTrade(symbol="TESTUSDT", signal_ts=0, entry_ts=60, exit_ts=3600,
                            entry=100.0, exit=110.0, stop=98.0, target=110.0, reason="target",
                            bars=60, mae_pct=0.02)
    result = simulate_portfolio([candidate], settings=SETTINGS)
    qty = 1_000.0 / (100.0 * 1.001)
    net = qty * 10.0 - qty * 210.0 * 0.001
    assert result["metrics"]["trades"] == 1
    assert result["metrics"]["net_pnl"] == pytest.approx(net, rel=1e-6)
    assert result["metrics"]["final_equity"] == pytest.approx(1_000.0 + net, rel=1e-6)
    assert result["metrics"]["fees"] == pytest.approx(qty * 210.0 * 0.001, rel=1e-6)


def test_portfolio_caps_concurrent_positions_and_keeps_cash_sane():
    settings = SimSettings(initial_capital=1_000.0, fee_pct=0.10, slippage_pct=0.05,
                           position_pct=10.0, max_open_positions=2, max_hold_hours=24.0)
    candidates = [
        SignalTrade(symbol=f"S{index}USDT", signal_ts=0, entry_ts=60, exit_ts=3600,
                    entry=100.0, exit=101.0, stop=99.0, target=102.0, reason="target",
                    bars=60, mae_pct=0.01)
        for index in range(3)
    ]
    result = simulate_portfolio(candidates, settings=settings)
    assert result["metrics"]["trades"] == 2
    assert result["rejections"]["max_open_positions"] == 1
    assert result["metrics"]["final_equity"] <= 1_000.0 + 2 * 10.0 * 1.01
    assert result["metrics"]["exposure_pct"] > 0


def test_buy_hold_starts_after_first_complete_signal_bar():
    frame = bars_1m([100.0] * 20)
    signals = pd.DataFrame([signal(60 + 60 * index, entry=False) for index in range(5)])
    trades, _ = generate_trades(frame, signals, symbol="TESTUSDT", strategy="buy_hold",
                                params=QuantParams(), settings=SETTINGS)
    trade = trades[0]
    assert trade.entry_ts == 60 and trade.exit_ts == 1140 and trade.reason == "end_of_data"
    assert trade.entry == pytest.approx(100.0 * 1.0005)
    assert trade.exit == pytest.approx(100.0 * 0.9995)


def test_realized_equity_does_not_double_count_closed_positions():
    settings = SimSettings(initial_capital=1_000.0, fee_pct=0.0, slippage_pct=0.0,
                           position_pct=50.0, max_open_positions=2, max_hold_hours=24.0)
    candidates = [
        SignalTrade(symbol="AUSDT", signal_ts=0, entry_ts=60, exit_ts=120, entry=100.0, exit=110.0,
                    stop=90.0, target=120.0, reason="signal", bars=2, mae_pct=0.0),
        SignalTrade(symbol="BUSDT", signal_ts=0, entry_ts=60, exit_ts=240, entry=100.0, exit=100.0,
                    stop=90.0, target=120.0, reason="signal", bars=4, mae_pct=0.0),
    ]
    result = simulate_portfolio(candidates, settings=settings)
    # 500 in A (+50 profit), 500 in B (flat): equity after A closes must be 1,050.
    points = {int(ts): equity for ts, equity in result["equity_curve"]}
    assert points[120] == pytest.approx(1_050.0)
    assert points[240] == pytest.approx(1_050.0)
    assert result["metrics"]["final_equity"] == pytest.approx(1_050.0)


def test_equal_weight_benchmark():
    out = equal_weight_buy_hold({"A": (100.0, 110.0), "B": (100.0, 90.0)})
    assert out["symbols"] == 2
    assert out["return_pct"] == pytest.approx(0.0)
    assert out["median_return_pct"] == pytest.approx(0.0)


def test_equal_weight_benchmark_pays_the_same_round_trip_costs():
    out = equal_weight_buy_hold({"A": (100.0, 110.0)}, fee_pct=0.10, slippage_pct=0.05)
    entry = 100.0 * 1.0005
    exit_ = 110.0 * 0.9995
    net = (exit_ - entry - (entry + exit_) * 0.001) / entry
    assert out["return_pct"] == pytest.approx(round(net * 100.0, 4))
    assert out["gross_return_pct"] == pytest.approx(10.0, rel=1e-6)
    assert out["return_pct"] < out["gross_return_pct"]


def test_params_reject_out_of_bounds_and_inverted_emas():
    with pytest.raises(ValueError):
        params_from_dict(overrides={"atr_stop_mult": 99})
    with pytest.raises(ValueError):
        params_from_dict(overrides={"ema_fast": 60, "ema_slow": 20})
    with pytest.raises(ValueError):
        params_from_dict(overrides={"not_a_field": 1})
    # D-011: a reward:risk floor was measured, made the aggregate worse, and was reverted.
    tight = params_from_dict(overrides={"atr_stop_mult": 3.5, "atr_target_mult": 1.0})
    assert tight.atr_target_mult == 1.0 and tight.atr_stop_mult == 3.5


def test_signal_frame_has_no_signal_during_warmup():
    frame = bars_1m([100.0 + index * 0.1 for index in range(200)])
    signals = signal_frame(resample(frame, "15m"), "ema_trend", QuantParams(ema_slow=48))
    warmup = QuantParams(ema_slow=48).warmup_bars()
    assert not signals["entry"].iloc[: min(warmup, len(signals))].any()
    assert not signals.loc[signals["ema_slow"].isna(), "entry"].any()
    assert signals["atr_pct"].iloc[: QuantParams().atr_period - 1].isna().all()


def test_indicators_are_finite_after_warmup():
    frame = bars_1m([100.0 + (index % 7) * 0.5 for index in range(3600)])
    df = add_indicators(resample(frame, "1h"), QuantParams())
    tail = df.iloc[QuantParams().warmup_bars():]
    assert len(tail) >= 5
    assert np.isfinite(tail[["ema_fast", "ema_slow", "rsi", "atr", "momentum", "volume_ratio"]].to_numpy()).all()


# ---------------------------------------------------------- signal diagnostics

def test_gate_funnel_is_cumulative_and_ends_at_the_entry_count():
    frame = bars_1m([100.0 * (1.0 + index / 5000.0) for index in range(6000)])
    signals = signal_frame(resample(frame, "1h"), "ema_trend", QuantParams())
    gates = gate_funnel(signals, "ema_trend", QuantParams())
    names = [step["gate"] for step in gates["funnel"]]
    counts = [step["passing"] for step in gates["funnel"]]
    assert names == ["ready", "trend", "close_above_ema_slow", "momentum_above_threshold"]
    assert counts == sorted(counts, reverse=True), "each conjunct can only remove samples"
    assert counts[0] == gates["ready_bars"]
    assert counts[-1] == gates["entry_signals"]
    assert gates["entry_signals"] > 0
    assert gates["conditions"]["close_above_ema_slow"] >= counts[2]


def test_gate_funnel_explains_a_zero_trade_market_instead_of_hiding_it():
    frame = bars_1m([100.0] * 6000)
    signals = signal_frame(resample(frame, "1h"), "ema_trend", QuantParams())
    gates = gate_funnel(signals, "ema_trend", QuantParams())
    assert gates["ready_bars"] > 0, "warm-up must not be blamed for a flat market"
    assert gates["entry_signals"] == 0
    funnel = {step["gate"]: step["passing"] for step in gates["funnel"]}
    assert funnel["trend"] == 0 and funnel["momentum_above_threshold"] == 0


# ------------------------------------------------- cost-aware and risk-based entries

def test_round_trip_cost_covers_both_legs_and_both_frictions():
    assert SimSettings(fee_pct=0.10, slippage_pct=0.05).round_trip_cost_pct() == pytest.approx(0.30)


def test_target_inside_the_cost_band_is_refused_and_counted():
    """A 0.4% target against a 0.3% round trip cannot pay for itself twice over."""
    frame = bars_1m([100.0] * 10)
    signals = pd.DataFrame([signal(300, atr_pct=0.001)])
    off, _ = generate_trades(frame, signals, symbol="TESTUSDT", strategy="ema_trend",
                             params=QuantParams(), settings=SETTINGS)
    assert len(off) == 1, "the switch must be off by default so old results reproduce"

    strict = SimSettings(initial_capital=1_000.0, fee_pct=0.10, slippage_pct=0.05,
                         position_pct=100.0, max_open_positions=1, max_hold_hours=1.0,
                         min_edge_cost_ratio=2.0)
    trades, rejections = generate_trades(frame, signals, symbol="TESTUSDT", strategy="ema_trend",
                                         params=QuantParams(), settings=strict)
    assert trades == []
    assert rejections.get("target_inside_cost_band") == 1


def test_cost_band_allows_a_target_that_clears_the_costs():
    frame = bars_1m([100.0] * 10)
    signals = pd.DataFrame([signal(300, atr_pct=0.02)])  # 2% ATR -> 6% target
    strict = SimSettings(initial_capital=1_000.0, fee_pct=0.10, slippage_pct=0.05,
                         position_pct=100.0, max_open_positions=1, max_hold_hours=1.0,
                         min_edge_cost_ratio=2.0)
    trades, rejections = generate_trades(frame, signals, symbol="TESTUSDT", strategy="ema_trend",
                                         params=QuantParams(), settings=strict)
    assert len(trades) == 1 and "target_inside_cost_band" not in rejections


def test_risk_sizing_makes_a_stop_out_cost_the_same_share_of_equity():
    """1% risk with a 5% stop must buy 2 units of a 100 entry, not the whole account."""
    candidate = SignalTrade(symbol="TESTUSDT", signal_ts=0, entry_ts=60, exit_ts=3600,
                            entry=100.0, exit=95.0, stop=95.0, target=110.0, reason="stop",
                            bars=60, mae_pct=0.05)
    sized = SimSettings(initial_capital=1_000.0, fee_pct=0.10, slippage_pct=0.05,
                        position_pct=100.0, max_open_positions=1, max_hold_hours=24.0,
                        risk_per_trade_pct=1.0)
    result = simulate_portfolio([candidate], settings=sized)
    trade = result["trades"][0]
    assert trade["qty"] == pytest.approx(2.0, rel=1e-9)
    fees = 2.0 * (100.0 + 95.0) * 0.001
    assert trade["net"] == pytest.approx(-10.0 - fees, rel=1e-6)
    assert result["metrics"]["net_return_pct"] == pytest.approx((-10.0 - fees) / 10.0, rel=1e-6)


def test_risk_sizing_never_exceeds_the_notional_cap():
    """A very tight stop must not turn equal-risk sizing into an oversized position."""
    candidate = SignalTrade(symbol="TESTUSDT", signal_ts=0, entry_ts=60, exit_ts=3600,
                            entry=100.0, exit=101.0, stop=99.9, target=110.0, reason="target",
                            bars=60, mae_pct=0.001)
    settings = SimSettings(initial_capital=1_000.0, fee_pct=0.10, slippage_pct=0.05,
                           position_pct=5.0, max_open_positions=1, max_hold_hours=24.0,
                           risk_per_trade_pct=1.0)
    result = simulate_portfolio([candidate], settings=settings)
    trade = result["trades"][0]
    assert trade["qty"] * 100.0 <= 1_000.0 * 0.05 + 1e-9


def test_risk_sizing_falls_back_to_notional_when_there_is_no_stop():
    """buy_hold carries stop=0; it must still be sized instead of rejected."""
    candidate = SignalTrade(symbol="TESTUSDT", signal_ts=0, entry_ts=60, exit_ts=3600,
                            entry=100.0, exit=110.0, stop=0.0, target=0.0, reason="end_of_data",
                            bars=60)
    settings = SimSettings(initial_capital=1_000.0, fee_pct=0.10, slippage_pct=0.05,
                           position_pct=100.0, max_open_positions=1, max_hold_hours=24.0,
                           risk_per_trade_pct=1.0)
    result = simulate_portfolio([candidate], settings=settings)
    assert result["metrics"]["trades"] == 1


# ------------------------------------------------- honest mark to market

def _marks(prices: dict[int, float]) -> dict[str, tuple[Any, Any]]:
    timestamps = np.array(sorted(prices), dtype="int64")
    closes = np.array([prices[int(ts)] for ts in timestamps], dtype="float64")
    return {"TESTUSDT": (timestamps, closes)}


def test_mark_to_market_reveals_a_drawdown_that_realized_pnl_hides():
    """A position that halves and recovers to break-even is a 50% drawdown, not 0%."""
    candidate = SignalTrade(symbol="TESTUSDT", signal_ts=0, entry_ts=60, exit_ts=3600,
                            entry=100.0, exit=100.0, stop=0.0, target=0.0, reason="end_of_data",
                            bars=60)
    settings = SimSettings(initial_capital=1_000.0, fee_pct=0.0, slippage_pct=0.0,
                           position_pct=100.0, max_open_positions=1, max_hold_hours=24.0)
    flat = simulate_portfolio([candidate], settings=settings)
    assert flat["metrics"]["drawdown_basis"] == "cost_basis"
    assert flat["metrics"]["max_drawdown_pct"] == pytest.approx(0.0)

    marked = simulate_portfolio(
        [candidate], settings=settings,
        marks=_marks({60: 100.0, 1800: 50.0, 3600: 100.0}),
    )
    assert marked["metrics"]["drawdown_basis"] == "mark_to_market"
    assert marked["metrics"]["mark_fallbacks"] == 0
    assert marked["metrics"]["max_drawdown_pct"] == pytest.approx(50.0, abs=1e-6)
    # The realized result is unchanged: marks change the *measurement*, not the PnL.
    assert marked["metrics"]["net_return_pct"] == pytest.approx(flat["metrics"]["net_return_pct"])


def test_a_mark_missing_for_a_symbol_falls_back_to_cost_and_says_so():
    candidate = SignalTrade(symbol="TESTUSDT", signal_ts=0, entry_ts=60, exit_ts=3600,
                            entry=100.0, exit=110.0, stop=95.0, target=110.0, reason="target",
                            bars=60)
    settings = SimSettings(initial_capital=1_000.0, fee_pct=0.0, slippage_pct=0.0,
                           position_pct=100.0, max_open_positions=1, max_hold_hours=24.0)
    result = simulate_portfolio([candidate], settings=settings, marks={"OTHERUSDT": (
        np.array([60, 3600], dtype="int64"), np.array([1.0, 1.0], dtype="float64"))})
    assert result["metrics"]["drawdown_basis"] == "mark_to_market"
    assert result["metrics"]["mark_fallbacks"] >= 1
    assert result["metrics"]["max_drawdown_pct"] == pytest.approx(0.0), "cost basis, not an invention"


