"""Alternative entry triggers for research scans.

Every lab function returns the same `Proposal` contract as `strategy.propose`, so the
deterministic Risk Gate still owns sizing, caps and the final decision. Only the entry
trigger differs; holding, stop/target handling and trend-break exits delegate to the
base strategy so a comparison never mixes in a different exit policy.
"""

from __future__ import annotations

from typing import Any, Callable

from .data import MarketSnapshot
from .indicators import closes, rsi
from .strategies import StrategyParams, default_params
from .strategy import Position, Proposal, analyze, propose

# Fail-closed diagnostic strings; nothing here promises a direction or a return.
REASON_INSUFFICIENT = "insufficient_history"


def _numbers(snapshot: MarketSnapshot, p: StrategyParams) -> dict[str, Any]:
    analysis = analyze(snapshot, p)
    ind = analysis.indicators
    return {
        "analysis": analysis,
        "close": ind.get("price"),
        "fast": ind.get("ema_fast"),
        "slow": ind.get("ema_slow"),
        "rsi": ind.get("rsi"),
        "atr": ind.get("atr"),
        "atr_pct": ind.get("atr_pct"),
    }


def _stack_ok(nums: dict[str, Any]) -> bool:
    return nums["fast"] is not None and nums["slow"] is not None and nums["fast"] > nums["slow"]


def _higher_ok(analysis: Any) -> bool:
    return any("higher timeframe trend agrees" in item for item in analysis.bullish)


_EXIT_KWARGS = (
    "confidence_threshold", "require_multiple_signals", "no_trade_if_signals_conflict",
    "min_reward_risk_ratio", "maker_fee_pct", "taker_fee_pct", "est_slippage_pct", "params",
)


def _exit(snapshot: MarketSnapshot, position: Position, kw: dict[str, Any]) -> Proposal:
    """All lab strategies share one exit policy; entry-only overrides must not leak.

    Extra entry switches (``min_atr_pct``, ``lookback``, …) are bound with
    ``functools.partial`` and would otherwise be forwarded into ``propose``,
    which accepts only the shared risk/exit contract.
    """
    return propose(snapshot, position, **{key: value for key, value in kw.items() if key in _EXIT_KWARGS})


def _confidence(analysis: Any, confirmations: list[bool], spread_pct: float | None) -> float:
    """Transparent heuristic: confirmations add 0.05 each, never a probability of profit."""
    score = 0.58 + 0.05 * sum(1 for flag in confirmations if flag)
    if analysis.regime == "TRENDING_UP":
        score += 0.04
    if analysis.volume_state == "ok":
        score += 0.03
    elif analysis.volume_state == "weak":
        score -= 0.03
    score -= 0.05 * max(0.0, (spread_pct or 0.0) / 0.30)
    return round(min(0.90, max(0.0, score)), 3)


def _entry(
    snapshot: MarketSnapshot,
    nums: dict[str, Any],
    *,
    confidence: float,
    reasons: list[str],
    kw: dict[str, Any],
    stop_mult: float | None = None,
    target_mult: float | None = None,
) -> Proposal:
    p: StrategyParams = kw.get("params") or default_params()
    analysis = nums["analysis"]
    atr_value = nums["atr"]
    if not atr_value or atr_value <= 0:
        return Proposal("NO_TRADE", 0.0, ["ATR unavailable, cannot size a stop"], None, None, None, analysis, "no_atr")

    entry = snapshot.quote.ask
    stop = entry - (stop_mult if stop_mult is not None else p.atr_stop_mult) * atr_value
    target = entry + (target_mult if target_mult is not None else p.atr_target_mult) * atr_value
    if stop <= 0:
        return Proposal("NO_TRADE", 0.0, ["computed stop is not positive"], None, None, None, analysis, "bad_stop")

    maker = float(kw["maker_fee_pct"])
    taker = float(kw["taker_fee_pct"])
    slippage = float(kw["est_slippage_pct"])
    required = float(kw["min_reward_risk_ratio"])
    round_trip = (maker + taker + slippage * 2) / 100.0
    net_reward = (target - entry) - entry * round_trip
    net_risk = (entry - stop) + entry * round_trip
    rr = net_reward / net_risk if net_risk > 0 else 0.0
    if rr < required:
        return Proposal(
            "NO_TRADE", 0.0,
            [f"net reward/risk {rr:.2f} < required {required:.2f} after fees and slippage"],
            None, None, None, analysis, "reward_risk",
        )
    if confidence < float(kw["confidence_threshold"]):
        return Proposal(
            "NO_TRADE", confidence,
            [f"confidence {confidence:.2f} below threshold {float(kw['confidence_threshold']):.2f}"],
            None, None, None, analysis, "confidence",
        )

    rationale = [analysis.summary] + reasons + [
        f"stop={stop:.2f} ({p.atr_stop_mult if stop_mult is None else stop_mult} ATR), "
        f"target={target:.2f} ({p.atr_target_mult if target_mult is None else target_mult} ATR)",
        f"net reward/risk {rr:.2f} including {round_trip * 100:.3f}% round-trip cost",
        "heuristic confidence from signal agreement; not a probability of profit",
    ]
    return Proposal("BUY", confidence, rationale, entry, stop, target, analysis, "entry_signal")


