"""Risk Engine — the hard gate. Plain, tested code, never a prompt instruction.

Every order must pass here. The gate recomputes size from config; it never
trusts the strategy's proposed notional. Anything ambiguous -> reject
(fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import Config
from .data import MarketSnapshot, estimate_slippage_pct
from .portfolio import AccountState
from .strategy import Position, Proposal


@dataclass
class RiskResult:
    approved: bool
    action: str
    reasons: list[str]
    symbol: str
    qty: float = 0.0
    notional: float = 0.0
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    est_fee_pct: float = 0.0
    est_slippage_pct: float = 0.0
    reward_risk_ratio: float | None = None
    checks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "action": self.action,
            "reasons": self.reasons,
            "symbol": self.symbol,
            "qty": self.qty,
            "notional": self.notional,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "reward_risk_ratio": self.reward_risk_ratio,
            "checks_passed": self.checks,
        }


def _reject(reason: str, action: str, symbol: str, checks: list[str]) -> RiskResult:
    return RiskResult(approved=False, action=action, reasons=[reason], symbol=symbol, checks=checks)


def evaluate(
    config: Config,
    state: AccountState,
    snapshot: MarketSnapshot,
    proposal: Proposal,
    position: Position | None,
    *,
    now: datetime,
    prices: dict[str, float],
    kill_switch_active: bool,
    clock_ok: bool,
) -> RiskResult:
    checks: list[str] = []
    symbol = snapshot.symbol
    limits = config.limits
    action = proposal.action

    def ok(name: str) -> None:
        checks.append(name)

    if kill_switch_active:
        return _reject("kill switch active", action, symbol, checks)
    ok("kill_switch")

    if config.mode == "PAUSED":
        return _reject("system paused", action, symbol, checks)
    ok("mode_running")

    if not clock_ok:
        return _reject("exchange clock skew unacceptable", action, symbol, checks)
    ok("clock")

    if not snapshot.quality.is_fresh:
        return _reject(f"untrusted data: {', '.join(snapshot.quality.warnings) or 'stale'}", action, symbol, checks)
    if any(w.startswith("stale_") or w in ("duplicate_timestamps", "impossible_ohlc", "non_positive_prices") for w in snapshot.quality.warnings):
        return _reject("data validation failed", action, symbol, checks)
    ok("data_quality")

    if symbol not in config.symbols:
        return _reject(f"{symbol} not in approved symbol list", action, symbol, checks)
    ok("symbol_allowlist")

    if action in ("HOLD", "NO_TRADE"):
        return _reject(f"no order for action {action}: {proposal.reason}", action, symbol, checks)

    if action == "SELL":
        if position is None:
            if not config.allow_short:
                return _reject("short selling disabled in base build", action, symbol, checks)
        else:
            if not proposal.reason.startswith("exit"):
                return _reject(f"exit reason not recognised: {proposal.reason!r}", action, symbol, checks)
            if not snapshot.quality.is_fresh:
                return _reject("cannot size an exit on untrusted data", action, symbol, checks)
            qty = snapshot.filters.round_qty(position.qty)
            if qty <= 0:
                return _reject("position rounds to zero on the exit side", action, symbol, checks)
            return RiskResult(
                approved=True, action="SELL", reasons=[proposal.reason] + proposal.rationale[:2], symbol=symbol,
                qty=qty, notional=qty * snapshot.quote.bid, entry=snapshot.quote.bid, stop=position.stop,
                target=position.target, est_fee_pct=config.taker_fee_pct, est_slippage_pct=limits.max_slippage_pct,
                reward_risk_ratio=None, checks=checks + ["exit_path"],
            )

    if action == "BUY":
        if position is not None:
            return _reject("position already open for symbol; no averaging down", action, symbol, checks)
        ok("no_existing_position")

        if len(state.positions) >= config.max_open_positions:
            return _reject(f"max_open_positions={config.max_open_positions} reached", action, symbol, checks)
        ok("open_positions")

        if state.trades_today >= limits.max_trades_per_day:
            return _reject(f"daily trade cap {limits.max_trades_per_day} reached", action, symbol, checks)
        ok("trade_count")

        if proposal.confidence < config.confidence_threshold:
            return _reject(
                f"confidence {proposal.confidence:.2f} < threshold {config.confidence_threshold:.2f}", action, symbol, checks
            )
        ok("confidence")

        if snapshot.quote.spread_pct > limits.max_spread_pct:
            return _reject(f"spread {snapshot.quote.spread_pct:.3f}% > cap {limits.max_spread_pct:.3f}%", action, symbol, checks)
        est_slippage = estimate_slippage_pct(snapshot.quote, limits.max_slippage_pct)
        if est_slippage > limits.max_slippage_pct:
            return _reject(f"estimated slippage {est_slippage:.3f}% > cap {limits.max_slippage_pct:.3f}%", action, symbol, checks)
        ok("spread")

        cooldown_seconds = limits.cooldown_minutes_after_loss * 60
        if state.last_loss_at and now.timestamp() - state.last_loss_at < cooldown_seconds:
            remaining = int(cooldown_seconds - (now.timestamp() - state.last_loss_at))
            return _reject(f"cooldown after loss, {remaining}s remaining", action, symbol, checks)
        ok("cooldown")

        if state.consecutive_errors >= limits.stop_on_consecutive_errors:
            return _reject(f"{state.consecutive_errors} consecutive errors; safe state required", action, symbol, checks)
        ok("error_budget")

        daily_limit = state.allocated_capital * limits.max_daily_loss_pct / 100.0
        if state.day_realized_pnl <= -daily_limit:
            return _reject(f"daily loss limit hit ({state.day_realized_pnl:.2f} <= -{daily_limit:.2f})", action, symbol, checks)
        weekly_limit = state.allocated_capital * limits.max_weekly_loss_pct / 100.0
        if state.week_realized_pnl <= -weekly_limit:
            return _reject(f"weekly loss limit hit ({state.week_realized_pnl:.2f} <= -{weekly_limit:.2f})", action, symbol, checks)
        drawdown = state.drawdown_pct(prices)
        if drawdown >= limits.max_drawdown_pct:
            return _reject(f"max drawdown {drawdown:.2f}% >= {limits.max_drawdown_pct:.2f}%", action, symbol, checks)
        ok("loss_limits")

        if proposal.entry is None or proposal.stop is None or proposal.target is None:
            return _reject("proposal missing entry/stop/target", action, symbol, checks)
        if proposal.stop >= proposal.entry or proposal.target <= proposal.entry:
            return _reject("entry/stop/target geometry invalid for a long", action, symbol, checks)
        ok("level_geometry")

        fees_pct = (config.maker_fee_pct + config.taker_fee_pct) / 100.0
        slip_pct = est_slippage / 100.0
        reward = proposal.target - proposal.entry - proposal.entry * (fees_pct + slip_pct)
        risk_units = proposal.entry - proposal.stop + proposal.entry * (fees_pct + slip_pct)
        if risk_units <= 0:
            return _reject("risk per unit is not positive after costs", action, symbol, checks)
        rr = reward / risk_units
        if rr < limits.min_reward_risk_ratio:
            return _reject(f"net reward/risk {rr:.2f} < {limits.min_reward_risk_ratio:.2f}", action, symbol, checks)
        ok("reward_risk")

        # max_account_allocation_pct caps total deployed value, position cap per trade.
        allocated_cap = state.allocated_capital * limits.max_account_allocation_pct / 100.0
        position_cap = state.allocated_capital * limits.max_position_pct_of_allocated_capital / 100.0
        risk_budget = state.allocated_capital * limits.max_risk_per_trade_pct / 100.0
        qty_by_risk = risk_budget / risk_units
        qty_by_position = position_cap / proposal.entry
        qty_by_exposure = max(0.0, allocated_cap - exposure_value(state, prices)) / proposal.entry
        qty_by_cash = max(0.0, state.cash * (1 - fees_pct) - 0.0) / (proposal.entry * (1 + fees_pct + slip_pct))
        qty = min(qty_by_risk, qty_by_position, qty_by_exposure, qty_by_cash)

        filters = snapshot.filters
        qty = filters.round_qty(qty)
        if qty <= 0:
            return _reject("computed position size rounds to zero", action, symbol, checks)
        if qty < filters.min_qty:
            return _reject(f"size {qty} below symbol min {filters.min_qty}", action, symbol, checks)
        notional = qty * proposal.entry
        if notional < filters.min_quote:
            return _reject(f"notional {notional:.2f} below symbol min quote {filters.min_quote}", action, symbol, checks)
        if filters.max_quote and notional > filters.max_quote:
            return _reject(f"notional {notional:.2f} above symbol max quote {filters.max_quote}", action, symbol, checks)
        if notional > position_cap:
            return _reject(f"notional {notional:.2f} above max position {position_cap:.2f}", action, symbol, checks)
        if notional > state.cash:
            return _reject(f"insufficient cash: need {notional:.2f}, have {state.cash:.2f}", action, symbol, checks)
        if exposure_value(state, prices) + notional > allocated_cap:
            return _reject(f"would exceed account allocation cap {allocated_cap:.2f}", action, symbol, checks)
        ok("sizing")

        return RiskResult(
            approved=True,
            action="BUY",
            reasons=proposal.rationale[:3] + [f"size capped by risk engine: {qty:.6f} {symbol}"],
            symbol=symbol,
            qty=qty,
            notional=notional,
            entry=proposal.entry,
            stop=proposal.stop,
            target=proposal.target,
            est_fee_pct=config.maker_fee_pct,
            est_slippage_pct=est_slippage,
            reward_risk_ratio=rr,
            checks=checks,
        )

    return _reject(f"unsupported action {action}", action, symbol, checks)


def exposure_value(state: AccountState, prices: dict[str, float]) -> float:
    return sum(prices.get(symbol, position.avg_entry) * position.qty for symbol, position in state.positions.items())
