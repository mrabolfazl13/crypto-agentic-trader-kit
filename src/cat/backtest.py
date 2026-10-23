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
from .strategy import Position, Proposal, analyze, propose
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
    return [bar for bar in higher_bars if bar.open_time + higher_step <= decision_time]


def _ts(position: Position) -> int:
    try:
        return int(datetime.fromisoformat(position.opened_at).timestamp())
    except ValueError:
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
) -> BacktestResult:
    params = params or default_params()
    timeframe = timeframe or config.timeframe
    step = seconds_per_bar(timeframe)
    confirm_tf = config.confirm_timeframe
    higher_step = seconds_per_bar(confirm_tf)
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
    while index < len(bars) - 1:
        window = bars[: index + 1]
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
        payload = build_decision(decision_id=f"bt-{index}", now=datetime.fromtimestamp(decision_time, tz=timezone.utc),
                                 snapshot=snap, proposal=proposal, risk=risk, analysis=analyze(snap, params))
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
            else:
                exit_price = nxt.open * (1 - slip)
                qty = risk.qty
                fee = qty * exit_price * fee_pct
                trade = state.record_close(symbol, qty, exit_price, fee, reason=proposal.reason,
                                          decision_id=payload["decision"]["decision_id"], at=nxt.open_time)
                if trade:
                    result.trades.append(BacktestTrade(symbol, 0, nxt.open_time, trade.entry_avg, exit_price,
                                                       trade.qty, trade.realized_pnl, trade.fees, trade.reason))

        # Intrabar stop/target on the bar we just traded into.
        check_bar = bars[index + 1]
        held = state.positions.get(symbol)
        if held:
            hit_stop = check_bar.low <= held.stop
            hit_target = check_bar.high >= held.target
            if hit_stop or hit_target:
                price = held.stop if hit_stop else held.target
                qty = held.qty
                fee = qty * price * fee_pct
                trade = state.record_close(symbol, qty, price, fee, reason="stop" if hit_stop else "target",
                                           decision_id=held.decision_id, at=check_bar.open_time)
                if trade:
                    result.trades.append(BacktestTrade(symbol, check_bar.open_time, check_bar.open_time,
                                                       trade.entry_avg, price, trade.qty, trade.realized_pnl, trade.fees, trade.reason))

        state.roll_periods(datetime.fromtimestamp(check_bar.open_time, tz=timezone.utc))
        result.equity_curve.append((check_bar.open_time, mark(index + 1)))
        index += 1

    # Close any residual position at the last close for a clean comparison.
    held = state.positions.get(symbol)
    if held:
        price = bars[-1].close
        fee = held.qty * price * fee_pct
        trade = state.record_close(symbol, held.qty, price, fee, reason="end_of_test", decision_id=held.decision_id, at=bars[-1].open_time)
        if trade:
            result.trades.append(BacktestTrade(symbol, _ts(held), bars[-1].open_time, trade.entry_avg, price, trade.qty,
                                               trade.realized_pnl, trade.fees, trade.reason))
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
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
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
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "avg_win": round(gross_win / len(wins), 4) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 4) if losses else None,
        "net_pnl": round(net, 4),
        "net_return_pct": round(net / state.allocated_capital * 100.0, 3),
        "fees_paid": round(result.fees_paid, 4),
        "max_drawdown_pct": round(result.max_drawdown_pct, 3),
        "sharpe_annualised_crude": round(sharpe, 3),
        "sortino_annualised_crude": round(sortino, 3),
        "bars": len(equity),
        "sharpe_note": "per-bar returns scaled by sqrt(bars/year); not a risk-free-adjusted figure and not a forecast",
    }
