from __future__ import annotations

import pytest

from cat.backtest import run_backtest
from cat.data import Kline, SymbolFilter
from cat.strategies import default_params
from cat.strategy import Proposal, analyze
from conftest import make_klines


def test_backtest_trade_pnl_and_profit_metrics_include_entry_and_exit_fees(config):
    prices = [100.0 + index * 0.01 for index in range(180)]
    bars = make_klines(prices)
    filters = SymbolFilter(
        symbol="BTC_USDT",
        price_precision=2,
        qty_precision=6,
        min_qty=0.000001,
        min_quote=3.0,
        max_quote=1_000_000.0,
        tradable=True,
        fee_pct=0.1,
    )

    def buy_once(snapshot, position, **kwargs):
        analysis = analyze(snapshot, kwargs["params"])
        if position is not None:
            return Proposal("NO_TRADE", 0.0, ["hold for test"], None, None, None, analysis, "test_hold")
        entry = snapshot.quote.ask
        return Proposal("BUY", 0.95, ["test entry"], entry, entry * 0.97, entry * 1.20, analysis, "test_entry")

    result = run_backtest(
        config,
        "BTC_USDT",
        bars=bars,
        higher_bars=[],
        filters=filters,
        timeframe="1h",
        warmup=80,
        params=default_params(),
        propose_fn=buy_once,
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    expected_round_trip_fees = trade.qty * trade.entry * (config.taker_fee_pct / 100.0) + (
        trade.qty * trade.exit * (config.taker_fee_pct / 100.0)
    )
    expected_net_pnl = trade.qty * (trade.exit - trade.entry) - expected_round_trip_fees
    assert trade.fees == pytest.approx(expected_round_trip_fees, abs=1e-6)
    assert trade.pnl == pytest.approx(expected_net_pnl, abs=1e-6)
    assert result.metrics["net_pnl"] == pytest.approx(result.final_equity - config.allocated_capital, abs=1e-6)