def _guard(snapshot: MarketSnapshot, kw: dict[str, Any], minimum: int) -> Proposal | None:
    p: StrategyParams = kw.get("params") or default_params()
    if len(snapshot.klines) < max(p.min_bars, minimum):
        analysis = analyze(snapshot, p)
        return Proposal(
            "NO_TRADE", 0.0, [f"need at least {max(p.min_bars, minimum)} closed bars"],
            None, None, None, analysis, REASON_INSUFFICIENT,
        )
    return None


def propose_breakout(snapshot: MarketSnapshot, position: Position | None, **kw: Any) -> Proposal:
    """Buy a close above the prior N-bar high while the trend stack and higher timeframe agree."""
    if position is not None:
        return _exit(snapshot, position, kw)
    lookback = int(kw.get("lookback", 30))
    min_atr_pct = float(kw.get("min_atr_pct", 0.4))
    max_atr_pct = float(kw.get("max_atr_pct", 6.0))
    require_higher = bool(kw.get("require_higher", True))
    require_regime = bool(kw.get("require_regime", False))
    fresh_only = bool(kw.get("fresh_only", False))
    guard = _guard(snapshot, kw, lookback + 2)
    if guard:
        return guard

    p: StrategyParams = kw.get("params") or default_params()
    nums = _numbers(snapshot, p)
    analysis = nums["analysis"]
    bars = snapshot.klines
    prior_high = max(bar.high for bar in bars[-lookback - 1 : -1])
    breakout = nums["close"] > prior_high
    if not breakout:
        return Proposal("NO_TRADE", 0.0, [f"close {nums['close']:.2f} below prior {lookback}-bar high {prior_high:.2f}"], None, None, None, analysis, "no_breakout")
    if fresh_only:
        # Only the bar that crosses the level is a signal. Without this, every
        # later bar that stays above the old high re-enters and pays the spread
        # again, which is what turned a trend into a fee stream in the scans.
        previous_high = max(bar.high for bar in bars[-lookback - 2 : -2])
        if bars[-2].close > previous_high:
            return Proposal(
                "NO_TRADE", 0.0,
                [f"not the first bar above the prior {lookback}-bar high"],
                None, None, None, analysis, "not_fresh",
            )
    stack = _stack_ok(nums)
    higher = _higher_ok(analysis)
    atr_pct = nums["atr_pct"]
    volatility_ok = atr_pct is not None and min_atr_pct <= atr_pct <= max_atr_pct
    rsi_ok = nums["rsi"] is not None and 50.0 <= nums["rsi"] <= 82.0

    if not breakout:
        return Proposal("NO_TRADE", 0.0, [f"close {nums['close']:.2f} below prior {lookback}-bar high {prior_high:.2f}"], None, None, None, analysis, "no_breakout")
    if not stack:
        return Proposal("NO_TRADE", 0.0, ["EMA fast is not above EMA slow"], None, None, None, analysis, "ema_stack")
    if require_higher and not higher:
        return Proposal("NO_TRADE", 0.0, ["higher timeframe trend does not agree"], None, None, None, analysis, "higher_timeframe")
    if require_regime and analysis.regime != (p.regime_gate or "TRENDING_UP"):
        return Proposal(
            "NO_TRADE", 0.0,
            [f"regime is {analysis.regime}; this stage only enters {p.regime_gate or 'TRENDING_UP'}"],
            None, None, None, analysis, "regime_filter",
        )
    if not volatility_ok:
        return Proposal("NO_TRADE", 0.0, [f"ATR% {atr_pct} outside {min_atr_pct}..{max_atr_pct}"], None, None, None, analysis, "volatility_band")
    if not rsi_ok:
        return Proposal("NO_TRADE", 0.0, [f"RSI {nums['rsi']} outside 50..82"], None, None, None, analysis, "rsi_band")

    confirmation = [
        stack, higher, breakout, analysis.volume_state == "ok", analysis.regime == "TRENDING_UP",
    ]
    confidence = _confidence(analysis, confirmation, snapshot.quote.spread_pct)
    reasons = [
        f"breakout: close {nums['close']:.2f} > prior {lookback}-bar high {prior_high:.2f}",
        "EMA fast above EMA slow",
    ]
    if higher:
        reasons.append("higher timeframe trend agrees (no look-ahead)")
    return _entry(snapshot, nums, confidence=confidence, reasons=reasons, kw=kw)


