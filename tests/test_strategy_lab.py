"""Offline tests for the alternative entry families and the month-scan aggregation."""

from __future__ import annotations

import math

from conftest import make_snapshot

from cat.month_scan import _breadth_wrapper, _pooled, breadth_at, breadth_series, render_report
from cat.strategies import default_params
from cat.strategy import Proposal
from cat.strategy_lab import LAB_STRATEGIES, propose_breakout, propose_momentum, propose_pullback


def signal_kwargs(config, **overrides):
    base = {
        "confidence_threshold": config.confidence_threshold,
        "require_multiple_signals": config.require_multiple_signals,
        "no_trade_if_signals_conflict": config.no_trade_if_signals_conflict,
        "min_reward_risk_ratio": config.limits.min_reward_risk_ratio,
        "maker_fee_pct": config.maker_fee_pct,
        "taker_fee_pct": config.taker_fee_pct,
        "est_slippage_pct": config.limits.max_slippage_pct,
    }
    base.update(overrides)
    return base


def breakout_prices(n: int = 140) -> list[float]:
    prices = [100.0 + index * 0.3 + 6.0 * math.sin(index / 4.0) for index in range(n)]
    prior_high = max(price * 1.004 for price in prices[-31:-1])
    prices[-1] = prior_high * 1.002
    return prices


def momentum_prices(n: int = 140) -> list[float]:
    return [100.0 + index * 0.3 + 8.0 * math.sin(index / 4.0) for index in range(n)]


def pullback_prices() -> list[float]:
    prices = [100.0 + index * 0.6 + 3.0 * math.sin(index / 4.0) for index in range(110)]
    last = prices[-1]
    for _ in range(4):
        last *= 0.98
        prices.append(last)
    prices.append(last * 1.02)
    return prices


def downtrend_prices(n: int = 140) -> list[float]:
    return [200.0 - index * 0.6 + 4.0 * math.sin(index / 4.0) for index in range(n)]


def test_breakout_enters_on_a_confirmed_breakout(config):
    proposal = propose_breakout(make_snapshot(prices=breakout_prices()), None, **signal_kwargs(config))
    assert proposal.action == "BUY"
    assert proposal.stop < proposal.entry < proposal.target
    assert proposal.confidence >= config.confidence_threshold


def test_breakout_rejects_without_a_breakout(config):
    proposal = propose_breakout(make_snapshot(prices=momentum_prices()), None, **signal_kwargs(config))
    assert proposal.action == "NO_TRADE"
    assert proposal.reason == "no_breakout"


def test_momentum_enters_on_sustained_roc(config):
    proposal = propose_momentum(make_snapshot(prices=momentum_prices()), None, **signal_kwargs(config))
    assert proposal.action == "BUY"
    assert proposal.stop < proposal.entry < proposal.target


def test_fresh_only_refuses_to_rebuy_momentum_that_was_already_strong(config):
    """A sustained trend must not be re-bought on every bar: that pays the fee repeatedly."""
    snapshot = make_snapshot(prices=momentum_prices())
    kwargs = signal_kwargs(config, fresh_only=True)
    proposal = propose_momentum(snapshot, None, **kwargs)
    assert proposal.action == "NO_TRADE"
    assert proposal.reason == "not_fresh"


def test_fresh_only_keeps_a_breakout_that_crosses_on_the_last_bar(config):
    proposal = propose_breakout(make_snapshot(prices=breakout_prices()), None, **signal_kwargs(config, fresh_only=True))
    assert proposal.action == "BUY"


def test_fresh_only_refuses_a_breakout_that_is_already_above_the_prior_high(config):
    prices = [100.0 + index * 0.3 + 6.0 * math.sin(index / 4.0) for index in range(140)]
    prior = max(price * 1.004 for price in prices[-31:-1])
    prices[-2] = prior * 1.002  # the previous bar already crossed
    prices[-1] = prior * 1.02
    proposal = propose_breakout(make_snapshot(prices=prices), None, **signal_kwargs(config, fresh_only=True))
    assert proposal.action == "NO_TRADE"
    assert proposal.reason == "not_fresh"


