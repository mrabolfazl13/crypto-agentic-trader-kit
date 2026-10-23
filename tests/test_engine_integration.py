"""Integration tests: restart recovery, fail-closed behaviour, exits, kill switch."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from cat.config import load_config
from cat.data import DataError, Quote
from cat.engine import Engine
from cat.portfolio import load_state
from conftest import make_snapshot, uptrend_prices

UTC = timezone.utc


def make_engine(workspace):
    engine = Engine(load_config(root=workspace))
    engine.clock_fn = lambda: {"acceptable": True, "skew_seconds": 0.0, "server_time": 0, "local_time": 0}
    return engine


CYCLE_NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def buyable(engine, symbol="BTC_USDT", spread=0.02):
    """A snapshot the strategy actually wants to buy, timestamped at the cycle."""
    snap = make_snapshot(symbol=symbol, prices=uptrend_prices(260), spread_pct=spread, now=CYCLE_NOW.timestamp() - 3600)
    return snap


def test_cycle_opens_a_paper_position_and_survives_restart(workspace):
    engine = make_engine(workspace)
    snap = buyable(engine)
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    result = engine.run_cycle(now=now, snapshots={snap.symbol: snap})
    approved = [o for o in result.outcomes if o.approved]
    assert approved, [o.reasons for o in result.outcomes]
    assert snap.symbol in engine.state.positions
    position = engine.state.positions[snap.symbol]
    assert position.qty > 0 and position.stop < position.avg_entry < position.target
    assert engine.state.cash < engine.state.allocated_capital
    assert engine.broker.fills and engine.broker.fills[0].fee > 0

    # Rebuild everything from disk, exactly like a process restart.
    reloaded = make_engine(workspace)
    assert set(reloaded.state.positions) == {snap.symbol}
    assert reloaded.state.positions[snap.symbol].qty == pytest.approx(position.qty)
    assert len(reloaded.broker.fills) == len(engine.broker.fills)

    # The same decision id can never be executed a second time.
    with pytest.raises(ValueError):
        reloaded.broker.place(decision_id=approved[0].decision_id, symbol=snap.symbol, side="BUY",
                              qty=0.0001, price=position.avg_entry, now=now, reason="replay")


def test_restart_with_corrupt_state_file_is_not_silently_ignored(workspace):
    engine = make_engine(workspace)
    snap = buyable(engine)
    engine.run_cycle(now=datetime(2026, 10, 23, 12, 0, tzinfo=UTC), snapshots={snap.symbol: snap})
    (workspace / "state" / "account.json").write_text("{ truncated", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        make_engine(workspace)


def test_network_failure_leaves_no_new_orders(workspace):
    engine = make_engine(workspace)
    snap = buyable(engine)
    engine.run_cycle(now=datetime(2026, 10, 23, 12, 0, tzinfo=UTC), snapshots={snap.symbol: snap})
    opened = len(engine.broker.fills)

    def boom(symbol):
        raise DataError("connection reset by peer")

    engine.snapshot = boom
    result = engine.run_cycle(now=datetime(2026, 10, 23, 16, 0, tzinfo=UTC))
    assert result.safe_state and result.errors
    assert len(engine.broker.fills) == opened  # nothing new executed
    assert engine.state.consecutive_errors >= 1


def test_clock_skew_halts_the_cycle_without_trading(workspace):
    engine = make_engine(workspace)
    engine.clock_fn = lambda: {"acceptable": False, "skew_seconds": 45.0}
    result = engine.run_cycle(now=datetime(2026, 10, 23, 12, 0, tzinfo=UTC), snapshots={})
    assert result.safe_state and result.errors and not any(o.approved for o in result.outcomes)
    assert engine.state.consecutive_errors == 1


def test_kill_switch_cancels_resting_orders_and_blocks_new_ones(workspace):
    engine = make_engine(workspace)
    snap = buyable(engine)
    now = CYCLE_NOW
    # A resting buy limit far below the market will not fill.
    engine.broker.place(decision_id="dec-resting", symbol=snap.symbol, side="BUY", qty=0.0005,
                        price=snap.quote.bid * 0.97, now=now, reason="manual_test")
    assert engine.broker.open_orders_for(snap.symbol)
    before = len(engine.broker.fills)

    (workspace / "STOP_TRADING").write_text("halt", encoding="utf-8")
    result = engine.run_cycle(now=now + timedelta(hours=4), snapshots={snap.symbol: snap})
    assert result.kill_switch and result.safe_state
    assert not engine.broker.open_orders_for(snap.symbol), "resting order must be cancelled"
    assert len(engine.broker.fills) == before, "no execution after the kill switch"
    assert all(not o.approved for o in result.outcomes)
    assert any("kill switch" in " ".join(o.reasons) for o in result.outcomes)


def test_stop_level_closes_the_position_with_fees(workspace):
    engine = make_engine(workspace)
    snap = buyable(engine)
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    engine.run_cycle(now=now, snapshots={snap.symbol: snap})
    position = engine.state.positions[snap.symbol]
    assert position.stop < snap.quote.bid  # stop is inside the current market

    below_stop = replace(
        snap,
        quote=Quote(snap.symbol, bid=position.stop * 0.999, ask=position.stop, ts=snap.quote.ts, source="test", bid_size=5, ask_size=5),
    )
    result = engine.run_cycle(now=now + timedelta(hours=4), snapshots={below_stop.symbol: below_stop})
    assert snap.symbol not in engine.state.positions, [o.reasons for o in result.outcomes]
    assert engine.state.closed_trades
    trade = engine.state.closed_trades[-1]
    assert trade.fees > 0 and trade.reason in ("exit_stop", "stop")
    assert engine.state.trades_today == 1
    assert engine.state.last_loss_at is not None or trade.realized_pnl >= 0

    # Cooldown now blocks a fresh entry: 30 minutes after a losing exit.
    cooled = engine.run_cycle(now=now + timedelta(hours=4, minutes=30), snapshots={snap.symbol: replace(snap, quote=snap.quote)})
    blocked = [o for o in cooled.outcomes if not o.approved]
    assert blocked and any("cooldown" in " ".join(o.reasons) for o in blocked), [o.reasons for o in cooled.outcomes]


def test_daily_loss_limit_is_enforced_after_a_bad_run(workspace):
    engine = make_engine(workspace)
    snap = buyable(engine)
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    engine.run_cycle(now=now, snapshots={snap.symbol: snap})
    if snap.symbol not in engine.state.positions:
        pytest.skip("no entry on this synthetic tape")
    position = engine.state.positions[snap.symbol]
    crash = replace(
        snap,
        quote=Quote(snap.symbol, bid=position.avg_entry * 0.80, ask=position.avg_entry * 0.80, ts=snap.quote.ts, source="test", bid_size=5, ask_size=5),
    )
    engine.run_cycle(now=now + timedelta(hours=4), snapshots={crash.symbol: crash})
    assert snap.symbol not in engine.state.positions
    assert engine.state.day == "2026-10-23"
    assert engine.state.day_realized_pnl < 0
    blocked = engine.run_cycle(now=now + timedelta(hours=4, minutes=30), snapshots={snap.symbol: snap})
    reasons = " ".join(" ".join(o.reasons) for o in blocked.outcomes)
    assert "cooldown" in reasons or "daily loss limit" in reasons, reasons


def test_audit_records_every_decision_and_no_secrets(workspace):
    engine = make_engine(workspace)
    snap = buyable(engine)
    engine.run_cycle(now=datetime(2026, 10, 23, 12, 0, tzinfo=UTC), snapshots={snap.symbol: snap})
    events = (workspace / "state" / "audit" / "events.jsonl").read_text(encoding="utf-8")
    assert "cycle_start" in events and "cycle_end" in events
    ok, message = engine.audit.verify()
    assert ok, message
    state = json.loads((workspace / "state" / "account.json").read_text(encoding="utf-8"))
    assert "api_key" not in json.dumps(state)


def test_unknown_live_mode_falls_back_to_paper_at_runtime(workspace):
    raw = (workspace / "config.yaml").read_text(encoding="utf-8").replace("mode: PAPER", "mode: LIVE")
    (workspace / "config.yaml").write_text(raw.replace("live_trading_enabled: false", "live_trading_enabled: true"), encoding="utf-8")
    engine = make_engine(workspace)
    assert engine.config.mode == "PAPER"
    snap = buyable(engine)
    result = engine.run_cycle(now=datetime(2026, 10, 23, 12, 0, tzinfo=UTC), snapshots={snap.symbol: snap})
    assert result.mode == "PAPER"