def propose_momentum(snapshot: MarketSnapshot, position: Position | None, **kw: Any) -> Proposal:
    """Buy sustained 4h momentum (ROC over N bars) while RSI is not stretched."""
    if position is not None:
        return _exit(snapshot, position, kw)
    lookback = int(kw.get("lookback", 20))
    min_roc = float(kw.get("min_roc_pct", 3.0))
    min_atr_pct = float(kw.get("min_atr_pct", 0.4))
    max_atr_pct = float(kw.get("max_atr_pct", 6.0))
    require_higher = bool(kw.get("require_higher", True))
    require_regime = bool(kw.get("require_regime", False))
    fresh_only = bool(kw.get("fresh_only", False))
    guard = _guard(snapshot, kw, lookback + 2)
    if guard:
        return guard

    p: StrategyParams = kw.get("params") or default_params()
    nums = _numbers(snapshot, p)
    analysis = nums["analysis"]
    prices = closes(snapshot.klines)
    roc = (prices[-1] - prices[-1 - lookback]) / prices[-1 - lookback] * 100.0 if prices[-1 - lookback] else 0.0
    previous_roc = (prices[-2] - prices[-2 - lookback]) / prices[-2 - lookback] * 100.0 if prices[-2 - lookback] else 0.0
    stack = _stack_ok(nums)
    higher = _higher_ok(analysis)
    atr_pct = nums["atr_pct"]
    volatility_ok = atr_pct is not None and min_atr_pct <= atr_pct <= max_atr_pct
    rsi_ok = nums["rsi"] is not None and 50.0 <= nums["rsi"] <= 74.0

    if roc < min_roc:
        return Proposal("NO_TRADE", 0.0, [f"{lookback}-bar momentum {roc:.2f}% below {min_roc}%"], None, None, None, analysis, "weak_momentum")
    if fresh_only and previous_roc >= min_roc:
        # Momentum that was already above the threshold last bar is not a new
        # signal; re-buying it on every bar is where the fee bleed came from.
        return Proposal(
            "NO_TRADE", 0.0,
            [f"momentum was already {previous_roc:.2f}% on the previous bar (not a fresh cross)"],
            None, None, None, analysis, "not_fresh",
        )
    if not stack:
        return Proposal("NO_TRADE", 0.0, ["EMA fast is not above EMA slow"], None, None, None, analysis, "ema_stack")
    if require_higher and not higher:
        return Proposal("NO_TRADE", 0.0, ["higher timeframe trend does not agree"], None, None, None, analysis, "higher_timeframe")
    if require_regime and analysis.regime != (p.regime_gate or "TRENDING_UP"):
        return Proposal(
            "NO_TRADE", 0.0,
            [f"regime is {analysis.regime}; this stage only enters {p.regime_gate or 'TRENDING_UP'}"],
            None, None, None, analysis, "regime_filter",
        )
    if not volatility_ok:
        return Proposal("NO_TRADE", 0.0, [f"ATR% {atr_pct} outside {min_atr_pct}..{max_atr_pct}"], None, None, None, analysis, "volatility_band")
    if not rsi_ok:
        return Proposal("NO_TRADE", 0.0, [f"RSI {nums['rsi']} outside 50..74"], None, None, None, analysis, "rsi_band")

    confirmation = [stack, higher, roc >= 2 * min_roc, analysis.volume_state == "ok", analysis.regime == "TRENDING_UP"]
    confidence = _confidence(analysis, confirmation, snapshot.quote.spread_pct)
    reasons = [f"{lookback}-bar momentum {roc:.2f}% with EMA fast above slow"]
    if higher:
        reasons.append("higher timeframe trend agrees (no look-ahead)")
    return _entry(snapshot, nums, confidence=confidence, reasons=reasons, kw=kw)


