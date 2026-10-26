"""Pessimistic PAPER execution for the quant engine.

Two stages, both deterministic and independent of the live/paper broker:

1. ``generate_trades`` turns closed-bar signals into candidate trades. Entry is
   the **open of the 1m bar that starts when the signal bar closes**, so no fill
   can happen on information the signal did not have. Stops are checked before
   targets inside every 1m bar; a bar that touches both is booked as a stop.
2. ``simulate_portfolio`` sizes accepted candidates against shared cash, caps
   concurrent positions and charges fees on both legs.

Fees and slippage are applied to every fill. Nothing here can place an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .features import QuantParams


@dataclass(frozen=True)
class SignalTrade:
    symbol: str
    signal_ts: int
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    stop: float
    target: float
    reason: str
    bars: int
    mae_pct: float = 0.0
    mfe_pct: float = 0.0


@dataclass(frozen=True)
class SimSettings:
    initial_capital: float = 10_000.0
    fee_pct: float = 0.10
    slippage_pct: float = 0.05
    position_pct: float = 5.0
    max_open_positions: int = 10
    max_hold_hours: float = 24.0
    min_notional: float = 10.0
    # Pre-registered switch, off by default so every earlier result reproduces.
    # When > 0 an entry is dropped unless its ATR target could pay the whole round
    # trip (fee + slippage on both legs) this many times over. A signal whose target
    # lives inside the cost band has negative expectancy by construction, so trading
    # it is a guaranteed slow bleed rather than a hypothesis.
    min_edge_cost_ratio: float = 0.0
    # Pre-registered switch, off by default. When > 0 the position is sized so that
    # a stop-out costs this share of equity, instead of sizing by fixed notional.
    # Fixed notional risks a different amount on every symbol because the ATR stop
    # moves; equal risk per trade is what makes a drawdown comparable across them.
    risk_per_trade_pct: float = 0.0

    def round_trip_cost_pct(self) -> float:
        """Both legs, both frictions: what a trade must earn back before it earns anything."""
        return 2.0 * (self.fee_pct + self.slippage_pct)


def _exec_index(timestamps: np.ndarray, bar_end: int) -> int | None:
    """Index of the 1m bar whose open time equals the signal bar's close time."""
    index = int(np.searchsorted(timestamps, np.int64(bar_end), side="left"))
    if index >= len(timestamps) or int(timestamps[index]) != int(bar_end):
        return None
    return index


def _next_exit_array(signals: pd.DataFrame) -> np.ndarray:
    exits = signals["exit"].fillna(False).to_numpy(dtype=bool)
    next_exit = np.full(len(signals), -1, dtype=np.int64)
    last = -1
    for index in range(len(signals) - 1, -1, -1):
        if exits[index]:
            last = index
        next_exit[index] = last
    return next_exit


