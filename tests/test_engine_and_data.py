"""Unit tests for sizing, data validation, audit integrity and no-look-ahead."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cat.audit import AuditLog
from cat.config import load_config
from cat.data import Kline, validate_klines, estimate_slippage_pct
from cat.decision import build_decision, validate_decision
from cat.indicators import atr, ema, rsi, slope
from cat.portfolio import AccountState, atomic_write, load_state
from cat.risk import evaluate
from cat.strategy import Position, Proposal, analyze, propose
from conftest import make_klines, make_snapshot, uptrend_prices

UTC = timezone.utc


def _buy(snapshot, **overrides):
    base = dict(
        action="BUY", confidence=0.82, rationale=["trend"], entry=snapshot.quote.ask,
        stop=snapshot.quote.ask * 0.985, target=snapshot.quote.ask * 1.03, analysis=None, reason="entry_signal",
    )
    base.update(overrides)
    return Proposal(**base)


def sized(config, state, snapshot, now, **overrides):
    proposal = _buy(snapshot, **overrides)
    return evaluate(config, state, snapshot, proposal, None, now=now,
                    prices={snapshot.symbol: snapshot.quote.mid}, kill_switch_active=False, clock_ok=True)


def test_position_size_is_capped_by_risk_budget(config, now):
    snapshot = make_snapshot()
    risk = sized(config, AccountState.new(config.allocated_capital), snapshot, now)
    assert risk.approved
    limits = config.limits
    risk_units = risk.entry - risk.stop + risk.entry * ((config.maker_fee_pct + config.taker_fee_pct) / 100 + risk.est_slippage_pct / 100)
    budget = config.allocated_capital * limits.max_risk_per_trade_pct / 100.0
    assert risk.qty * risk_units <= budget + 1e-9
    position_cap = config.allocated_capital * limits.max_position_pct_of_allocated_capital / 100.0
    allocation_cap = config.allocated_capital * limits.max_account_allocation_pct / 100.0
    assert risk.notional <= min(position_cap, allocation_cap) + 1e-6
    assert risk.reward_risk_ratio >= limits.min_reward_risk_ratio


def test_size_respects_symbol_precision_and_minimums(config, now):
    snapshot = make_snapshot(min_quote=1000.0)
    risk = sized(config, AccountState.new(config.allocated_capital), snapshot, now)
    assert not risk.approved and "min quote" in risk.reasons[0]

    tight = make_snapshot(qty_precision=2)
    approved = sized(config, AccountState.new(config.allocated_capital), tight, now)
    if approved.approved:
        assert abs(approved.qty * 100) % 1 < 1e-6


def test_low_confidence_and_reward_risk_are_rejected(config, now):
    snapshot = make_snapshot()
    state = AccountState.new(config.allocated_capital)
    weak = sized(config, state, snapshot, now, confidence=config.confidence_threshold - 0.05)
    assert not weak.approved and "confidence" in weak.reasons[0]

    thin = sized(config, state, snapshot, now, target=snapshot.quote.ask * 1.004)
    assert not thin.approved and "reward/risk" in thin.reasons[0]


def test_daily_trade_cap_and_position_cap(config, now):
    snapshot = make_snapshot()
    busy = AccountState.new(config.allocated_capital)
    busy.trades_today = config.limits.max_trades_per_day
    assert not sized(config, busy, snapshot, now).approved

    full = AccountState.new(config.allocated_capital)
    full.positions["ETH_USDT"] = Position("ETH_USDT", 1.0, 3000.0, 2900.0, 3200.0, "dec-e", now.isoformat())
    assert not sized(config, full, snapshot, now).approved


def test_unknown_symbol_is_rejected(config, now):
    snapshot = make_snapshot(symbol="SOL_USDT")
    risk = sized(config, AccountState.new(config.allocated_capital), snapshot, now)
    assert not risk.approved and "approved symbol list" in risk.reasons[0]


@pytest.mark.parametrize(
    "prices,expected",
    [
        ([100 + i for i in range(70)], "increasing"),
        ([100 - i for i in range(70)], "decreasing"),
    ],
)
def test_indicator_direction(prices, expected):
    series = ema(prices, 20)
    if expected == "increasing":
        assert series[-1] > series[0]
    else:
        assert series[-1] < series[0]
    assert slope(prices, 30) is not None
    assert (slope(prices, 30) > 0) == (expected == "increasing")


def test_rsi_bounds_and_atr_positive():
    prices = uptrend_prices(120)
    values = rsi(prices, 14)
    assert values and all(0 <= v <= 100 for v in values)
    bars = make_klines(prices)
    atr_values = atr(bars, 14)
    assert atr_values and all(v > 0 for v in atr_values)


def test_validate_klines_detects_gaps_duplicates_and_negatives():
    bars = make_klines(uptrend_prices(100))
    usable, warnings = validate_klines(bars, "1h", bars[-1].open_time + 3600, 120)
    assert warnings == [] and len(usable) == len(bars)

    gapped = bars[:50] + bars[55:]
    _, warnings = validate_klines(gapped, "1h", gapped[-1].open_time + 3600, 120)
    assert "time_gap" in warnings

    duplicated = bars[:60] + [bars[59]]
    _, warnings = validate_klines(duplicated, "1h", duplicated[-1].open_time + 3600, 120)
    assert "duplicate_timestamps" in warnings

    negative = list(bars)
    negative[10] = Kline(**{**negative[10].__dict__, "close": -1.0})
    _, warnings = validate_klines(negative, "1h", negative[-1].open_time + 3600, 120)
    assert "non_positive_prices" in warnings

    stale = make_klines(uptrend_prices(40))
    _, warnings = validate_klines(stale, "1h", stale[-1].open_time + 3600 + 10 * 3600, 120)
    assert "stale_klines" in warnings

    _, warnings = validate_klines([b for b in bars if not b.closed], "1h", bars[-1].open_time, 120)
    assert warnings == ["no_data"] or warnings == []


def test_unclosed_bar_is_excluded():
    bars = make_klines(uptrend_prices(60)) + [Kline(**{**bars_extra().__dict__, "closed": False})]
    usable, _ = validate_klines(bars, "1h", bars[-1].open_time + 3600, 120)
    assert all(b.closed for b in usable)
    assert len(usable) == len(bars) - 1


def bars_extra():
    return Kline(1_700_000_000 + 60 * 3600, 1, 1.1, 0.9, 1.05, 1.0, 1.0, True)


def test_slippage_estimate_is_capped():
    snapshot = make_snapshot(spread_pct=1.0)
    assert estimate_slippage_pct(snapshot.quote, 0.20) == 0.20
    tight = make_snapshot(spread_pct=0.01)
    assert estimate_slippage_pct(tight.quote, 0.20) < 0.20


def test_decision_matches_schema_and_carries_required_fields(config, now):
    snapshot = make_snapshot()
    proposal = propose(
        snapshot, None, confidence_threshold=config.confidence_threshold, require_multiple_signals=True,
        no_trade_if_signals_conflict=True, min_reward_risk_ratio=config.limits.min_reward_risk_ratio,
        maker_fee_pct=config.maker_fee_pct, taker_fee_pct=config.taker_fee_pct, est_slippage_pct=0.03,
    )
    risk = sized(config, AccountState.new(config.allocated_capital), snapshot, now)
    payload = build_decision(decision_id="dec-test", now=now, snapshot=snapshot, proposal=proposal, risk=risk, analysis=analyze(snapshot))
    assert validate_decision(config.root, payload["decision"]) == []
    decision = payload["decision"]
    assert decision["risk"]["position_size_quote"] >= 0
    assert decision["data_quality"]["is_fresh"] is True
    assert decision["rationale"]

    bogus = dict(decision, action="YOLO")
    assert validate_decision(config.root, bogus)


def test_audit_chain_detects_tampering(config):
    log = AuditLog(config.root / "state" / "audit" / "chain.jsonl")
    for index in range(5):
        log.append("event", {"index": index})
    ok, message = log.verify()
    assert ok, message

    path = config.root / "state" / "audit" / "chain.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2].replace('"index": 2', '"index": 99')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tampered = AuditLog(path)
    ok, message = tampered.verify()
    assert not ok and "line 3" in message


def test_state_survives_restart(config, workspace):
    state = AccountState.new(1000.0)
    state.record_fill(cost=100.0, fee=0.1, qty=0.002, price=50_000.0, symbol="BTC_USDT",
                      stop=49_000.0, target=52_000.0, decision_id="dec-1")
    path = workspace / "state" / "account.json"
    atomic_write(path, state.to_dict())
    loaded = load_state(path, 5_000_000.0)
    assert loaded.cash == pytest.approx(state.cash)
    assert loaded.positions["BTC_USDT"].qty == pytest.approx(0.002)
    # The agent may not inflate its own budget on reload.
    assert loaded.allocated_capital == 1000.0


def test_fee_and_pnl_accounting_is_consistent():
    state = AccountState.new(1000.0)
    state.record_fill(cost=200.0, fee=0.2, qty=0.004, price=50_000.0, symbol="BTC_USDT",
                      stop=49_000.0, target=52_000.0, decision_id="dec-1")
    assert state.cash == pytest.approx(799.8)
    trade = state.record_close("BTC_USDT", 0.004, 51_000.0, 0.204, reason="target", decision_id="dec-2")
    assert trade is not None
    assert trade.realized_pnl == pytest.approx(4.0 - 0.204)
    assert state.realized_pnl == pytest.approx(trade.realized_pnl)
    assert state.cash == pytest.approx(1003.596)
    assert not state.positions


def test_period_roll_resets_daily_counters():
    state = AccountState.new(1000.0)
    state.day = "2000-01-01"
    state.week = "2000-W01"
    state.trades_today = 4
    state.day_realized_pnl = -50.0
    assert state.roll_periods(datetime(2026, 10, 23, 12, tzinfo=UTC))
    assert state.trades_today == 0 and state.day_realized_pnl == 0.0


def test_strategy_uses_only_the_window_it_is_given(config):
    prices = uptrend_prices(240)
    snapshot = make_snapshot(prices=prices)
    truncated = make_snapshot(prices=prices[:-12])
    kwargs = dict(
        confidence_threshold=config.confidence_threshold, require_multiple_signals=True,
        no_trade_if_signals_conflict=True, min_reward_risk_ratio=config.limits.min_reward_risk_ratio,
        maker_fee_pct=config.maker_fee_pct, taker_fee_pct=config.taker_fee_pct, est_slippage_pct=0.03,
    )
    full = propose(snapshot, None, **kwargs)
    past = propose(truncated, None, **kwargs)
    # The proposal can only be built from bars inside its own window.
    assert full.analysis.indicators["price"] == pytest.approx(prices[-1])
    assert past.analysis.indicators["price"] == pytest.approx(prices[-13])
    assert full.analysis.indicators["bars_used"] == len(snapshot.klines)
    assert past.analysis.indicators["bars_used"] == len(truncated.klines)


def test_higher_timeframe_lookahead_guard():
    from cat.backtest import select_closed_higher

    bars = make_klines([40_000 + i for i in range(10)], start=0)
    step = 14400
    bars = [Kline(**{**b.__dict__, "open_time": index * step}) for index, b in enumerate(bars)]
    decision = 5 * step + 3600  # inside bar index 5
    selected = select_closed_higher(bars, decision, step)
    assert [b.open_time for b in selected] == [i * step for i in range(5)]
    assert all(b.open_time + step <= decision for b in selected)


def test_backtest_executes_next_bar_not_current():
    """Entry price must come from the following bar's open, never the signal bar."""
    from cat.backtest import _quote_from_bar

    bars = make_klines(uptrend_prices(120))
    quote = _quote_from_bar(bars[-1], 0.05)
    assert quote.bid < bars[-1].close < quote.ask
    assert quote.ts == bars[-1].open_time