def propose_pullback(snapshot: MarketSnapshot, position: Position | None, **kw: Any) -> Proposal:
    """Buy an RSI reset inside an intact uptrend (dip, not a breakdown)."""
    if position is not None:
        return _exit(snapshot, position, kw)
    min_atr_pct = float(kw.get("min_atr_pct", 0.4))
    max_atr_pct = float(kw.get("max_atr_pct", 6.0))
    require_higher = bool(kw.get("require_higher", True))
    require_regime = bool(kw.get("require_regime", False))
    rsi_reset = float(kw.get("rsi_reset", 40.0))
    guard = _guard(snapshot, kw, 60)
    if guard:
        return guard

    p: StrategyParams = kw.get("params") or default_params()
    nums = _numbers(snapshot, p)
    analysis = nums["analysis"]
    series = rsi(closes(snapshot.klines), p.rsi_period)
    if len(series) < 2:
        return Proposal("NO_TRADE", 0.0, ["RSI history too short"], None, None, None, analysis, "rsi_band")
    crossed = series[-2] < rsi_reset <= series[-1] and series[-1] < rsi_reset + 15.0
    stack = _stack_ok(nums)
    higher = _higher_ok(analysis)
    atr_pct = nums["atr_pct"]
    volatility_ok = atr_pct is not None and min_atr_pct <= atr_pct <= max_atr_pct
    above_slow = nums["slow"] is not None and nums["close"] > nums["slow"]

    if not crossed:
        return Proposal("NO_TRADE", 0.0, [f"RSI did not reset and turn up from below {rsi_reset:g} (prev {series[-2]:.1f}, now {series[-1]:.1f})"], None, None, None, analysis, "no_pullback")
    if not stack:
        return Proposal("NO_TRADE", 0.0, ["EMA fast is not above EMA slow"], None, None, None, analysis, "ema_stack")
    if not above_slow:
        return Proposal("NO_TRADE", 0.0, ["price is below the slow EMA"], None, None, None, analysis, "below_slow")
    if require_higher and not higher:
        return Proposal("NO_TRADE", 0.0, ["higher timeframe trend does not agree"], None, None, None, analysis, "higher_timeframe")
    if require_regime and analysis.regime != (p.regime_gate or "TRENDING_UP"):
        return Proposal(
            "NO_TRADE", 0.0,
            [f"regime is {analysis.regime}; this stage only enters {p.regime_gate or 'TRENDING_UP'}"],
            None, None, None, analysis, "regime_filter",
        )
    if not volatility_ok:
        return Proposal("NO_TRADE", 0.0, [f"ATR% {atr_pct} outside {min_atr_pct}..{max_atr_pct}"], None, None, None, analysis, "volatility_band")

    confirmation = [stack, higher, crossed, analysis.volume_state != "collapsing", analysis.regime == "TRENDING_UP"]
    confidence = _confidence(analysis, confirmation, snapshot.quote.spread_pct)
    reasons = [f"RSI reset {series[-2]:.1f} -> {series[-1]:.1f} while price holds above EMA slow"]
    if higher:
        reasons.append("higher timeframe trend agrees (no look-ahead)")
    return _entry(snapshot, nums, confidence=confidence, reasons=reasons, kw=kw)


LAB_STRATEGIES: dict[str, Callable[..., Proposal]] = {
    "trend_v1": propose,
    "breakout_30": propose_breakout,
    "momentum_20": propose_momentum,
    "pullback_rsi": propose_pullback,
}