def test_pullback_enters_after_an_rsi_reset(config):
    proposal = propose_pullback(make_snapshot(prices=pullback_prices()), None, **signal_kwargs(config))
    assert proposal.action == "BUY"
    assert proposal.stop < proposal.entry < proposal.target


def test_every_family_refuses_a_downtrend(config):
    snapshot = make_snapshot(prices=downtrend_prices())
    for name in ("breakout_30", "momentum_20"):
        proposal = LAB_STRATEGIES[name](snapshot, None, **signal_kwargs(config))
        assert proposal.action == "NO_TRADE", name


def test_confidence_ceiling_blocks_entries(config):
    kwargs = signal_kwargs(config, confidence_threshold=0.99)
    proposal = propose_breakout(make_snapshot(prices=breakout_prices()), None, **kwargs)
    assert proposal.action == "NO_TRADE"
    assert proposal.reason == "confidence"


def test_pooled_summary_aggregates_pooled_trades_not_averaged_ratios():
    rows = [
        {"metrics": {"net_return_pct": 2.0, "net_pnl": 20.0, "max_drawdown_pct": 1.0, "fees_paid": 1.0},
         "buy_hold_pct": 5.0,
         "trades": [{"pnl": 10.0, "fees": 0.5, "reason": "target"}, {"pnl": -4.0, "fees": 0.5, "reason": "stop"}],
         "rejected": {"regime_filter": 3}},
        {"metrics": {"net_return_pct": -1.0, "net_pnl": -10.0, "max_drawdown_pct": 3.0, "fees_paid": 1.0},
         "buy_hold_pct": -2.0,
         "trades": [{"pnl": 6.0, "fees": 0.5, "reason": "target"}],
         "rejected": {"regime_filter": 1, "no_breakout": 4}},
    ]
    summary = _pooled(rows)
    assert summary["symbols"] == 2
    assert summary["symbols_with_trades"] == 2
    assert summary["trades"] == 3
    assert summary["win_rate_pct"] == 66.67
    assert summary["profit_factor"] == 4.0  # (10 + 6) / 4
    assert summary["net_pnl_sum"] == 10.0
    assert summary["mean_net_return_pct"] == 0.5
    assert summary["positive_symbols_pct"] == 50.0
    assert summary["beat_buy_hold_pct"] == 50.0
    assert summary["rejected"]["no_breakout"] == 4


def test_report_renders_without_fabricated_values():
    scan = {
        "meta": {
            "source": "gate:spot/candlesticks", "coverage_end": 1_700_000_000,
            "symbols_with_data": 0, "symbols_requested": 100, "generated_at": "2026-10-24T00:00:00+00:00",
            "disclaimer": "historical only", "days": 30, "timeframe": "4h",
        },
        "strategies": {},
        "latest": {},
    }
    text = render_report(scan)
    assert "اسکن 30 روزه" in text
    assert "1.3" not in text  # no invented percentages anywhere


def test_report_names_the_requested_window_and_keeps_other_windows(config):
    from cat.month_scan import write_report

    base = {
        "meta": {
            "source": "gate:spot/candlesticks", "coverage_end": 1_700_000_000,
            "symbols_with_data": 0, "symbols_requested": 100, "generated_at": "2026-10-24T00:00:00+00:00",
            "disclaimer": "historical only", "timeframe": "4h",
        },
        "strategies": {},
        "latest": {},
    }
    sixty = dict(base, meta=dict(base["meta"], days=60))
    thirty = dict(base, meta=dict(base["meta"], days=30))
    sixty_path, _ = write_report(sixty, None, config)
    thirty_path, _ = write_report(thirty, None, config)
    assert sixty_path != thirty_path  # a longer scan must not overwrite a shorter one
    assert "_60d_" in sixty_path.name and "_30d_" in thirty_path.name
    assert "اسکن 60 روزه" in sixty_path.read_text(encoding="utf-8")


STEP = 3600
START = 1_700_000_000


