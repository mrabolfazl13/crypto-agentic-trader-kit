"""Strategy layer: turns validated market data into a structured proposal.

This layer has no order authority. It only produces a TradeDecision that the
deterministic risk engine may reject.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from .data import MarketSnapshot
from .indicators import atr, closes, ema, pct_changes, realized_vol_pct, rsi, slope, sma
from .strategies import StrategyParams, default_params

EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14
ATR_STOP_MULT = 1.5
ATR_TARGET_MULT = 3.5
MIN_BARS = 60


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry: float
    stop: float
    target: float
    decision_id: str
    opened_at: str


@dataclass
class Analysis:
    regime: str
    summary: str
    bullish: list[str] = field(default_factory=list)
    bearish: list[str] = field(default_factory=list)
    indicators: dict[str, Any] = field(default_factory=dict)
    scenarios: dict[str, str] = field(default_factory=dict)
    volume_state: str = "unknown"
    invalidation: str = ""


@dataclass
class Proposal:
    action: str
    confidence: float
    rationale: list[str]
    entry: float | None
    stop: float | None
    target: float | None
    analysis: Analysis
    reason: str = ""


def decision_id() -> str:
    return f"dec-{uuid.uuid4().hex[:12]}"


def analyze(snapshot: MarketSnapshot, params: StrategyParams | None = None) -> Analysis:
    p = params or default_params()
    bars = snapshot.klines
    prices = closes(bars)
    volumes = [b.quote_volume for b in bars]
    fast = ema(prices, p.ema_fast)
    slow = ema(prices, p.ema_slow)
    rsi_series = rsi(prices, p.rsi_period)
    atr_series = atr(bars, p.atr_period)
    returns = pct_changes(prices)
    vol = realized_vol_pct(returns, p.vol_lookback)
    trend_slope = slope(prices, p.slope_lookback)
    volume_avg = sma(volumes, p.volume_avg_bars)
    ema_fast_key = f"ema{p.ema_fast}"
    ema_slow_key = f"ema{p.ema_slow}"

    last_price = prices[-1]
    last_fast = fast[-1] if fast else None
    last_slow = slow[-1] if slow else None
    last_rsi = rsi_series[-1] if rsi_series else None
    last_atr = atr_series[-1] if atr_series else None
    avg_volume = volume_avg[-1] if volume_avg else None

    higher_fast = higher_slow = None
    if snapshot.higher_klines and len(snapshot.higher_klines) >= p.ema_slow:
        hp = closes(snapshot.higher_klines)
        hf, hs = ema(hp, p.ema_fast), ema(hp, p.ema_slow)
        if hf and hs:
            higher_fast, higher_slow = hf[-1], hs[-1]

    bullish: list[str] = []
    bearish: list[str] = []
    if last_fast and last_slow:
        if last_fast > last_slow:
            bullish.append(f"{ema_fast_key} above {ema_slow_key} (uptrend structure)")
        else:
            bearish.append(f"{ema_fast_key} below {ema_slow_key} (downtrend structure)")
    if last_price and last_slow:
        if last_price > last_slow:
            bullish.append("price above long-term average")
        else:
            bearish.append("price below long-term average")
    if higher_fast and higher_slow:
        if higher_fast > higher_slow:
            bullish.append("higher timeframe trend agrees (no look-ahead)")
        else:
            bearish.append("higher timeframe trend disagrees")
    if last_rsi is not None:
        if last_rsi > p.rsi_stretched:
            bearish.append(f"RSI {last_rsi:.1f} stretched (mean-reversion risk)")
        elif p.rsi_bullish_low <= last_rsi <= p.rsi_bullish_high:
            bullish.append(f"RSI {last_rsi:.1f} in healthy momentum band")
        elif last_rsi < p.rsi_weak:
            bearish.append(f"RSI {last_rsi:.1f} weak momentum")

    if vol is None or trend_slope is None or last_atr is None:
        regime = "UNKNOWN"
    elif vol > p.vol_threshold or (last_atr / last_price * 100) > p.atr_pct_threshold:
        regime = "HIGH_VOLATILITY"
    elif trend_slope > p.slope_threshold and len(bullish) > len(bearish):
        regime = "TRENDING_UP"
    elif trend_slope < -p.slope_threshold and len(bearish) > len(bullish):
        regime = "TRENDING_DOWN"
    else:
        regime = "RANGING"

    # Participation band: a hard block only when volume is collapsing.
    recent_volume = sum(volumes[-3:]) / 3.0 if len(volumes) >= 3 else None
    if not avg_volume or recent_volume is None:
        volume_state = "unknown"
    elif recent_volume >= p.volume_weak_ratio * avg_volume:
        volume_state = "ok"
    elif recent_volume >= p.volume_collapse_ratio * avg_volume:
        volume_state = "weak"
    else:
        volume_state = "collapsing"

    indicators = {
        "price": round(last_price, 6),
        ema_fast_key: round(last_fast, 6) if last_fast else None,
        ema_slow_key: round(last_slow, 6) if last_slow else None,
        "ema_fast": round(last_fast, 6) if last_fast else None,
        "ema_slow": round(last_slow, 6) if last_slow else None,
        f"rsi{p.rsi_period}": round(last_rsi, 2) if last_rsi is not None else None,
        "rsi": round(last_rsi, 2) if last_rsi is not None else None,
        f"atr{p.atr_period}": round(last_atr, 6) if last_atr is not None else None,
        "atr": round(last_atr, 6) if last_atr is not None else None,
        "atr_pct": round(last_atr / last_price * 100, 4) if last_atr and last_price else None,
        "realized_vol_pct_per_bar": round(vol, 4) if vol is not None else None,
        "trend_slope_pct_per_bar": round(trend_slope, 4) if trend_slope is not None else None,
        "spread_pct": round(snapshot.quote.spread_pct, 4),
        "recent_volume3": round(recent_volume, 2) if recent_volume is not None else None,
        "avg_volume20": round(avg_volume, 2) if avg_volume else None,
        "volume_state": volume_state,
        "bars_used": len(bars),
        "strategy_version": p.label(),
    }
    summary = (
        f"{snapshot.symbol} {snapshot.timeframe}: regime={regime}, "
        f"price={last_price:.2f}, spread={snapshot.quote.spread_pct:.3f}%"
    )
    # Short histories (fresh listings) leave the EMAs unpopulated; say so instead of formatting None.
    scenarios = {
        "bullish": (
            f"continuation above {last_fast:.2f} toward +{p.atr_target_mult} ATR"
            if last_fast is not None else f"unavailable: needs {p.ema_fast} closes for EMA{p.ema_fast}"
        ),
        "bearish": (
            f"loss of {last_slow:.2f} opens {p.atr_stop_mult} ATR downside"
            if last_slow is not None else f"unavailable: needs {p.ema_slow} closes for EMA{p.ema_slow}"
        ),
        "neutral": "chop inside 1 ATR band; no edge after fees",
    }
    return Analysis(
        volume_state=volume_state,
        regime=regime,
        summary=summary,
        bullish=bullish,
        bearish=bearish,
        indicators=indicators,
        scenarios=scenarios,
        invalidation=f"close below {last_slow:.2f}" if last_slow else "insufficient data",
    )


def propose(
    snapshot: MarketSnapshot,
    position: Position | None,
    *,
    confidence_threshold: float,
    require_multiple_signals: bool,
    no_trade_if_signals_conflict: bool,
    min_reward_risk_ratio: float,
    maker_fee_pct: float,
    taker_fee_pct: float,
    est_slippage_pct: float,
    params: StrategyParams | None = None,
) -> Proposal:
    p = params or default_params()
    analysis = analyze(snapshot, p)
    prices = closes(snapshot.klines)
    ema_fast_key = f"ema{p.ema_fast}"
    ema_slow_key = f"ema{p.ema_slow}"
    if len(snapshot.klines) < p.min_bars:
        return Proposal("NO_TRADE", 0.0, [f"need at least {p.min_bars} closed bars"], None, None, None, analysis, "insufficient_history")

    last_atr = analysis.indicators.get("atr")
    last_price = prices[-1]
    if not last_atr or last_atr <= 0:
        return Proposal("NO_TRADE", 0.0, ["ATR unavailable, cannot size a stop"], None, None, None, analysis, "no_atr")

    bid, ask = snapshot.quote.bid, snapshot.quote.ask

    if position:
        stop_hit = bid <= position.stop
        target_hit = ask >= position.target
        trend_broken = analysis.indicators.get(ema_fast_key) is not None and (
            analysis.indicators[ema_fast_key] < analysis.indicators[ema_slow_key]
        )
        if stop_hit:
            return Proposal(
                "SELL", 1.0,
                [f"stop level {position.stop:.2f} reached (bid {bid:.2f})", "risk rule, not a forecast"],
                bid, position.stop, position.target, analysis, "exit_stop",
            )
        if target_hit:
            return Proposal(
                "SELL", 1.0,
                [f"target level {position.target:.2f} reached (ask {ask:.2f})", "take planned profit"],
                bid, position.stop, position.target, analysis, "exit_target",
            )
        if trend_broken:
            return Proposal(
                "SELL", 0.75,
                [f"{ema_fast_key} crossed below {ema_slow_key} while holding {position.symbol}", "trend thesis invalidated"],
                bid, position.stop, position.target, analysis, "exit_trend_broken",
            )
        return Proposal(
            "HOLD", 0.6,
            [f"holding {position.qty:.6f} {position.symbol} at avg {position.avg_entry:.2f}", "thesis intact"],
            None, position.stop, position.target, analysis, "hold_position",
        )

    if p.regime_gate and analysis.regime != p.regime_gate:
        return Proposal(
            "NO_TRADE", 0.0,
            [f"regime is {analysis.regime}; this version only enters {p.regime_gate}"],
            None, None, None, analysis, "regime_filter",
        )

    if analysis.volume_state == "collapsing":
        return Proposal(
            "NO_TRADE", 0.0, ["volume of the last 3 bars is below 0.6x its 20-bar average; participation is collapsing"],
            None, None, None, analysis, "volume_collapsing",
        )

    if no_trade_if_signals_conflict and analysis.bearish:
        return Proposal(
            "NO_TRADE", 0.0,
            [f"opposing evidence while trending up: {'; '.join(analysis.bearish[:2])}"],
            None, None, None, analysis, "signals_conflict",
        )
    if len(analysis.bullish) < 2 and require_multiple_signals:
        return Proposal(
            "NO_TRADE", 0.0, [f"only {len(analysis.bullish)} bullish signal(s); multiple signals required"],
            None, None, None, analysis, "single_signal",
        )

    entry = ask  # a buy crosses the spread
    stop = entry - p.atr_stop_mult * last_atr
    target = entry + p.atr_target_mult * last_atr
    if stop <= 0:
        return Proposal("NO_TRADE", 0.0, ["computed stop is not positive"], None, None, None, analysis, "bad_stop")

    gross_reward = target - entry
    gross_risk = entry - stop
    round_trip_cost_pct = (maker_fee_pct + taker_fee_pct + est_slippage_pct * 2) / 100.0
    net_reward = gross_reward - entry * round_trip_cost_pct
    net_risk = gross_risk + entry * round_trip_cost_pct
    rr = net_reward / net_risk if net_risk > 0 else 0.0

    if rr < min_reward_risk_ratio:
        return Proposal(
            "NO_TRADE", 0.0,
            [f"net reward/risk {rr:.2f} < required {min_reward_risk_ratio:.2f} after fees and slippage"],
            None, None, None, analysis, "reward_risk",
        )

    agreement = len(analysis.bullish)
    spread_penalty = max(0.0, (snapshot.quote.spread_pct or 0.0) / 0.30)
    volume_penalty = 0.06 if analysis.volume_state == "weak" else 0.0
    confidence = min(0.95, 0.50 + 0.09 * agreement + (0.08 if analysis.regime == p.regime_gate else 0.0) - 0.05 * spread_penalty - volume_penalty)
    if confidence < confidence_threshold:
        return Proposal(
            "NO_TRADE", round(confidence, 3),
            [f"confidence {confidence:.2f} below threshold {confidence_threshold:.2f}"],
            None, None, None, analysis, "confidence",
        )

    rationale = [analysis.summary] + analysis.bullish + [
        f"stop={stop:.2f} ({p.atr_stop_mult} ATR), target={target:.2f} ({p.atr_target_mult} ATR)",
        f"net reward/risk {rr:.2f} including {round_trip_cost_pct*100:.3f}% round-trip cost",
        f"invalidation: {analysis.invalidation}",
        f"strategy {p.label()}",
        "heuristic confidence from signal agreement; not a probability of profit",
    ]
    return Proposal("BUY", round(confidence, 3), rationale, entry, stop, target, analysis, "entry_signal", )
