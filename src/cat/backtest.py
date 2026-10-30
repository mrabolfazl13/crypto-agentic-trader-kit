"""Event-driven backtest on real candles, executing next-bar with costs.

No look-ahead: a decision at bar t only sees bars closed at or before t and is
filled at the open of bar t+1. When both stop and target touch one bar, the
pessimistic stop-first assumption is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .config import Config
from .data import Kline, MarketSnapshot, Quote, SymbolFilter, DataQuality, seconds_per_bar
from .decision import build_decision, validate_decision
from .http import HttpClient
from .indicators import closes
from .portfolio import AccountState, ClosedTrade
from .risk import evaluate
from .strategies import StrategyParams, default_params
from .strategy import Proposal, analyze, propose
from datetime import timezone as _tz  # re-export guard


@dataclass
class BacktestTrade:
    symbol: str
    entry_time: int
    exit_time: int
    entry: float
    exit: float
    qty: float
    pnl: float
    fees: float
    reason: str
    side: str = "long"
    entry_reason: str = ""
    decision_time: int = 0
    confidence: float | None = None
    regime: str = ""
    volume_state: str = ""
    indicators: dict[str, Any] = field(default_factory=dict)
    bullish: list[str] = field(default_factory=list)
    bearish: list[str] = field(default_factory=list)
    initial_stop: float | None = None
    initial_target: float | None = None
    final_stop: float | None = None
    final_target: float | None = None
    risk_pct: float | None = None
    rr_ratio: float | None = None
    capital_pct: float | None = None
    mfe_pct: float | None = None
    mae_pct: float | None = None
    holding_hours: float | None = None

    def evidence(self) -> dict[str, Any]:
        """Fields beyond the base ledger, persisted as one JSON blob per trade."""
        return {
            "side": self.side,
            "entry_reason": self.entry_reason,
            "decision_time": self.decision_time,
            "confidence": self.confidence,
            "regime": self.regime,
            "volume_state": self.volume_state,
            "indicators": self.indicators,
            "bullish": self.bullish,
            "bearish": self.bearish,
            "initial_stop": self.initial_stop,
            "initial_target": self.initial_target,
            "final_stop": self.final_stop,
            "final_target": self.final_target,
            "risk_pct": self.risk_pct,
            "rr_ratio": self.rr_ratio,
            "capital_pct": self.capital_pct,
            "mfe_pct": self.mfe_pct,
            "mae_pct": self.mae_pct,
            "holding_hours": self.holding_hours,
        }


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    bars: int
    start: str
    end: str
    trades: list[BacktestTrade] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    fees_paid: float = 0.0
    final_equity: float = 0.0
    max_drawdown_pct: float = 0.0
    buy_hold_pct: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)
    params_label: str = ""
    params: dict[str, Any] = field(default_factory=dict)


def select_closed_higher(higher_bars: list[Kline], decision_time: int, higher_step: int) -> list[Kline]:
    """Only bars whose own close time is at or before the decision moment.

    This is the look-ahead guard for the multi-timeframe confirmation.
    """
    return [bar for bar in higher_bars if bar.closed and bar.open_time + higher_step <= decision_time]


def _closed_ts(trade: Any) -> int:
    """Fill time of the trade that was just closed.

    The stop/target path fires on whichever bar touches the level, not only the
    entry bar, so the entry time must come from the trade itself — deriving it
    from the exit bar would report a holding time of zero for every stop.
    """
    try:
        return int(datetime.fromisoformat(trade.opened_at).timestamp())
    except (AttributeError, TypeError, ValueError):
        return 0


def _quote_from_bar(bar: Kline, spread_pct: float) -> Quote:
    half = bar.close * spread_pct / 200.0
    return Quote(symbol="", bid=bar.close - half, ask=bar.close + half, ts=bar.open_time, source="history")


def _snapshot(symbol: str, timeframe: str, window: list[Kline], higher: list[Kline] | None, bar: Kline, filters: SymbolFilter, spread_pct: float, staleness: float) -> MarketSnapshot:
    quote = _quote_from_bar(bar, spread_pct)
    quote = Quote(symbol=symbol, bid=quote.bid, ask=quote.ask, ts=bar.open_time + seconds_per_bar(timeframe), source="history")
    return MarketSnapshot(
        symbol=symbol, timeframe=timeframe, klines=window, higher_klines=higher, quote=quote, cross_quote=None,
        filters=filters, quality=DataQuality(is_fresh=True, warnings=[], age_seconds=0.0), fetched_at=quote.ts,
    )


def run_backtest(
    config: Config,
    symbol: str,
    *,
    bars: list[Kline],
    higher_bars: list[Kline],
    filters: SymbolFilter,
    timeframe: str | None = None,
    warmup: int = 80,
    assumed_spread_pct: float = 0.05,
    params: StrategyParams | None = None,
    propose_fn: Callable[..., Proposal] | None = None,
    window_bars: int | None = None,
    decision_every: int = 1,
) -> BacktestResult:
    params = params or default_params()
    timeframe = timeframe or config.timeframe
    step = seconds_per_bar(timeframe)
    confirm_tf = config.confirm_timeframe
    higher_step = seconds_per_bar(confirm_tf)
    # Production hands the classifier a fixed trailing window (`limit=300` in
    # GateProvider.klines); an unbounded replay window is a different protocol, so
    # a caller that means to match it passes it. Leaving it None keeps the
    # historical behaviour untouched.
    if window_bars is not None:
        if window_bars < 2:
            raise ValueError("window_bars must be at least 2 bars")
        if warmup > len(bars) - 2:
            raise ValueError("not enough bars to replay after warmup")
        warmup = max(warmup, window_bars)
    # The deployed engine decides on its cycle tick, not on every bar: `decision_every`
    # bars per tick (12 at 15m for a 3h cycle). Between ticks only the exit ledger runs,
    # because stop/target are exchange-side brackets. Default 1 keeps every historical
    # replay byte-identical.
    if decision_every < 1:
        raise ValueError("decision_every must be at least 1 bar")
    prices = closes(bars)
    result = BacktestResult(
        symbol=symbol, timeframe=timeframe, bars=len(bars),
        start=datetime.fromtimestamp(bars[0].open_time, tz=timezone.utc).isoformat(timespec="minutes"),
        end=datetime.fromtimestamp(bars[-1].open_time, tz=timezone.utc).isoformat(timespec="minutes"),
        params_label=params.label(), params=params.tuning(),
    )
    state = AccountState.new(config.allocated_capital or 1000.0)
    fee_pct = config.taker_fee_pct / 100.0

    def mark(index: int) -> float:
        return state.equity({symbol: bars[index].close})

    index = warmup
    evidence: dict[str, Any] | None = None

    def append_trade(trade: ClosedTrade | None, exit_ts: int, exit_price: float,
                     final_stop: float | None, final_target: float | None) -> None:
        """Persist a closed trade with the entry context captured at decision time."""
        nonlocal evidence
        if trade is None:
            return
        entry = trade.entry_avg
        entry_time = _closed_ts(trade)
        ev = evidence or {}
        initial_stop = ev.get("initial_stop")
        risk_pct = rr_ratio = None
        if initial_stop is not None and entry > initial_stop:
            risk_pct = (entry - initial_stop) / entry * 100.0
            initial_target = ev.get("initial_target")
            if initial_target is not None and initial_target > entry:
                rr_ratio = (initial_target - entry) / (entry - initial_stop)
        entry_fee = float(ev.get("entry_fee", 0.0) or 0.0)
        # The account cash ledger already paid both sides, but ClosedTrade only
        # knows the exit fee. Attribute the entry fee to this round trip so per-
        # trade PnL, win rate and profit factor are genuinely net of all costs.
        total_fees = round(float(trade.fees) + entry_fee, 8)
        net_pnl = round(float(trade.realized_pnl) - entry_fee, 8)
        result.trades.append(BacktestTrade(
            symbol=symbol, entry_time=entry_time, exit_time=exit_ts, entry=entry, exit=exit_price,
            qty=trade.qty, pnl=net_pnl, fees=total_fees, reason=trade.reason,
            side="long", entry_reason=ev.get("entry_reason", ""), decision_time=ev.get("decision_time", 0),
            confidence=ev.get("confidence"), regime=ev.get("regime", ""), volume_state=ev.get("volume_state", ""),
            indicators=ev.get("indicators", {}), bullish=ev.get("bullish", []), bearish=ev.get("bearish", []),
            initial_stop=initial_stop, initial_target=ev.get("initial_target"),
            final_stop=final_stop, final_target=final_target,
            risk_pct=round(risk_pct, 3) if risk_pct is not None else None,
            rr_ratio=round(rr_ratio, 3) if rr_ratio is not None else None,
            capital_pct=round(ev["capital_pct"], 3) if "capital_pct" in ev else None,
            mfe_pct=round(ev["mfe_pct"], 3) if "mfe_pct" in ev else None,
            mae_pct=round(ev["mae_pct"], 3) if "mae_pct" in ev else None,
            holding_hours=round((exit_ts - entry_time) / 3600.0, 3) if entry_time else None,
        ))
        evidence = None

    while index < len(bars) - 1:
        # A decision tick is the only moment production can propose; off-tick bars fall
        # straight through to the intrabar exit check below.
        if (index - warmup) % decision_every == 0:
            history = bars[: index + 1]
            # Clamped like production: at most window_bars bars, always ending on the
            # decision bar. Nothing newer than `window` reaches the classifier.
            window = history[-window_bars:] if window_bars else history
            decision_time = bars[index].open_time + step
            higher = select_closed_higher(higher_bars, decision_time, higher_step)
            snap = _snapshot(symbol, timeframe, window, higher[-200:] if len(higher) >= 60 else None, bars[index], filters, assumed_spread_pct, config.limits.stop_on_data_staleness_seconds)
            position = state.positions.get(symbol)
            signal = propose_fn or propose
            proposal = signal(snap, position, **{
                "confidence_threshold": config.confidence_threshold,
                "require_multiple_signals": config.require_multiple_signals,
                "no_trade_if_signals_conflict": config.no_trade_if_signals_conflict,
                "min_reward_risk_ratio": config.limits.min_reward_risk_ratio,
                "maker_fee_pct": config.maker_fee_pct,
                "taker_fee_pct": config.taker_fee_pct,
                "est_slippage_pct": config.limits.max_slippage_pct,
                "params": params,
            })
            risk = evaluate(
                config, state, snap, proposal, position,
                now=datetime.fromtimestamp(decision_time, tz=timezone.utc),
                prices={symbol: bars[index].close}, kill_switch_active=False, clock_ok=True,
            )
            analysis = analyze(snap, params)
            payload = build_decision(decision_id=f"bt-{index}", now=datetime.fromtimestamp(decision_time, tz=timezone.utc),
                                     snapshot=snap, proposal=proposal, risk=risk, analysis=analysis)
            if validate_decision(config.root, payload["decision"]):
                risk.approved = False
                risk.reasons = ["schema invalid"]

            if not risk.approved:
                key = proposal.reason if proposal.action in ("NO_TRADE", "HOLD") else (risk.reasons[0].split(":")[0] if risk.reasons else "unknown")
                result.rejected[key] = result.rejected.get(key, 0) + 1
            else:
                nxt = bars[index + 1]
                slip = config.limits.max_slippage_pct / 100.0
                if risk.action == "BUY":
                    entry = nxt.open * (1 + slip)
                    qty = risk.qty
                    cost = qty * entry
                    fee = cost * fee_pct
                    if cost + fee <= state.cash:
                        state.record_fill(cost=cost, fee=fee, qty=qty, price=entry, symbol=symbol,
                                          stop=risk.stop, target=risk.target, decision_id=payload["decision"]["decision_id"],
                                          at=nxt.open_time)
                        held = state.positions[symbol]
                        shift = entry - risk.entry
                        held.stop, held.target = risk.stop + shift, risk.target + shift
                        # Everything below was knowable at the decision bar; the exit
                        # ledger keeps it separate from the post-trade analysis.
                        evidence = {
                            "entry": entry,
                            "entry_fee": fee,
                            "entry_reason": proposal.reason,
                            "decision_time": decision_time,
                            "confidence": float(proposal.confidence),
                            "regime": analysis.regime,
                            "volume_state": analysis.volume_state,
                            "indicators": dict(analysis.indicators),
                            "bullish": list(analysis.bullish),
                            "bearish": list(analysis.bearish),
                            "initial_stop": held.stop,
                            "initial_target": held.target,
                            "capital_pct": cost / mark(index) * 100.0,
                            "mfe_pct": 0.0,
                            "mae_pct": 0.0,
                        }
                else:
                    exit_price = nxt.open * (1 - slip)
                    qty = risk.qty
                    fee = qty * exit_price * fee_pct
                    held_before = state.positions.get(symbol)
                    final_stop = held_before.stop if held_before else None
                    final_target = held_before.target if held_before else None
                    trade = state.record_close(symbol, qty, exit_price, fee, reason=proposal.reason,
                                              decision_id=payload["decision"]["decision_id"], at=nxt.open_time)
                    append_trade(trade, nxt.open_time, exit_price, final_stop, final_target)

        # Intrabar stop/target on the bar we just traded into.
        check_bar = bars[index + 1]
        held = state.positions.get(symbol)
        if held:
            if evidence is not None:
                entry_px = float(evidence["entry"])
                evidence["mfe_pct"] = max(evidence["mfe_pct"], (check_bar.high - entry_px) / entry_px * 100.0)
                evidence["mae_pct"] = min(evidence["mae_pct"], (check_bar.low - entry_px) / entry_px * 100.0)
            hit_stop = check_bar.low <= held.stop
            hit_target = check_bar.high >= held.target
            if hit_stop or hit_target:
                price = held.stop if hit_stop else held.target
                qty = held.qty
                fee = qty * price * fee_pct
                final_stop, final_target = held.stop, held.target
                trade = state.record_close(symbol, qty, price, fee, reason="stop" if hit_stop else "target",
                                           decision_id=held.decision_id, at=check_bar.open_time)
                append_trade(trade, check_bar.open_time, price, final_stop, final_target)

        state.roll_periods(datetime.fromtimestamp(check_bar.open_time, tz=timezone.utc))
        result.equity_curve.append((check_bar.open_time, mark(index + 1)))
        index += 1

    # Close any residual position at the last close for a clean comparison.
    held = state.positions.get(symbol)
    if held:
        # The fill happens at the close of the final bar, not its open; using the
        # open would turn a same-bar entry/exit into a zero-length trade.
        closed_at = bars[-1].open_time + step
        price = bars[-1].close
        fee = held.qty * price * fee_pct
        final_stop, final_target = held.stop, held.target
        trade = state.record_close(symbol, held.qty, price, fee, reason="end_of_test", decision_id=held.decision_id, at=closed_at)
        append_trade(trade, closed_at, price, final_stop, final_target)
        result.equity_curve[-1] = (bars[-1].open_time, state.cash)

    result.final_equity = state.cash
    result.fees_paid = state.fees_paid
    peak, dd = 0.0, 0.0
    for _, equity in result.equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            dd = max(dd, (peak - equity) / peak * 100.0)
    result.max_drawdown_pct = dd
    result.buy_hold_pct = (prices[-1] - prices[warmup]) / prices[warmup] * 100.0
    result.metrics = compute_metrics(result, state, step)
    result.params = params.tuning()
    return result


def compute_metrics(result: BacktestResult, state: AccountState, step: int) -> dict[str, Any]:
    trades = result.trades
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    # Trade.pnl is net of entry and exit fees. Reconstruct pre-fee PnL by
    # adding the round-trip fees recorded on each trade; keep net totals
    # separate so the portfolio-level profit factor never silently ignores costs.
    net_profit = sum(t.pnl for t in wins)
    net_loss = abs(sum(t.pnl for t in losses))
    gross_trade_pnls = [t.pnl + t.fees for t in trades]
    gross_win = sum(value for value in gross_trade_pnls if value > 0)
    gross_loss = abs(sum(value for value in gross_trade_pnls if value <= 0))
    equity = [e for _, e in result.equity_curve] or [state.allocated_capital]
    returns = [(equity[i] - equity[i - 1]) / equity[i - 1] for i in range(1, len(equity)) if equity[i - 1] > 0]
    mean = sum(returns) / len(returns) if returns else 0.0
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1) if len(returns) > 1 else 0.0
    std = variance ** 0.5
    bars_per_year = (365 * 24 * 3600) / step
    sharpe = (mean / std * (bars_per_year ** 0.5)) if std > 0 else 0.0
    downside = [r for r in returns if r < 0]
    d_std = (sum(r ** 2 for r in downside) / len(downside)) ** 0.5 if downside else 0.0
    sortino = (mean / d_std * (bars_per_year ** 0.5)) if d_std > 0 else 0.0
    net = result.final_equity - state.allocated_capital
    return {
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 2) if trades else None,
        "profit_factor": round(net_profit / net_loss, 3) if net_loss > 0 else None,
        "gross_profit": round(gross_win, 4),
        "gross_loss": round(gross_loss, 4),
        "net_profit": round(net_profit, 4),
        "net_loss": round(net_loss, 4),
        "avg_win": round(net_profit / len(wins), 4) if wins else None,
        "avg_loss": round(-net_loss / len(losses), 4) if losses else None,
        "net_pnl": round(net, 4),
        "net_return_pct": round(net / state.allocated_capital * 100.0, 3),
        "fees_paid": round(result.fees_paid, 4),
        "max_drawdown_pct": round(result.max_drawdown_pct, 3),
        "sharpe_annualised_crude": round(sharpe, 3),
        "sortino_annualised_crude": round(sortino, 3),
        "bars": len(equity),
        "sharpe_note": "per-bar returns scaled by sqrt(bars/year); not a risk-free-adjusted figure and not a forecast",
    }