def generate_trades(bars: pd.DataFrame, signals: pd.DataFrame, *, symbol: str, strategy: str,
                    params: QuantParams, settings: SimSettings,
                    entry_after_ts: int | None = None) -> tuple[list[SignalTrade], dict[str, int]]:
    """Build non-overlapping candidate trades for one symbol from its closed-bar signals.

    ``entry_after_ts`` keeps indicator warmup visible while refusing fills before the
    evaluated window starts (used by walk-forward folds and the untouched hold-out).
    """
    rejections: dict[str, int] = {}
    if bars.empty or signals.empty:
        return [], {"no_data": 1}

    def reject(reason: str) -> None:
        rejections[reason] = rejections.get(reason, 0) + 1

    timestamps = bars["ts"].to_numpy(dtype="int64")
    opens = bars["open"].to_numpy(dtype="float64")
    highs = bars["high"].to_numpy(dtype="float64")
    lows = bars["low"].to_numpy(dtype="float64")
    closes = bars["close"].to_numpy(dtype="float64")
    slip = settings.slippage_pct / 100.0
    max_hold_bars = max(1, int(round(settings.max_hold_hours * 60)))
    bar_ends = signals["bar_end"].to_numpy(dtype="int64")

    if strategy == "buy_hold":
        complete = signals["complete"].fillna(False).to_numpy(dtype=bool)
        if entry_after_ts is not None:
            complete &= bar_ends >= int(entry_after_ts)
        first = int(np.argmax(complete)) if complete.any() else -1
        if first < 0:
            return [], {"no_complete_bar": 1}
        start = _exec_index(timestamps, int(bar_ends[first]))
        if start is None:
            return [], {"missing_execution_bar": 1}
        entry = float(opens[start]) * (1.0 + slip)
        exit_price = float(closes[-1]) * (1.0 - slip)
        path_low = float(lows[start:].min())
        path_high = float(highs[start:].max())
        return [SignalTrade(symbol=symbol, signal_ts=int(bar_ends[first]),
                            entry_ts=int(timestamps[start]), exit_ts=int(timestamps[-1]),
                            entry=entry, exit=exit_price, stop=0.0, target=0.0, reason="end_of_data",
                            bars=len(timestamps) - start,
                            mae_pct=max(0.0, 1.0 - path_low / entry),
                            mfe_pct=max(0.0, path_high / entry - 1.0))], rejections

    next_exit = _next_exit_array(signals)
    entries = signals["entry"].fillna(False).to_numpy(dtype=bool)
    atr_pct = signals["atr_pct"].to_numpy(dtype="float64")
    trades: list[SignalTrade] = []
    busy_until = -1

    for index in range(len(signals)):
        if not entries[index]:
            continue
        if not np.isfinite(atr_pct[index]) or atr_pct[index] <= 0:
            reject("invalid_atr")
            continue
        start = _exec_index(timestamps, int(bar_ends[index]))
        if start is None:
            reject("missing_execution_bar")
            continue
        if entry_after_ts is not None and int(timestamps[start]) < int(entry_after_ts):
            reject("before_window")
            continue
        if start <= busy_until:
            reject("overlapping_position")
            continue

        entry = float(opens[start]) * (1.0 + slip)
        stop_pct = min(max(params.atr_stop_mult * float(atr_pct[index]), 0.002), 0.10)
        target_pct = min(max(params.atr_target_mult * float(atr_pct[index]), 0.004), 0.25)
        if settings.min_edge_cost_ratio > 0.0:
            # Refuse signals whose whole move is eaten by costs: the target must pay
            # the round trip ``min_edge_cost_ratio`` times before it counts as an edge.
            required_target_pct = settings.min_edge_cost_ratio * settings.round_trip_cost_pct() / 100.0
            if target_pct < required_target_pct:
                reject("target_inside_cost_band")
                continue
        stop = entry * (1.0 - stop_pct)
        target = entry * (1.0 + target_pct)

        exit_signal = int(next_exit[index + 1]) if index + 1 < len(signals) else -1
        signal_exit_index: int | None = None
        if exit_signal >= 0:
            signal_exit_index = _exec_index(timestamps, int(bar_ends[exit_signal]))
            if signal_exit_index is None:
                reject("missing_exit_bar")
                signal_exit_index = None

        last = min(start + max_hold_bars - 1, len(timestamps) - 1)
        exit_index, exit_price, reason = last, float(closes[last]) * (1.0 - slip), "time"
        lowest, highest = entry, entry
        for offset in range(start, last + 1):
            low, high, open_ = float(lows[offset]), float(highs[offset]), float(opens[offset])
            lowest = min(lowest, low)
            highest = max(highest, high)
            if low <= stop:
                exit_index, exit_price, reason = offset, min(open_, stop) * (1.0 - slip), "stop"
                break
            if high >= target:
                exit_index, exit_price, reason = offset, max(open_, target) * (1.0 - slip), "target"
                break
            if signal_exit_index is not None and offset >= signal_exit_index:
                exit_index, exit_price, reason = offset, open_ * (1.0 - slip), "signal"
                break

        trades.append(SignalTrade(
            symbol=symbol, signal_ts=int(bar_ends[index]), entry_ts=int(timestamps[start]),
            exit_ts=int(timestamps[exit_index]), entry=entry, exit=exit_price, stop=stop, target=target,
            reason=reason, bars=exit_index - start + 1,
            mae_pct=max(0.0, 1.0 - lowest / entry), mfe_pct=max(0.0, highest / entry - 1.0),
        ))
        busy_until = exit_index

    if not trades and not rejections:
        rejections["no_entry_signal"] = len(signals)
    return trades, rejections


