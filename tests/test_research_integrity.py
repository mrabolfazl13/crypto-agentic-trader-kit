from __future__ import annotations

import pytest

from cat.data import Kline
from cat.research import validate_research_bars
from conftest import make_klines


def test_research_accepts_closed_contiguous_ohlcv_history():
    bars = make_klines([100.0, 101.0, 99.0, 102.0])
    validate_research_bars(bars, "1h")


def test_research_rejects_missing_market_candles():
    bars = make_klines([100.0, 101.0, 102.0, 103.0])
    for index in range(2, len(bars)):
        bars[index] = Kline(
            open_time=bars[index].open_time + 3600,
            open=bars[index].open,
            high=bars[index].high,
            low=bars[index].low,
            close=bars[index].close,
            base_volume=bars[index].base_volume,
            quote_volume=bars[index].quote_volume,
            closed=True,
        )
    with pytest.raises(ValueError, match="unexpected gap"):
        validate_research_bars(bars, "1h")


def test_research_drops_unclosed_final_candle():
    bars = make_klines([100.0, 101.0, 102.0])
    bars[-1] = Kline(**{**bars[-1].__dict__, "closed": False})
    usable = validate_research_bars(bars, "1h")
    assert usable == bars[:-1]


def test_research_rejects_unclosed_candle_inside_history():
    bars = make_klines([100.0, 101.0, 102.0, 103.0])
    bars[1] = Kline(**{**bars[1].__dict__, "closed": False})
    with pytest.raises(ValueError, match="unexpected gap"):
        validate_research_bars(bars, "1h")


def test_research_rejects_non_finite_ohlcv_values():
    bars = make_klines([100.0, 101.0, 102.0])
    bars[-1] = Kline(**{**bars[-1].__dict__, "close": float("nan")})
    with pytest.raises(ValueError, match="non-finite"):
        validate_research_bars(bars, "1h")


def test_research_rejects_invalid_ohlc_range():
    bars = make_klines([100.0, 101.0, 102.0])
    bars[-1] = Kline(**{**bars[-1].__dict__, "high": bars[-1].close - 1.0})
    with pytest.raises(ValueError, match="invalid OHLC range"):
        validate_research_bars(bars, "1h")


def test_backtest_higher_timeframe_filter_ignores_unclosed_candles():
    from cat.backtest import select_closed_higher

    bars = make_klines([100.0, 101.0, 102.0])
    bars[0] = Kline(**{**bars[0].__dict__, "closed": False})
    selected = select_closed_higher(bars, bars[2].open_time + 3600, 3600)
    assert selected == bars[1:3]


def test_optimizer_walk_forward_never_receives_final_holdout(config, monkeypatch):
    from cat import research
    from cat.strategies import default_params

    bars = make_klines([100.0 + index * 0.01 for index in range(1000)])
    filters = __import__("conftest").make_snapshot().filters
    observed = []

    def fake_grid_search(*args, **kwargs):
        return [{
            "params_label": "candidate-only",
            "params": default_params().as_dict(),
            "metrics": {"net_return_pct": 0.1},
            "train": {"metrics": {"net_return_pct": 0.1}, "rejected": {}},
            "validation": {"metrics": {"net_return_pct": 0.2}},
        }]

    def fake_evaluate(*args, **kwargs):
        return {"metrics": {"net_return_pct": 0.0}, "trades": 0, "rejected": {},
                "params_label": "baseline", "start": "", "end": "", "bars": len(kwargs.get("bars", [])),
                "buy_hold_pct": 0.0, "equity_curve": [], "benchmark_curve": []}

    def fake_walk_forward(*args, **kwargs):
        supplied = kwargs["bars"]
        observed.append((len(supplied), supplied[-1].open_time, kwargs.get("strategy")))
        candidate = kwargs.get("strategy") is None
        net = 2.0 if candidate else 1.0
        factor = 1.5 if candidate else 1.2
        folds = [{"test_metrics": {"net_return_pct": net}} for _ in range(2)]
        return {
            "folds": folds,
            "aggregate": {"net_return_pct_median": net, "profit_factor_mean": factor,
                          "max_drawdown_pct": 1.0, "trades": 20, "net_pnl": 2.0},
        }

    class Store:
        def log(self, *args, **kwargs):
            pass

        def record_run(self, *args, **kwargs):
            pass

        def save_strategy_version(self, *args, **kwargs):
            pass

    monkeypatch.setattr(research, "grid_search", fake_grid_search)
    monkeypatch.setattr(research, "evaluate", fake_evaluate)
    monkeypatch.setattr(research, "walk_forward", fake_walk_forward)

    report = research.run_research(
        Store(), config, symbol="BTC_USDT", timeframe="1h", bars=bars, higher_bars=[],
        filters=filters, grid={"ema_fast": [12]}, folds=2, warmup=80,
        champion={"strategy": "trend_v1", "params": default_params().as_dict(),
                  "walk_forward": {"aggregate": {"net_return_pct_median": 999.0}}},
    )

    split = research.split_bars(bars, min_bars=max(research.MIN_SEGMENT_BARS, 81))
    assert len(observed) == 2  # candidate and incumbent are re-measured
    assert all(length == len(split.train) + len(split.validation) for length, _, _ in observed)
    assert all(last_ts < split.test[0].open_time for _, last_ts, _ in observed)
    assert report["test"]["bars"] == len(split.test)


def test_research_allows_only_explicitly_declared_session_gaps():
    bars = make_klines([100.0, 101.0, 102.0])
    bars[2] = Kline(**{**bars[2].__dict__, "open_time": bars[2].open_time + 7200})
    validate_research_bars(
        bars,
        "1h",
        gap_is_expected=lambda previous, following: following - previous == 3 * 3600,
    )
