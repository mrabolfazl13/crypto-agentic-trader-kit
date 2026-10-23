"""Mandatory safety scenarios from docs/TEST_PLAN_FA.md."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import yaml

from cat.audit import AuditLog, redact
from cat.broker import LiveExecutor, LiveTradingBlocked, Order, PaperBroker
from cat.config import ConfigError, load_config, owner_confirmation_path
from cat.engine import Engine
from cat.portfolio import AccountState
from cat.risk import evaluate
from cat.strategy import Position, Proposal
from conftest import make_snapshot


def buy_proposal(**overrides) -> Proposal:
    base = dict(
        action="BUY",
        confidence=0.82,
        rationale=["trend signals aligned", "stop/target computed from ATR"],
        entry=60_000.0,
        stop=59_000.0,
        target=63_000.0,
        analysis=None,
        reason="entry_signal",
    )
    base.update(overrides)
    return Proposal(**base)


def gate(config, state, snapshot, proposal, position=None, now=None, **kwargs):
    defaults = dict(
        now=now, prices={snapshot.symbol: snapshot.quote.mid}, kill_switch_active=False, clock_ok=True
    )
    defaults.update(kwargs)
    return evaluate(config, state, snapshot, proposal, position, **defaults)


def test_default_config_is_paper(workspace):
    cfg = yaml.safe_load((workspace / "config.yaml").read_text(encoding="utf-8")) if workspace else None
    assert cfg["trading"]["mode"] == "PAPER"
    assert cfg["trading"]["live_trading_enabled"] is False


def test_live_flag_off_with_bullish_model_cannot_reach_exchange(config, now):
    """The documented case: model says BUY, live is off -> nothing can be sent."""
    state = AccountState.new(config.allocated_capital)
    snapshot = make_snapshot()
    proposal = buy_proposal(entry=snapshot.quote.ask, stop=snapshot.quote.ask * 0.985, target=snapshot.quote.ask * 1.03)
    risk = gate(config, state, snapshot, proposal, now=now)
    assert risk.approved, risk.reasons  # approved *inside* the paper boundary

    executor = LiveExecutor(config, AuditLog(config.root / "state" / "audit" / "x.jsonl"))
    order = Order(
        client_order_id="live-1", decision_id=risk.symbol, symbol=snapshot.symbol, side="BUY", qty=risk.qty,
        limit_price=risk.entry, created_at=now.isoformat(), expires_at=now.timestamp(),
    )
    with pytest.raises(LiveTradingBlocked):
        executor.place(order)


def test_live_mode_without_owner_record_is_forced_to_paper(workspace):
    text = (workspace / "config.yaml").read_text(encoding="utf-8")
    payload = yaml.safe_load(text)
    payload["trading"]["mode"] = "LIVE"
    payload["trading"]["live_trading_enabled"] = True
    (workspace / "config.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    config = load_config(root=workspace)
    assert config.mode == "PAPER"
    assert any("owner confirmation" in w for w in config.warnings)


def test_owner_confirmation_ties_to_exact_config(workspace):
    payload = yaml.safe_load((workspace / "config.yaml").read_text(encoding="utf-8"))
    payload["trading"]["mode"] = "LIVE"
    payload["trading"]["live_trading_enabled"] = True
    (workspace / "config.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    from cat.config import config_digest

    probe = load_config(root=workspace)
    digest = config_digest(probe.raw)
    record = {
        "action": "ENABLE_LIVE_TRADING",
        "owner": "owner-user",
        "allocated_capital": 250,
        "live_trading_enabled": True,
        "config_digest": digest,
    }
    owner_confirmation_path(workspace).write_text(json.dumps(record), encoding="utf-8")
    assert load_config(root=workspace).mode == "LIVE"

    payload["risk"]["max_daily_loss_pct"] = 50.0  # owner loosens limits after signing
    (workspace / "config.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    assert load_config(root=workspace).mode == "PAPER"


def test_withdraw_permission_is_refused(workspace):
    payload = yaml.safe_load((workspace / "config.yaml").read_text(encoding="utf-8"))
    payload["trading"]["allow_withdrawals"] = True
    (workspace / "config.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ConfigError, match="allow_withdrawals"):
        load_config(root=workspace)


def test_leverage_request_is_disabled_with_warning(config, workspace):
    payload = yaml.safe_load((workspace / "config.yaml").read_text(encoding="utf-8"))
    payload["trading"]["allow_leverage"] = True
    (workspace / "config.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    loosened = load_config(root=workspace)
    assert loosened.allow_leverage is False and loosened.allow_margin is False
    assert any("margin/leverage" in w for w in loosened.warnings)


def test_daily_loss_limit_blocks_new_entries(config, now):
    state = AccountState.new(config.allocated_capital)
    state.day_realized_pnl = -state.allocated_capital * config.limits.max_daily_loss_pct / 100 - 0.01
    snapshot = make_snapshot()
    proposal = buy_proposal(entry=snapshot.quote.ask, stop=snapshot.quote.ask * 0.985, target=snapshot.quote.ask * 1.03)
    risk = gate(config, state, snapshot, proposal, now=now)
    assert not risk.approved and "daily loss limit" in risk.reasons[0]


def test_weekly_and_drawdown_limits_block(config, now):
    state = AccountState.new(config.allocated_capital)
    state.week_realized_pnl = -state.allocated_capital * config.limits.max_weekly_loss_pct / 100
    snapshot = make_snapshot()
    proposal = buy_proposal(entry=snapshot.quote.ask, stop=snapshot.quote.ask * 0.985, target=snapshot.quote.ask * 1.03)
    assert not gate(config, state, snapshot, proposal, now=now).approved

    drawn = AccountState.new(1000.0)
    drawn.peak_equity = 1000.0
    drawn.cash = 900.0
    snapshot2 = make_snapshot()
    risk = gate(config, drawn, snapshot2, proposal, now=now)
    assert not risk.approved and "drawdown" in risk.reasons[0]


def test_stale_data_rejects_orders(config, now):
    state = AccountState.new(config.allocated_capital)
    snapshot = make_snapshot(is_fresh=False, warnings=["stale_klines"])
    proposal = buy_proposal(entry=snapshot.quote.ask, stop=snapshot.quote.ask * 0.985, target=snapshot.quote.ask * 1.03)
    risk = gate(config, state, snapshot, proposal, now=now)
    assert not risk.approved and "untrusted data" in risk.reasons[0]


def test_kill_switch_rejects_everything(config, now):
    state = AccountState.new(config.allocated_capital)
    snapshot = make_snapshot()
    proposal = buy_proposal(entry=snapshot.quote.ask, stop=snapshot.quote.ask * 0.985, target=snapshot.quote.ask * 1.03)
    risk = gate(config, state, snapshot, proposal, now=now, kill_switch_active=True)
    assert not risk.approved and "kill switch" in risk.reasons[0]


def test_wide_spread_rejects(config, now):
    state = AccountState.new(config.allocated_capital)
    snapshot = make_snapshot(spread_pct=config.limits.max_spread_pct + 0.5)
    proposal = buy_proposal(entry=snapshot.quote.ask, stop=snapshot.quote.ask * 0.985, target=snapshot.quote.ask * 1.03)
    risk = gate(config, state, snapshot, proposal, now=now)
    assert not risk.approved and "spread" in risk.reasons[0]


def test_short_is_refused_in_base_build(config, now):
    state = AccountState.new(config.allocated_capital)
    snapshot = make_snapshot()
    proposal = buy_proposal(action="SELL", entry=None, stop=None, target=None, reason="exit_signal")
    risk = gate(config, state, snapshot, proposal, position=None, now=now)
    assert not risk.approved and "short" in risk.reasons[0]


def test_no_averaging_down_and_no_martingale(config, now):
    state = AccountState.new(config.allocated_capital)
    snapshot = make_snapshot()
    state.positions[snapshot.symbol] = Position(
        symbol=snapshot.symbol, qty=0.001, avg_entry=59_000.0, stop=58_000.0, target=62_000.0,
        decision_id="dec-1", opened_at=now.isoformat(),
    )
    proposal = buy_proposal(entry=snapshot.quote.ask, stop=snapshot.quote.ask * 0.985, target=snapshot.quote.ask * 1.03)
    risk = gate(config, state, snapshot, proposal, position=state.positions[snapshot.symbol], now=now)
    assert not risk.approved and "already open" in risk.reasons[0]


def test_consecutive_errors_forces_safe_state(config, now):
    state = AccountState.new(config.allocated_capital)
    state.consecutive_errors = config.limits.stop_on_consecutive_errors
    snapshot = make_snapshot()
    proposal = buy_proposal(entry=snapshot.quote.ask, stop=snapshot.quote.ask * 0.985, target=snapshot.quote.ask * 1.03)
    risk = gate(config, state, snapshot, proposal, now=now)
    assert not risk.approved and "consecutive errors" in risk.reasons[0]


def test_cooldown_after_loss(config, now):
    state = AccountState.new(config.allocated_capital)
    state.last_loss_at = now.timestamp() - 30
    snapshot = make_snapshot()
    proposal = buy_proposal(entry=snapshot.quote.ask, stop=snapshot.quote.ask * 0.985, target=snapshot.quote.ask * 1.03)
    risk = gate(config, state, snapshot, proposal, now=now)
    assert not risk.approved and "cooldown" in risk.reasons[0]


def test_duplicate_decision_is_refused(config, now):
    """An order with the same decision id can never be sent twice."""
    state = AccountState.new(config.allocated_capital)
    broker = PaperBroker(config=config, state=state, audit=AuditLog(config.root / "state" / "audit" / "idem.jsonl"))
    broker.place(decision_id="dec-same", symbol="BTC_USDT", side="BUY", qty=0.001, price=60_000.0, now=now, reason="x")
    with pytest.raises(ValueError, match="already executed"):
        broker.place(decision_id="dec-same", symbol="BTC_USDT", side="BUY", qty=0.001, price=60_000.0, now=now, reason="x")


def test_symbol_lock_blocks_contradictory_orders(config, now):
    state = AccountState.new(config.allocated_capital)
    broker = PaperBroker(config=config, state=state, audit=AuditLog(config.root / "state" / "audit" / "lock.jsonl"))
    broker.place(decision_id="dec-a", symbol="BTC_USDT", side="BUY", qty=0.001, price=60_000.0, now=now, reason="x")
    with pytest.raises(ValueError, match="resting order"):
        broker.place(decision_id="dec-b", symbol="BTC_USDT", side="SELL", qty=0.001, price=59_000.0, now=now, reason="x")


def test_unfilled_order_expires_before_retry(config, now):
    """Timeout -> cancel first; a blind duplicate send is impossible."""
    state = AccountState.new(config.allocated_capital)
    broker = PaperBroker(config=config, state=state, audit=AuditLog(config.root / "state" / "audit" / "exp.jsonl"))
    order = broker.place(decision_id="dec-exp", symbol="BTC_USDT", side="BUY", qty=0.001, price=60_000.0, now=now, reason="x")
    later = now.replace(second=0)
    assert broker.cancel_unfilled(later) == []
    order.expires_at = later.timestamp() - 1
    cancelled = broker.cancel_unfilled(later)
    assert [o.client_order_id for o in cancelled] == [order.client_order_id]
    assert order.status == "CANCELLED"


def test_secrets_never_reach_the_audit_log(config):
    log = AuditLog(config.root / "state" / "audit" / "secret.jsonl")
    log.append(
        "exchange_error",
        {"error": "auth failed api_key=ABCDEFGHIJKLMNOP secret: topsecretvalue", "token": "bearer-xyz"},
    )
    written = (config.root / "state" / "audit" / "secret.jsonl").read_text(encoding="utf-8")
    assert "topsecretvalue" not in written
    assert "ABCDEFGHIJKLMNOP" not in written
    assert "bearer-xyz" not in written
    assert "[REDACTED]" in written
    assert redact({"nested": {"api_secret": "abc"}})["nested"]["api_secret"] == "[REDACTED]"


def test_engine_run_cycle_cannot_place_live_orders(workspace, config, now, monkeypatch):
    """Even with an approved-looking BUY, the wired executor is paper-only."""
    engine = Engine(config)
    engine.clock_fn = lambda: {"acceptable": True, "skew_seconds": 0.0, "server_time": 0, "local_time": 0}
    snapshot = make_snapshot()
    result = engine.run_cycle(now=now, snapshots={snapshot.symbol: replace(snapshot, symbol="BTC_USDT")})
    assert result.mode == "PAPER"
    assert all(o.order_id is None or o.order_id.startswith("paper-") for o in result.outcomes)
    with pytest.raises(LiveTradingBlocked):
        engine.live.place(Order(
            client_order_id="x", decision_id="y", symbol="BTC_USDT", side="BUY", qty=1, limit_price=1,
            created_at="", expires_at=0,
        ))