@dataclass
class _Position:
    trade: SignalTrade
    qty: float


def _metrics(trades: list[dict[str, Any]], equity_curve: list[list[float]], capital: float,
             rejections: dict[str, int], max_open_risk_pct: float) -> dict[str, Any]:
    nets = [trade["net"] for trade in trades]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value <= 0]
    gross_win = float(sum(wins))
    gross_loss = abs(float(sum(losses)))
    peak, max_dd = capital, 0.0
    for _ts, equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)
    final = float(equity_curve[-1][1]) if equity_curve else capital
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 2) if trades else None,
        "net_pnl": round(final - capital, 4),
        "net_return_pct": round((final / capital - 1.0) * 100.0, 4) if capital > 0 else 0.0,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "avg_win": round(gross_win / len(wins), 4) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 4) if losses else None,
        "fees": round(float(sum(trade["fees"] for trade in trades)), 4),
        "max_drawdown_pct": round(max_dd, 3),
        "max_open_risk_pct": round(max_open_risk_pct, 3),
        # Realized equity only moves when a trade closes, so the worst unrealized
        # excursion is tracked separately and the larger of the two is the honest
        # risk figure a gate should compare against.
        "effective_drawdown_pct": round(max(max_dd, max_open_risk_pct), 3),
        "final_equity": round(final, 4),
        "rejections": dict(sorted(rejections.items())),
        "note": "PAPER simulation on closed bars with fees and slippage; not a forecast",
    }


