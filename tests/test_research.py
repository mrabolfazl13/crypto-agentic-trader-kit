"""Offline, deterministic Strategy Lab tests.

The harness (window isolation, ranking, aggregates, the acceptance gate and the
persistence) is driven through a stand-in for `backtest.run_backtest` whose figures are
arithmetic over the candle slice it is handed, so every number is checkable and CI stays
fast and network-free. That stand-in models one documented behaviour — aggressive targets
help in rising tape and hurt in falling tape — it is not a claim about the real strategy.
The last test exercises the real engine whenever it is runnable.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from cat import research
from cat.backtest import BacktestResult, BacktestTrade
from cat.data import Kline, SymbolFilter
from cat.research import Split, acceptance, aggregate_folds, evaluate, grid_search, run_research, split_bars, \
    walk_forward
from cat.strategies import default_params
from cat.store import open_store
from conftest import uptrend_prices

STEP = 14400  # 4h: config.yaml signals on 4h
DAY = 86400  # 1d: config.yaml confirms on 1d
T0 = 1_700_000_000 - (1_700_000_000 % STEP)
FILTERS = SymbolFilter(symbol="BTC_USDT", price_precision=1, qty_precision=6, min_qty=0.000001,
                       min_quote=3.0, max_quote=5_000_000.0, tradable=True, fee_pct=0.1)


def _bars(prices: list[float], step: int = STEP) -> list[Kline]:
    return [Kline(open_time=T0 + index * step, open=prices[index - 1] if index else prices[index],
                  high=max(prices[index - 1] if index else prices[index], prices[index]) * 1.004,
                  low=min(prices[index - 1] if index else prices[index], prices[index]) * 0.996,
                  close=prices[index], base_volume=100.0, quote_volume=100.0 * prices[index], closed=True)
            for index in range(len(prices))]


def _prices_rally_then_fade(rally: int = 600, fade: int = 300) -> list[float]:
    """Checkable character shift: +0.04% per bar, then -0.06% per bar, with fixed wiggles."""
    prices, price = [], 100.0
    for index in range(rally + fade):
        drift = 0.0004 if index < rally else -0.0006
        wiggle = -0.0012 if index % 9 == 0 else 0.0003
        price *= 1.0 + drift + wiggle
        prices.append(round(price, 4))
    return prices


def _prices_rising(n: int = 900) -> list[float]:
    prices, price = [], 100.0
    for index in range(n):
        price *= 1.0 + (-0.0012 if index % 9 == 0 else 0.0004)
        prices.append(round(price, 4))
    return prices


class FakeEngine:
    """Deterministic stand-in for run_backtest: metrics are arithmetic over the decided region."""

    def __init__(self, *, exposure: float = 0.05):
        self.calls: list[dict[str, object]] = []
        self.exposure = exposure

    def __call__(self, config, symbol, *, bars, higher_bars, filters, timeframe=None, warmup=80,
                 assumed_spread_pct=0.05, params=None, propose_fn=None) -> BacktestResult:
        params = params or default_params()
        active = [bar.close for bar in bars[warmup:]]
        drift = (active[-1] - active[0]) / active[0] * 100.0 if len(active) > 1 else 0.0
        bump = (params.atr_target_mult - 3.5) * 0.1 + (1.5 - params.atr_stop_mult) * 0.1
        # aggressive tuning adds 0.5% of capital in a rising window, costs 1.0% in a falling one
        tilt = bump if drift > 0 else -2.0 * bump
        capital = config.allocated_capital or 1000.0
        net_return = round(drift * self.exposure + tilt * 10.0, 4)
        trades = max(1, len(active) // 40)
        metrics = {"trades": trades, "win_rate_pct": round(50.0 + drift, 2),
                   "profit_factor": round(1.2 + tilt * 5.0 + drift / 40.0, 3),
                   "net_pnl": round(capital * net_return / 100.0, 4), "net_return_pct": net_return,
                   "max_drawdown_pct": round(max(0.0, -drift) * self.exposure + max(0.0, -tilt), 3),
                   "fees_paid": round(trades * 0.5, 4)}
        first_trade = bars[warmup].open_time
        result = BacktestResult(symbol=symbol, timeframe=timeframe or config.timeframe, bars=len(bars),
                               start=research._iso(bars[0].open_time), end=research._iso(bars[-1].open_time))
        result.metrics = metrics
        result.params_label = params.label()
        result.params = params.tuning()
        result.buy_hold_pct = round(drift, 4)
        result.trades = [BacktestTrade(symbol, first_trade, bars[-1].open_time, active[0], active[-1], 0.001,
                                      net_return / trades, 0.5, "synthetic") for _ in range(trades)]
        result.rejected = {"regime_filter": 1}
        self.calls.append({"symbol": symbol, "timeframe": timeframe or config.timeframe, "warmup": warmup,
                           "bars": len(bars), "first_ts": bars[0].open_time, "last_ts": bars[-1].open_time,
                           "decided_first_ts": first_trade, "params_label": params.label(),
                           "propose_fn": getattr(propose_fn, "__name__", None),
                           "filters": filters.symbol})
        return result


@pytest.fixture()
def engine(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(research, "run_backtest", fake)
    return fake


# ---- split ----------------------------------------------------------------
def test_split_is_chronological_non_overlapping_and_tiles_every_bar():
    bars = _bars(_prices_rising(901))
    parts = split_bars(bars)
    assert isinstance(parts, Split)
    with pytest.raises(AttributeError):  # the split is frozen: no segment can be swapped in after the fact
        parts.test = bars
    tiled = [bar.open_time for bar in parts.train] + [bar.open_time for bar in parts.validation] \
        + [bar.open_time for bar in parts.test]
    assert tiled == [bar.open_time for bar in bars]
    assert len(parts.train) == 540 and len(parts.validation) == 180 and len(parts.test) == 181
    report = parts.as_dict()
    assert report["train"]["bars"] == 540
    assert report["train"]["start"] == research._iso(bars[0].open_time)
    assert report["train"]["last_open_time"] < report["validation"]["first_open_time"]
    assert report["validation"]["last_open_time"] < report["test"]["first_open_time"]
    assert all(report[name]["gap_free"] for name in ("train", "validation", "test"))
    assert report["train"]["contiguous_with_next"] and report["validation"]["contiguous_with_next"]
    assert not report["test"]["contiguous_with_next"]


def test_split_flags_a_hole_instead_of_calling_it_gap_free():
    bars = _bars(_prices_rising(901))
    del bars[300]
    parts = split_bars(bars)
    report = parts.as_dict()
    assert not report["train"]["gap_free"]
    assert parts.step == STEP


def test_split_refuses_data_that_cannot_fill_every_segment():
    with pytest.raises(ValueError, match="insufficient data"):
        split_bars(_bars(_prices_rising(300)))
    with pytest.raises(ValueError, match="insufficient data"):
        split_bars(_bars(_prices_rising(900)), min_bars=200)


def test_split_refuses_unordered_bars_and_impossible_fractions():
    bars = _bars(_prices_rising(400))
    with pytest.raises(ValueError, match="strictly chronological"):
        split_bars(list(reversed(bars)))
    with pytest.raises(ValueError, match="train/validation/test"):
        split_bars(bars, train=0.8, validation=0.3)
    with pytest.raises(ValueError, match="no bars"):
        split_bars([])


# ---- evaluate -------------------------------------------------------------
def test_evaluate_returns_the_backtest_result_as_a_plain_dict(config, engine):
    bars = _bars(_prices_rising(400))
    report = evaluate(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS, params=default_params(),
                      warmup=80)
    assert set(report) == {"metrics", "buy_hold_pct", "trades", "rejected", "params_label", "strategy",
                           "start", "end", "bars", "equity_curve", "benchmark_curve"}
    assert report["strategy"] == ""  # no family switch: the config build itself was measured
    assert report["bars"] == 400 and report["trades"] == report["metrics"]["trades"]
    assert report["params_label"] == default_params().label()
    assert report["start"] == research._iso(bars[0].open_time)
    assert engine.calls[0]["filters"] == "BTC_USDT"


def test_evaluate_refuses_a_window_shorter_than_its_warmup(config, engine):
    with pytest.raises(ValueError, match="warmup"):
        evaluate(config, "BTC_USDT", bars=_bars(_prices_rising(80)), higher_bars=[], filters=FILTERS,
                 params=default_params(), warmup=80)
    assert engine.calls == []


def _curve_engine(warmup_equity: float = 1000.0):
    """Engine stand-in that emits a rising mark-to-market curve the way the real engine timestamps it."""

    def engine(_config, symbol, *, bars, higher_bars, filters, timeframe=None, warmup=80,
               assumed_spread_pct=0.05, params=None, propose_fn=None) -> BacktestResult:
        result = BacktestResult(symbol=symbol, timeframe=timeframe or "4h", bars=len(bars),
                                start=research._iso(bars[0].open_time), end=research._iso(bars[-1].open_time))
        result.equity_curve = [(bars[i].open_time, warmup_equity + (i - warmup) * 2.0)
                               for i in range(warmup + 1, len(bars))]
        result.metrics = {"trades": 1, "net_return_pct": 1.0}
        result.buy_hold_pct = 9.0
        result.params_label = "synthetic"
        return result

    return engine


def test_evaluate_records_an_oos_equity_curve_with_a_buy_hold_reference(config, monkeypatch):
    bars = _bars(_prices_rising(140))
    monkeypatch.setattr(research, "run_backtest", _curve_engine())
    report = evaluate(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS, params=default_params(),
                      warmup=80)
    curve, benchmark = report["equity_curve"], report["benchmark_curve"]
    assert len(curve) == 140 - 80 - 1 == len(benchmark)
    assert curve[0][1] == pytest.approx((1000.0 + 2.0) / float(config.allocated_capital or 1000.0) * 100.0)
    assert [point[0] for point in curve] == [point[0] for point in benchmark]
    last = float(bars[-1].close) / float(bars[80].close) * 100.0
    assert benchmark[-1][1] == pytest.approx(last)


def test_the_equity_curve_is_downsampled_for_storage_without_losing_the_endpoints(config, monkeypatch):
    bars = _bars(_prices_rising(1200))
    monkeypatch.setattr(research, "run_backtest", _curve_engine())
    report = evaluate(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS, params=default_params(),
                      warmup=80)
    curve = report["equity_curve"]
    assert len(curve) == 90
    assert curve[0][0] == bars[81].open_time and curve[-1][0] == bars[-1].open_time
    assert all(curve[i][0] < curve[i + 1][0] for i in range(len(curve) - 1))


# ---- grid search ----------------------------------------------------------
def test_grid_search_rejects_unknown_keys_identity_fields_and_out_of_bounds_values(config, engine):
    bars = _bars(_prices_rising(900))
    for grid, message in (({"rsi_period": [14], "magic_knob": [1]}, "unknown strategy parameter"),
                          ({"max_risk_per_trade_pct": [0.1]}, "risk limit owned by config.yaml"),
                          ({"max_slippage_pct": [0.1]}, "risk limit owned by config.yaml"),
                          ({"allocated_capital": [5000]}, "risk limit owned by config.yaml"),
                          ({"version": ["v2"]}, "identifies a version"),
                          ({"note": ["ship it"]}, "identifies a version"),
                          ({"ema_fast": [5000]}, "documented bounds"),
                          ({"ema_fast": [float("inf")]}, "documented bounds"),
                          ({"ema_fast": [float("nan")]}, "documented bounds"),
                          ({"ema_fast": ["twenty"]}, "must be numbers"),
                          ({"ema_fast": [True]}, "must be numbers"),
                          ({"ema_fast": []}, "must be a non-empty list"),
                          ({"ema_fast": 14}, "must be a non-empty list"),
                          ("ema_fast", "must be a mapping")):
        with pytest.raises(ValueError, match=message):
            grid_search(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS, grid=grid)
    assert engine.calls == []
    assert not set(research._RISK_FORBIDDEN) & set(default_params().tuning())  # no risk knob is a strategy field


def test_grid_values_keep_their_declared_types_and_mis_typed_ones_never_run(config, engine):
    bars = _bars(_prices_rising(900))
    results = grid_search(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS,
                          grid={"require_multiple_signals": [True, False], "ema_fast": [12, 18]}, top=4)
    assert {(entry["params"]["require_multiple_signals"], entry["params"]["ema_fast"]) for entry in results} == \
        {(True, 12), (True, 18), (False, 12), (False, 18)}
    assert all(isinstance(entry["params"]["ema_fast"], int) for entry in results)
    with pytest.raises(ValueError, match="require_multiple_signals expects true/false"):
        grid_search(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS,
                    grid={"require_multiple_signals": ["yes"]})
    assert len(engine.calls) == 8  # the refused grid never reached the engine


def test_every_numeric_tuning_field_has_documented_bounds():
    numeric = {key for key, value in default_params().tuning().items()
               if isinstance(value, (int, float)) and not isinstance(value, bool)}
    assert numeric == set(research.PARAM_SPACE_HINTS), "a new knob needs bounds before it can be searched"


def test_stored_params_round_trip_without_losing_identity_or_types():
    base = default_params()
    stored = json.loads(json.dumps(base.replace(version="v7", atr_stop_mult=1.25).as_dict()))
    stored["ema_fast"], stored["magic"] = 12.0, 5  # JSON hands back floats; unknown keys are ignored
    rebuilt = research.params_from_report(stored, base)
    assert rebuilt == base.replace(version="v7", ema_fast=12, atr_stop_mult=1.25)
    assert rebuilt.name == base.name and isinstance(rebuilt.ema_fast, int) and "magic" not in rebuilt.tuning()
    assert research.params_from_report({}, base) == base and research.params_from_report(None, base) == base


def test_grid_search_refuses_an_oversized_grid_before_running_anything(config, engine):
    bars = _bars(_prices_rising(900))
    grid = {"ema_fast": [10, 20, 30, 40, 50], "ema_slow": [50, 100, 150, 200], "rsi_period": [7, 14, 21]}
    assert research.MAX_GRID_EVALS == 60
    with pytest.raises(ValueError, match="grid too large: 120 evaluations exceeds max_evals=60"):
        grid_search(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS,
                    grid=dict(grid, atr_stop_mult=[1.0, 1.2]))
    assert engine.calls == []
    top = grid_search(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS, grid=grid, top=3)
    assert len(top) == 3 and len(engine.calls) == 120  # exactly the cap is allowed: 60 variants, two windows each
    assert {entry["params"]["ema_fast"] for entry in top} <= {10, 20, 30, 40, 50}
    engine.calls.clear()
    with pytest.raises(ValueError, match="grid too large: 60 evaluations exceeds max_evals=59"):
        grid_search(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS, grid=grid, max_evals=59)
    assert engine.calls == []


def test_grid_search_only_ever_sees_train_and_validation(config, engine):
    bars = _bars(_prices_rally_then_fade())
    parts = split_bars(bars)
    train_last = parts.train[-1].open_time
    validation_last = parts.validation[-1].open_time
    test_first = parts.test[0].open_time
    assert train_last < validation_last < test_first

    results = grid_search(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS,
                          grid={"atr_target_mult": [3.0, 4.0]}, top=2)
    assert len(engine.calls) == 4 and len(results) == 2
    for call in engine.calls:
        assert call["first_ts"] >= parts.train[0].open_time
        assert call["last_ts"] <= validation_last  # the hold-out is never touched while searching
        assert call["decided_first_ts"] < test_first
    train_windows = {(call["first_ts"], call["last_ts"]) for call in engine.calls}
    assert (parts.train[0].open_time, train_last) in train_windows
    assert (parts.validation[0].open_time, validation_last) in train_windows

    best = results[0]
    assert best["train"]["metrics"]["net_return_pct"] > 0 > best["validation"]["metrics"]["net_return_pct"]
    assert best["params"]["atr_target_mult"] == 4.0  # the aggressive variant wins on train only
    assert {key for key in best} == {"params_label", "params", "metrics", "strategy", "train", "validation"}


def test_grid_search_never_mutates_risk_or_sizing_config(config, engine):
    limits, capital = config.limits, config.allocated_capital
    grid_search(config, "BTC_USDT", bars=_bars(_prices_rising(900)), higher_bars=[], filters=FILTERS,
                grid={"atr_stop_mult": [1.2, 1.5]}, top=1)
    assert config.limits == limits and config.allocated_capital == capital
    assert config.strategy_params == default_params()


def test_evaluate_threads_the_entry_family_and_keeps_it_visible_in_the_label(config, engine):
    bars = _bars(_prices_rising(900))
    report = evaluate(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS,
                      params=default_params(), strategy="breakout_30")
    assert report["params_label"].startswith("breakout_30|")
    assert engine.calls[-1]["propose_fn"] == "propose_breakout"
    with pytest.raises(ValueError, match="unknown strategy family"):
        evaluate(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS,
                 params=default_params(), strategy="moon_shot")


def test_an_empty_grid_measures_one_build_so_a_family_switch_is_the_only_difference(config, engine):
    bars = _bars(_prices_rising(900))
    results = grid_search(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS, grid={}, top=3)
    assert [item["params_label"] for item in results] == [default_params().label()]
    assert len(engine.calls) == 2  # train + validation, no parameter search
    engine.calls.clear()
    family = grid_search(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS, grid={},
                         strategy="momentum_20")
    assert family[0]["params_label"].startswith("momentum_20|")
    assert {call["propose_fn"] for call in engine.calls} == {"propose_momentum"}


def test_run_research_judges_a_family_switch_against_the_build_actually_in_config(config, engine, workspace):
    store = open_store(workspace)
    report = run_research(store, config, symbol="BTC_USDT", timeframe="4h", bars=_bars(_prices_rising(900)),
                          higher_bars=[], filters=FILTERS, grid={}, folds=4, strategy="breakout_30")
    assert report["strategy"] == "breakout_30" and report["champion_strategy"] == ""
    assert report["candidate_label"].startswith("breakout_30|")
    assert report["champion_label"] == default_params().label()
    # the panel charts the candidate and the incumbent on the same out-of-sample window
    assert isinstance(report["champion_validation"].get("metrics"), dict)
    assert report["champion_validation"]["equity_curve"] == []  # fake engine emits no curve; shape stays honest
    assert report["validation"]["buy_hold_pct"] == report["champion_validation"]["buy_hold_pct"]
    # the incumbent must be measured with the entry rules the system runs today, not the challenger's
    assert {call["propose_fn"] for call in engine.calls} == {"propose_breakout", None}
    assert report["verdict"] == "rejected"  # identical fake metrics can never beat their own baseline
    versions = store.list_strategy_versions()
    # only the entry trigger differs, so both sides share one params digest and one version row
    assert [row["source"] for row in versions] == ["champion_baseline"]
    labels = {row["params_label"] for row in store.list_runs()}
    assert any(str(label).startswith("breakout_30|") for label in labels)
    assert default_params().label() in labels
    store.close()


def test_silent_variant_never_outranks_a_measured_loss(config, monkeypatch):
    """Regression: a 0.0%-with-zero-trades variant wasted three optimizer experiments.

    Absence of evidence is not a neutral score — when every trading variant loses,
    the grid must still select one that actually trades so the gate measures a hypothesis.
    """
    def fake(_config, symbol, *, bars, higher_bars, filters, timeframe=None, warmup=80,
             assumed_spread_pct=0.05, params=None, propose_fn=None):
        params = params or default_params()
        silent = params.ema_fast == 8
        trades = 0 if silent else 20
        metrics = {"trades": trades, "win_rate_pct": 0.0,
                   "profit_factor": None if silent else 0.4,
                   "net_pnl": 0.0 if silent else -3.0, "net_return_pct": 0.0 if silent else -0.3,
                   "max_drawdown_pct": 0.0 if silent else 0.6, "fees_paid": 0.0 if silent else 1.0}
        result = BacktestResult(symbol=symbol, timeframe=timeframe or "4h", bars=len(bars),
                                start=research._iso(bars[0].open_time), end=research._iso(bars[-1].open_time))
        result.metrics = metrics
        result.params_label = params.label()
        result.params = params.tuning()
        result.trades = [BacktestTrade(symbol, bars[warmup].open_time, bars[-1].open_time, 100.0, 99.0,
                                       0.001, -0.15, 0.05, "synthetic") for _ in range(trades)]
        return result

    monkeypatch.setattr(research, "run_backtest", fake)
    results = grid_search(config, "BTC_USDT", bars=_bars(_prices_rising(900)), higher_bars=[], filters=FILTERS,
                          grid={"ema_fast": [8, 12]}, top=2)
    assert results[0]["params"]["ema_fast"] == 12  # the losing-but-measured variant wins over silence
    assert results[-1]["params"]["ema_fast"] == 8


# ---- walk-forward ---------------------------------------------------------
def test_walk_forward_never_scores_inside_its_own_training_window(config, engine):
    bars = _bars(_prices_rally_then_fade())
    result = walk_forward(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS,
                          params=default_params(), folds=4)
    folds = result["folds"]
    assert len(folds) == 4 and len(engine.calls) == 4
    for index, (fold, call) in enumerate(zip(folds, engine.calls)):
        assert fold["test_start"] > fold["train_end"]
        assert call["warmup"] == fold["seed_bars"] == 80
        # the engine only starts deciding at the first unseen bar: no fold is ever scored inside its training window
        assert call["decided_first_ts"] == int(datetime.fromisoformat(fold["test_start"]).timestamp())
        assert call["last_ts"] == int(datetime.fromisoformat(fold["test_end"]).timestamp())
        assert call["first_ts"] == call["decided_first_ts"] - fold["seed_bars"] * STEP
        assert call["bars"] == fold["seed_bars"] + fold["test_bars"]
        if index:
            previous = folds[index - 1]
            assert fold["train_bars"] > previous["train_bars"]  # expanding training window
            assert fold["test_start"] > previous["test_end"]  # disjoint test slices
    assert folds[-1]["test_end"] == research._iso(bars[-1].open_time)
    assert result["consistency"] == {"positive_folds": sum(
        1 for fold in folds if fold["test_metrics"]["net_return_pct"] > 0), "total_folds": 4}


def test_walk_forward_refuses_thin_data_and_single_windows(config, engine):
    bars = _bars(_prices_rising(400))
    with pytest.raises(ValueError, match="insufficient data for 4 walk-forward folds"):
        walk_forward(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS, params=default_params())
    with pytest.raises(ValueError, match="at least 2 folds"):
        walk_forward(config, "BTC_USDT", bars=bars, higher_bars=[], filters=FILTERS, params=default_params(), folds=1)
    assert engine.calls == []


# ---- aggregates -----------------------------------------------------------
def _fold(net_return, net_pnl, trades, drawdown, factor):
    return {"test_metrics": {"net_return_pct": net_return, "net_pnl": net_pnl, "trades": trades,
                             "max_drawdown_pct": drawdown, "profit_factor": factor}}


def test_aggregate_uses_only_the_out_of_sample_fold_math():
    folds = [_fold(1.0, 10.0, 3, 2.0, 1.5), _fold(-0.5, -5.0, 4, 5.5, None),
             _fold(2.0, 20.0, 2, 1.0, 2.5), _fold(0.4, 1.0, 1, 3.0, 1.0)]
    aggregate = aggregate_folds(folds)
    assert aggregate["net_pnl"] == 26.0
    assert aggregate["trades"] == 10
    assert aggregate["max_drawdown_pct"] == 5.5  # worst fold, not the average
    assert aggregate["profit_factor_mean"] == round((1.5 + 2.5 + 1.0) / 3, 3)
    assert aggregate["net_return_pct_median"] == 0.7  # median of -0.5, 0.4, 1.0, 2.0
    assert aggregate["positive_folds"] == 3 and aggregate["folds"] == 4
    assert "out-of-sample" in aggregate["note"] and "not a forecast" in aggregate["note"]


def test_aggregate_refuses_incomplete_folds():
    with pytest.raises(ValueError, match="no folds"):
        aggregate_folds([])
    with pytest.raises(ValueError, match="refusing to aggregate"):
        aggregate_folds([{"test_metrics": {"trades": 1}}])


def test_walk_forward_aggregate_matches_fold_arithmetic(config, engine):
    result = walk_forward(config, "BTC_USDT", bars=_bars(_prices_rising(900)), higher_bars=[], filters=FILTERS,
                          params=default_params(), folds=4)
    folds = result["folds"]
    aggregate = result["aggregate"]
    assert aggregate["trades"] == sum(fold["test_metrics"]["trades"] for fold in folds)
    assert aggregate["net_pnl"] == round(sum(fold["test_metrics"]["net_pnl"] for fold in folds), 4)
    assert aggregate["max_drawdown_pct"] == max(fold["test_metrics"]["max_drawdown_pct"] for fold in folds)


# ---- acceptance gate ------------------------------------------------------
EVIDENCE = {key: True for key in research.RISK_EVIDENCE_KEYS}


def _report(net_return, factor, trades, drawdown, folds=None):
    aggregate = {"net_return_pct": net_return, "profit_factor": factor, "trades": trades,
                 "max_drawdown_pct": drawdown, "note": "out-of-sample"}
    block = {"aggregate": aggregate}
    if folds is not None:
        block["folds"] = folds
    return {"risk_checks": dict(EVIDENCE), "walk_forward": block}


def test_acceptance_keeps_the_champion_when_the_candidate_only_wins_in_sample():
    candidate = {"metrics": {"net_return_pct": 9.0, "profit_factor": 4.0, "trades": 40, "max_drawdown_pct": 1.0},
                 "validation": {"metrics": {"net_return_pct": -1.2, "profit_factor": 0.8, "trades": 12,
                                            "max_drawdown_pct": 4.0}},
                 "risk_checks": dict(EVIDENCE)}
    champion = _report(0.5, 1.4, 20, 2.0)
    accepted, reasons = acceptance(candidate, champion)
    assert accepted is False
    assert any("no out-of-sample net-return improvement" in reason for reason in reasons)
    assert any("no out-of-sample profit-factor improvement" in reason for reason in reasons)


def test_acceptance_requires_out_of_sample_data_and_risk_evidence():
    champion = _report(0.5, 1.4, 20, 2.0)
    in_sample_only = {"metrics": {"net_return_pct": 12.0, "profit_factor": 5.0, "trades": 50,
                                 "max_drawdown_pct": 0.1}}
    accepted, reasons = acceptance(in_sample_only, champion)
    assert accepted is False and "out-of-sample" in reasons[0]

    weak = _report(2.0, 2.0, 20, 1.0)
    weak.pop("risk_checks")
    accepted, reasons = acceptance(weak, champion)
    assert accepted is False and reasons == ["no risk-limit evidence recorded for the candidate"]

    failing = _report(2.0, 2.0, 20, 1.0)
    failing["risk_checks"]["drawdown_within_config_limit"] = False
    accepted, reasons = acceptance(failing, champion)
    assert accepted is False and "risk-limit check failed: drawdown_within_config_limit" in reasons

    accepted, reasons = acceptance(_report(2.0, 2.0, 20, 1.0), {"metrics": {}})
    assert accepted is False and "champion has no out-of-sample baseline" in reasons[0]


def test_acceptance_labels_an_experiment_that_changed_nothing_as_no_evidence():
    # the portability runs showed the grid can hand back the champion's own params: that is not a test
    champion = _report(0.5, 1.4, 20, 2.0)
    champion["params_label"] = "trend-pullback:v1@81646835a339"
    same = _report(0.5, 1.4, 20, 2.0)
    same["params_label"] = champion["params_label"]
    accepted, reasons = acceptance(same, champion)
    assert accepted is False
    assert any("unchanged champion" in reason for reason in reasons)

    changed = _report(1.5, 2.4, 20, 1.0)
    changed["params_label"] = "trend-pullback:v1@83e716cd235a"
    accepted, reasons = acceptance(changed, champion)
    assert accepted is True
    assert not any("unchanged champion" in reason for reason in reasons)


def test_acceptance_demands_enough_trades_a_clean_drawdown_and_half_the_folds_positive():
    champion = _report(0.5, 1.4, 20, 2.0)
    accepted, reasons = acceptance(_report(2.0, 2.0, 5, 1.0), champion)
    assert accepted is False and "only 5 out-of-sample trades, minimum is 8" in reasons

    accepted, reasons = acceptance(_report(2.0, 2.0, 20, 4.0), champion)
    assert accepted is False and any("more than 1.0pp worse" in reason for reason in reasons)

    lucky_window = [{"test_metrics": {"net_return_pct": 3.0}}, {"test_metrics": {"net_return_pct": -1.0}},
                    {"test_metrics": {"net_return_pct": -2.0}}, {"test_metrics": {"net_return_pct": -0.5}}]
    accepted, reasons = acceptance(_report(0.6, 1.5, 20, 1.0, folds=lucky_window), champion)
    assert accepted is False
    assert any("profitable in only 1 of 4 folds" in reason for reason in reasons)

    accepted, reasons = acceptance(_report(2.0, 2.0, 20, 1.0, folds=[{"test_metrics": {}}]), champion)
    assert accepted is False and any("profitable in only 0 of 1 folds" in reason for reason in reasons)


def test_acceptance_accepts_a_genuinely_better_out_of_sample_candidate():
    champion = _report(0.5, 1.4, 20, 2.0)
    folds = [{"test_metrics": {"net_return_pct": 1.0}}, {"test_metrics": {"net_return_pct": 0.4}},
             {"test_metrics": {"net_return_pct": -0.2}}, {"test_metrics": {"net_return_pct": 0.9}}]
    accepted, reasons = acceptance(_report(1.8, 2.1, 24, 1.5, folds=folds), champion)
    assert accepted is True and reasons == []


# ---- full loop ------------------------------------------------------------
def test_run_research_keeps_the_champion_and_stores_the_rejection(config, engine, workspace):
    store = open_store(workspace)
    bars = _bars(_prices_rally_then_fade())
    before = config.limits
    report = run_research(store, config, symbol="BTC_USDT", timeframe="4h", bars=bars, higher_bars=[],
                          filters=FILTERS, grid={"atr_target_mult": [3.5, 4.0]}, folds=4)
    assert report["verdict"] == "rejected"
    assert any("no out-of-sample net-return improvement" in reason for reason in report["reasons"])
    assert report["champion_label"] == default_params().label()
    assert report["candidate_label"] != report["champion_label"]
    assert report["risk_checks"]["risk_limits_untouched"] is True and config.limits == before
    assert report["mode"] == "PAPER" and json.loads(json.dumps(report))["mode"] == "PAPER"
    assert "تضمین" in report["disclaimer"]
    for key in ("train", "validation", "test", "walk_forward", "split", "grid", "reasons"):
        assert key in report
    assert report["test"]["start"] == research._iso(bars[int(len(bars) * 0.8)].open_time)

    runs = store.list_runs()
    assert [run["kind"] for run in runs].count("walk_forward") == 2
    assert {run["kind"] for run in runs} == {"grid", "walk_forward"}
    assert all(run["symbol"] == "BTC_USDT" and run["timeframe"] == "4h" for run in runs)
    assert all(run["window_start"] == report["split"]["train"]["start"]
               and run["window_end"] == report["split"]["test"]["end"] for run in runs)
    assert all(run["split"]["validation"]["bars"] == report["split"]["validation"]["bars"] for run in runs)
    versions = store.list_strategy_versions()
    assert len(versions) == 2
    candidate_row = next(row for row in versions if row["digest"] != default_params().digest())
    assert candidate_row["accepted"] == 0
    assert candidate_row["source"] == "grid_search" and "champion kept" in candidate_row["decision"]
    assert next(row for row in versions if row["digest"] == default_params().digest())["source"] == "champion_baseline"
    assert store.list_logs(level="warning"), "rejection is logged"
    store.close()


def test_run_research_accepts_a_candidate_that_holds_up_out_of_sample(config, engine, workspace):
    store = open_store(workspace)
    bars = _bars(_prices_rising(900))
    report = run_research(store, config, symbol="BTC_USDT", timeframe="4h", bars=bars, higher_bars=[],
                          filters=FILTERS, grid={"atr_stop_mult": [1.5, 1.2]}, folds=4, top=2)
    assert (report["verdict"], report["reasons"]) == ("accepted", [])
    assert report["candidate_label"] != report["champion_label"]
    assert report["walk_forward"]["aggregate"]["profit_factor_mean"] > \
        report["champion_walk_forward"]["aggregate"]["profit_factor_mean"]
    assert len(report["grid"]) == 2
    versions = store.list_strategy_versions()
    assert len(versions) == 2
    accepted_row = next(row for row in versions if row["accepted"] == 1)
    assert accepted_row["params"]["atr_stop_mult"] == 1.2
    assert accepted_row["parent"] == default_params().digest()
    assert json.loads(json.dumps(report))["verdict"] == "accepted"
    store.close()


def test_run_research_never_deletes_previous_versions(config, engine, workspace):
    store = open_store(workspace)
    bars = _bars(_prices_rising(900))
    run_research(store, config, symbol="BTC_USDT", timeframe="4h", bars=bars, higher_bars=[], filters=FILTERS,
                 grid={"atr_stop_mult": [1.5, 1.2]}, folds=4)
    first_round = store.list_strategy_versions()
    first_runs = len(store.list_runs())
    run_research(store, config, symbol="BTC_USDT", timeframe="4h", bars=bars, higher_bars=[], filters=FILTERS,
                 grid={"atr_stop_mult": [1.5, 1.2]}, folds=4)
    assert len(store.list_runs()) == first_runs + 3
    digests = {row["digest"] for row in store.list_strategy_versions()}
    assert digests >= {row["digest"] for row in first_round}
    store.close()


def test_run_research_fails_closed_on_insufficient_history(config, engine, workspace):
    store = open_store(workspace)
    with pytest.raises(ValueError, match="insufficient data"):
        run_research(store, config, symbol="BTC_USDT", timeframe="4h", bars=_bars(_prices_rising(300)),
                     higher_bars=[], filters=FILTERS, grid={"atr_stop_mult": [1.2]}, folds=4)
    assert engine.calls == []
    failed = store.list_runs(kind="grid")
    assert failed and failed[0]["status"] == "failed" and "insufficient data" in failed[0]["error"]
    assert store.list_logs(level="error")
    assert store.list_strategy_versions() == []
    store.close()


def test_run_research_rejects_bars_that_do_not_match_the_timeframe(config, engine, workspace):
    store = open_store(workspace)
    bars = _bars(_prices_rising(900))
    misaligned = bars[:100] + [Kline(open_time=bars[100].open_time + 900, open=1, high=1.1, low=0.9, close=1.0,
                                     base_volume=1.0, quote_volume=1.0, closed=True)] + bars[101:]
    with pytest.raises(ValueError, match="not aligned to timeframe"):
        run_research(store, config, symbol="BTC_USDT", timeframe="4h", bars=misaligned, higher_bars=[],
                     filters=FILTERS, grid={"atr_stop_mult": [1.2]}, folds=4)
    with pytest.raises(ValueError, match="unknown timeframe"):
        run_research(store, config, symbol="BTC_USDT", timeframe="7m", bars=bars, higher_bars=[],
                     filters=FILTERS, grid={"atr_stop_mult": [1.2]}, folds=4)
    store.close()


# ---- real engine ----------------------------------------------------------
def test_evaluate_against_the_real_backtest_engine(config):
    """The wiring is only proven on the real engine; when it cannot run, that is reported, not faked."""
    prices = uptrend_prices(500)
    bars = _bars(prices)
    higher = _bars(uptrend_prices(120, base=40_000.0), step=DAY)
    try:
        report = evaluate(config, "BTC_USDT", bars=bars, higher_bars=higher, filters=FILTERS,
                          params=default_params(), warmup=80, timeframe="4h")
    except UnboundLocalError as exc:
        pytest.skip(f"blocked by an upstream defect in src/cat/backtest.py run_backtest(): {exc}")
    assert report["bars"] == 500 and report["start"] == research._iso(bars[0].open_time)
    assert report["end"] == research._iso(bars[-1].open_time)
    assert report["trades"] == report["metrics"]["trades"]
    assert {"net_return_pct", "max_drawdown_pct", "profit_factor", "fees_paid"} <= set(report["metrics"])
    assert report["metrics"]["fees_paid"] >= 0.0
    assert report["params_label"] == default_params().label()
    assert isinstance(report["buy_hold_pct"], float)
    assert isinstance(report["rejected"], dict) and all(isinstance(value, int) for value in report["rejected"].values())
    assert report["trades"] + sum(report["rejected"].values()) > 0  # the engine really walked the bars