def _breadth_data(ups: int, downs: int, n: int = 80) -> dict:
    symbols = {}
    for index in range(ups):
        prices = [100.0 * (1 + 0.005 * step) for step in range(n)]
        symbols[f"UP{index}_USDT"] = {"bars": [[START + step * STEP, p, p, p, p, 1.0, 100.0, True] for step, p in enumerate(prices)]}
    for index in range(downs):
        prices = [100.0 * (1 - 0.005 * step) for step in range(n)]
        symbols[f"DN{index}_USDT"] = {"bars": [[START + step * STEP, p, p, p, p, 1.0, 100.0, True] for step, p in enumerate(prices)]}
    return symbols


def test_breadth_series_counts_symbols_above_their_own_ema50():
    data = _breadth_data(ups=8, downs=2)
    series = breadth_series(data, list(data), STEP)
    assert series == sorted(series)
    assert series[-1][1] == 0.8
    assert series[-1][0] == START + 79 * STEP + STEP


def test_breadth_at_returns_the_latest_snapshot_not_a_future_one():
    data = _breadth_data(ups=5, downs=5)
    series = breadth_series(data, list(data), STEP)
    assert series
    assert breadth_at(series, series[0][0]) == series[0][1]
    assert breadth_at(series, series[-1][0] + 10) == series[-1][1]
    assert breadth_at(series, series[0][0] - 1) is None
    assert breadth_at([], 1_000) is None


def test_breadth_gate_blocks_entries_when_the_market_is_weak(config):
    data = _breadth_data(ups=2, downs=8)
    series = breadth_series(data, list(data), STEP)
    last_stamp = series[-1][0]
    wrapped = _breadth_wrapper(LAB_STRATEGIES["momentum_20"], series, 0.5)
    snapshot = make_snapshot(prices=momentum_prices(), now=last_stamp)
    blocked = wrapped(snapshot, None, **signal_kwargs(config))
    assert blocked.action == "NO_TRADE"
    assert blocked.reason == "market_breadth"

    strong_data = _breadth_data(ups=10, downs=0)
    strong = _breadth_wrapper(LAB_STRATEGIES["momentum_20"], breadth_series(strong_data, list(strong_data), STEP), 0.5)
    allowed = strong(snapshot, None, **signal_kwargs(config))
    assert allowed.action == "BUY"


def test_breadth_gate_fails_closed_without_a_snapshot(config):
    wrapped = _breadth_wrapper(LAB_STRATEGIES["momentum_20"], [], 0.5)
    proposal = wrapped(make_snapshot(prices=momentum_prices()), None, **signal_kwargs(config))
    assert proposal.action == "NO_TRADE"
    assert proposal.reason == "market_breadth_unavailable"


def test_improvement_phase_builds_breadth_candidates(config, monkeypatch):
    import cat.month_scan as ms

    data = _breadth_data(ups=10, downs=2)
    calls: list[dict] = []

    def fake_window_rows(*args, **kwargs):
        calls.append(kwargs)
        return [], {}

    monkeypatch.setattr(ms, "_window_rows", fake_window_rows)
    result = ms._run_improvements(
        config, data, list(data), {}, ms.ScanOptions(strategies=("momentum_20",)), progress=lambda _: None,
    )
    assert result["breadth"] is not None
    assert result["chosen"] is None  # no trades from the stub, so nothing may be selected
    wrapped = [kw for kw in calls if getattr(kw.get("signal"), "__name__", "") == "wrapped"]
    assert len(wrapped) == 2  # breadth50 and breadth50+stop2_target4
    assert all(kw["params"] is None for kw in wrapped if kw["name"].endswith(":breadth50"))


def _open_position(snapshot, hours_ago: float):
    from datetime import datetime, timedelta, timezone

    from cat.strategy import Position

    opened = datetime.fromtimestamp(snapshot.fetched_at, tz=timezone.utc) - timedelta(hours=hours_ago)
    last = snapshot.klines[-1].close
    return Position(symbol="MOM_USDT", qty=1.0, avg_entry=last, stop=last * 0.97, target=last * 1.08,
                    decision_id="t-1", opened_at=opened.isoformat())