def simulate_portfolio(candidates: list[SignalTrade], *, settings: SimSettings,
                       rejections: dict[str, int] | None = None) -> dict[str, Any]:
    """Shared-cash portfolio pass over per-symbol candidates.

    Sizing uses equity measured at cost basis (cash + open position cost), which
    ignores unrealized PnL instead of inventing a mark for every minute.
    """
    rejections = dict(rejections or {})
    capital = float(settings.initial_capital)
    fee = settings.fee_pct / 100.0
    cash = capital
    ordered = sorted(candidates, key=lambda trade: (trade.entry_ts, trade.symbol, trade.signal_ts))
    open_positions: list[_Position] = []
    closed: list[dict[str, Any]] = []
    curve: list[list[float]] = []
    total_gross_value_seconds = 0.0
    worst_open_risk = 0.0

    def realize(position: _Position) -> None:
        nonlocal cash
        trade = position.trade
        gross = position.qty * (trade.exit - trade.entry)
        fees = position.qty * (trade.entry + trade.exit) * fee
        net = gross - fees
        cash += position.qty * trade.exit - position.qty * trade.exit * fee
        closed.append({
            "symbol": trade.symbol, "entry_ts": trade.entry_ts, "exit_ts": trade.exit_ts,
            "entry": round(trade.entry, 8), "exit": round(trade.exit, 8), "qty": round(position.qty, 8),
            "gross": round(gross, 6), "fees": round(fees, 6), "net": round(net, 6),
            "reason": trade.reason, "bars": trade.bars,
        })

    def mark_equity(timestamp: int) -> None:
        equity = cash + sum(position.qty * position.trade.entry for position in open_positions)
        curve.append([float(timestamp), round(equity, 6)])

    if ordered:
        curve.append([float(ordered[0].entry_ts - 1), capital])
    for candidate in ordered:
        for position in sorted(open_positions, key=lambda item: item.trade.exit_ts):
            if position.trade.exit_ts <= candidate.entry_ts:
                open_positions.remove(position)
                realize(position)
                mark_equity(position.trade.exit_ts)

        if len(open_positions) >= settings.max_open_positions:
            rejections["max_open_positions"] = rejections.get("max_open_positions", 0) + 1
            continue
        equity = cash + sum(position.qty * position.trade.entry for position in open_positions)
        budget = equity * settings.position_pct / 100.0
        if settings.risk_per_trade_pct > 0.0 and candidate.stop > 0.0 and candidate.entry > candidate.stop:
            # Equal risk per trade: size from the distance to the stop, never above the
            # notional cap, so a wide stop cannot silently risk more of the account.
            risk_budget = equity * settings.risk_per_trade_pct / 100.0
            risk_qty = risk_budget / (candidate.entry - candidate.stop)
            qty = min(budget / (candidate.entry * (1.0 + fee)), risk_qty)
        else:
            qty = budget / (candidate.entry * (1.0 + fee))
        cost = qty * candidate.entry * (1.0 + fee)
        if cost > cash:
            qty = cash / (candidate.entry * (1.0 + fee)) if cash > 0 else 0.0
            cost = qty * candidate.entry * (1.0 + fee)
        if qty <= 0 or qty * candidate.entry < settings.min_notional:
            rejections["min_notional"] = rejections.get("min_notional", 0) + 1
            continue
        cash -= cost
        open_positions.append(_Position(trade=candidate, qty=qty))
        open_risk = sum(position.qty * position.trade.entry * position.trade.mae_pct
                        for position in open_positions)
        worst_open_risk = max(worst_open_risk, open_risk / max(equity, 1e-9) * 100.0)
        total_gross_value_seconds += cost * max(candidate.exit_ts - candidate.entry_ts, 0)

    while open_positions:
        position = min(open_positions, key=lambda item: item.trade.exit_ts)
        open_positions.remove(position)
        realize(position)
        mark_equity(position.trade.exit_ts)

    if curve:
        curve.append([float(max(item["exit_ts"] for item in closed)), round(cash, 6)])
        first_ts, last_ts = curve[0][0], curve[-1][0]
        span = max(last_ts - first_ts, 1.0)
        exposure = total_gross_value_seconds / (capital * span) * 100.0
    else:
        exposure = 0.0
    metrics = _metrics(closed, curve, capital, rejections, worst_open_risk)
    metrics["exposure_pct"] = round(exposure, 3)
    metrics["avg_hold_hours"] = round(
        float(np.mean([(trade["exit_ts"] - trade["entry_ts"]) / 3600.0 for trade in closed])), 3
    ) if closed else None
    return {"metrics": metrics, "trades": closed, "equity_curve": curve, "rejections": rejections}


def equal_weight_buy_hold(closes: dict[str, tuple[float, float]], fee_pct: float = 0.0,
                          slippage_pct: float = 0.0) -> dict[str, Any]:
    """Benchmark from first/last close per symbol: equal weight, one round trip of costs.

    The reference pays the same entry/exit fee and slippage as a strategy trade so
    the comparison is not flattered by a cost-free buy & hold.
    """
    gross = [last / first - 1.0 for first, last in closes.values() if first > 0 and last > 0]
    net = []
    for first, last in closes.values():
        if not (first > 0 and last > 0):
            continue
        entry = first * (1.0 + slippage_pct / 100.0)
        exit_price = last * (1.0 - slippage_pct / 100.0)
        costs = (entry + exit_price) * fee_pct / 100.0
        net.append((exit_price - entry - costs) / entry)
    if not gross:
        return {"symbols": 0, "return_pct": None, "note": "no usable closes"}
    return {
        "symbols": len(gross),
        "return_pct": round(float(np.mean(net)) * 100.0, 4),
        "gross_return_pct": round(float(np.mean(gross)) * 100.0, 4),
        "median_return_pct": round(float(np.median(net)) * 100.0, 4),
        "positive_symbols": int(sum(1 for value in net if value > 0)),
        "fee_pct": fee_pct, "slippage_pct": slippage_pct,
        "note": "equal-weight buy & hold over each symbol's stored window, net of one round trip",
    }