def test_horizon_param_forces_an_exit_after_the_time_limit(config):
    from cat.strategy import propose

    snapshot = make_snapshot(prices=momentum_prices())
    params = default_params().replace(max_hold_bars=3)
    proposal = propose(snapshot, _open_position(snapshot, 3), params=params, **signal_kwargs(config))
    assert proposal.action == "SELL"
    assert proposal.reason == "exit_horizon"
    assert proposal.entry == snapshot.quote.bid


def test_horizon_param_leaves_a_young_position_to_the_strategy(config):
    from cat.strategy import propose

    snapshot = make_snapshot(prices=momentum_prices())
    params = default_params().replace(max_hold_bars=3)
    proposal = propose(snapshot, _open_position(snapshot, 1), params=params, **signal_kwargs(config))
    assert proposal.reason != "exit_horizon"


def test_horizon_is_absent_from_the_base_digest_until_it_is_used(config):
    """A disabled horizon must not rename every already-published version."""
    base = default_params()
    assert "max_hold_bars" not in base.tuning()
    timed = base.replace(max_hold_bars=3)
    assert timed.tuning()["max_hold_bars"] == 3
    assert timed.digest() != base.digest()


def test_pooled_reports_hold_durations_and_intraday_share():
    rows = [{
        "metrics": {"net_return_pct": 0.5, "net_pnl": 5.0, "max_drawdown_pct": 1.0, "fees_paid": 0.3},
        "buy_hold_pct": 1.0,
        "trades": [
            {"pnl": 1.0, "fees": 0.1, "reason": "target", "entry_time": 1_000_000, "exit_time": 1_000_000 + 2 * 3600},
            {"pnl": -1.0, "fees": 0.1, "reason": "exit_time_limit", "entry_time": 1_000_000, "exit_time": 1_000_000 + 30 * 3600},
            {"pnl": 2.0, "fees": 0.1, "reason": "stop", "entry_time": 1_000_000, "exit_time": 1_000_000 + 5 * 3600},
        ],
        "rejected": {},
    }]
    summary = _pooled(rows)
    assert summary["median_hold_hours"] == 5.0
    assert summary["mean_hold_hours"] == 12.33
    assert summary["hold_under_3h_pct"] == 33.3
    assert summary["hold_buckets"] == {"lt_1h": 0, "1_3h": 1, "3_8h": 1, "8_24h": 0, "gte_24h": 1}


def test_backtest_time_stop_records_real_hold_durations(config):
    """A forced exit must record the actual entry time, not epoch zero."""
    from datetime import datetime

    from cat.backtest import run_backtest
    from cat.data import Kline, SymbolFilter
    from cat.strategy import analyze, propose

    step = 3600
    start = 1_700_000_000
    prices = [100.0 + index * 0.2 for index in range(160)]
    bars = []
    for index, price in enumerate(prices):
        open_ = prices[index - 1] if index else price
        bars.append(Kline(start + index * step, open_, max(open_, price) * 1.002,
                          min(open_, price) * 0.998, price, 100.0, 100.0 * price, True))
    filters = SymbolFilter(symbol="BTC_USDT", price_precision=2, qty_precision=4, min_qty=0.001,
                           min_quote=3.0, max_quote=1_000_000.0, tradable=True, fee_pct=0.1)

    def buy_once(snapshot, position, **kw):
        if position is not None:
            # The real exit policy, so the horizon rule is what gets measured.
            return propose(snapshot, position, **kw)
        analysis = analyze(snapshot, kw["params"])
        entry = snapshot.quote.ask
        return Proposal("BUY", 0.8, ["test entry"], entry, entry * 0.97, entry * 1.08, analysis, "test_entry")

    result = run_backtest(
        config, "BTC_USDT", bars=bars, higher_bars=[], filters=filters, timeframe="1h", warmup=80,
        params=default_params().replace(max_hold_bars=3), propose_fn=buy_once,
    )
    assert result.trades
    assert all(trade.entry_time >= start for trade in result.trades)  # entry_time is never the epoch
    holds = [(trade.exit_time - trade.entry_time) / 3600.0 for trade in result.trades]
    assert 3.0 in holds  # three 1h bars held, then the forced exit filled on the next open
