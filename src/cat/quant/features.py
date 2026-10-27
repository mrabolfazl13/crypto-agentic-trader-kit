"""Resampling and explainable indicators for the quant research engine.

Everything here works on *closed* bars only. A resampled bucket is usable only
when every expected 1m bar inside it is present, so a gap can never masquerade
as a real 15m/1h/4h candle. Indicators are computed with explicit ``min_periods``
so the warmup region stays NaN instead of silently using partial windows.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

import numpy as np
import pandas as pd

TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400}


@dataclass(frozen=True)
class QuantParams:
    ema_fast: int = 12
    ema_slow: int = 48
    rsi_period: int = 14
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    atr_target_mult: float = 3.0
    slope_lookback: int = 20
    slope_threshold: float = 0.0
    volume_avg_bars: int = 20
    volume_min_ratio: float = 0.0
    rsi_pullback_low: int = 35
    rsi_pullback_high: int = 55
    rsi_exit: int = 70
    breakout_lookback: int = 20
    exit_lookback: int = 10

    def as_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    def label(self) -> str:
        return (f"ema{self.ema_fast}/{self.ema_slow} rsi{self.rsi_period} "
                f"stop{self.atr_stop_mult:g}x target{self.atr_target_mult:g}x")

    def warmup_bars(self) -> int:
        return int(max(self.ema_slow, self.rsi_period, self.atr_period, self.slope_lookback,
                       self.volume_avg_bars, self.breakout_lookback) + 2)


PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "ema_fast": (5, 40), "ema_slow": (20, 120), "rsi_period": (5, 30), "atr_period": (5, 30),
    "atr_stop_mult": (0.8, 4.0), "atr_target_mult": (1.0, 8.0), "slope_lookback": (5, 60),
    "slope_threshold": (-0.02, 0.05), "volume_avg_bars": (5, 60), "volume_min_ratio": (0.0, 3.0),
    "rsi_pullback_low": (15, 45), "rsi_pullback_high": (40, 70), "rsi_exit": (55, 90),
    "breakout_lookback": (5, 60), "exit_lookback": (3, 40),
}
_INT_FIELDS = {"ema_fast", "ema_slow", "rsi_period", "atr_period", "slope_lookback",
               "volume_avg_bars", "rsi_pullback_low", "rsi_pullback_high", "rsi_exit",
               "breakout_lookback", "exit_lookback"}


def params_from_dict(base: QuantParams | None = None, overrides: dict[str, Any] | None = None) -> QuantParams:
    """Typed, range-checked overrides; an unknown key is an error, never ignored silently."""
    base = base or QuantParams()
    values = base.as_dict()
    for key, value in (overrides or {}).items():
        if key not in values:
            raise ValueError(f"unknown strategy parameter {key!r}")
        if key in _INT_FIELDS:
            if isinstance(value, bool) or int(value) != float(value):
                raise ValueError(f"{key} must be an integer, got {value!r}")
            values[key] = int(value)
        else:
            values[key] = float(value)
        low, high = PARAM_BOUNDS[key]
        if not low <= float(values[key]) <= high:
            raise ValueError(f"{key}={values[key]} is outside the audited bounds [{low:g}, {high:g}]")
    params = QuantParams(**values)
    if params.ema_fast >= params.ema_slow:
        raise ValueError(f"ema_fast ({params.ema_fast}) must be below ema_slow ({params.ema_slow})")
    if params.rsi_pullback_low >= params.rsi_pullback_high:
        raise ValueError("rsi_pullback_low must be below rsi_pullback_high")
    return params


def resample(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate 1m bars into calendar-aligned buckets labelled by their close time.

    ``bar_end`` is the decision timestamp: a bucket labelled 00:15 covers
    [00:00, 00:15) and becomes actionable only at 00:15, which is exactly the
    open time of the next 1m bar. Incomplete buckets are marked, never filled.
    """
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"unsupported timeframe {timeframe!r}")
    step = TIMEFRAME_SECONDS[timeframe]
    expected = step // 60
    if frame.empty:
        return pd.DataFrame(columns=["bar_end", "open", "high", "low", "close", "volume", "bars", "complete"])
    ts = frame["ts"].to_numpy(dtype="int64")
    bucket = (ts // step + 1) * step
    work = frame.assign(bar_end=bucket)
    grouped = work.groupby("bar_end", sort=True)
    out = grouped.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                      close=("close", "last"), volume=("volume", "sum"), bars=("ts", "count"))
    out = out.reset_index()
    out["complete"] = out["bars"] == expected
    return out[["bar_end", "open", "high", "low", "close", "volume", "bars", "complete"]]


def add_indicators(frame: pd.DataFrame, params: QuantParams) -> pd.DataFrame:
    """EMA trend, Wilder RSI/ATR, normalized momentum and relative volume."""
    out = frame.copy()
    close = out["close"].astype("float64")
    high = out["high"].astype("float64")
    low = out["low"].astype("float64")
    volume = out["volume"].astype("float64")

    out["ema_fast"] = close.ewm(span=params.ema_fast, adjust=False, min_periods=params.ema_fast).mean()
    out["ema_slow"] = close.ewm(span=params.ema_slow, adjust=False, min_periods=params.ema_slow).mean()

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / params.rsi_period, adjust=False, min_periods=params.rsi_period).mean()
    avg_loss = loss.ewm(alpha=1.0 / params.rsi_period, adjust=False, min_periods=params.rsi_period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, 100.0).where(avg_gain.notna() & avg_loss.notna())
    out["rsi"] = rsi

    prev_close = close.shift(1)
    true_range = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    out["atr"] = true_range.ewm(alpha=1.0 / params.atr_period, adjust=False,
                                min_periods=params.atr_period).mean()

    lookback = params.slope_lookback
    out["momentum"] = close / close.shift(lookback) - 1.0
    out["volume_ratio"] = volume / volume.rolling(params.volume_avg_bars, min_periods=params.volume_avg_bars).mean()
    out["breakout_high"] = high.rolling(params.breakout_lookback, min_periods=params.breakout_lookback).max().shift(1)
    out["exit_low"] = low.rolling(params.exit_lookback, min_periods=params.exit_lookback).min().shift(1)
    return out


def _signal_parts(df: pd.DataFrame, strategy: str, params: QuantParams) -> dict[str, Any]:
    """Conjunctive entry gates, exit rule and the cumulative funnel that explains them."""
    ready = (df["complete"].fillna(False).astype(bool)
             & df["ema_fast"].notna() & df["ema_slow"].notna()
             & df["rsi"].notna() & df["atr"].notna() & df["momentum"].notna())
    trend = df["ema_fast"] > df["ema_slow"]
    close_above = df["close"] > df["ema_slow"]
    momentum_up = df["momentum"] > params.slope_threshold
    rsi_band = df["rsi"].between(params.rsi_pullback_low, params.rsi_pullback_high)
    rsi_rising = df["rsi"] > df["rsi"].shift(1)
    volume_ok = df["volume_ratio"] >= params.volume_min_ratio
    breakout = df["close"] > df["breakout_high"]

    if strategy == "buy_hold":
        entry = pd.Series(False, index=df.index)
        exit_ = pd.Series(False, index=df.index)
        funnel: list[tuple[str, pd.Series]] = [("ready", ready)]
    elif strategy == "ema_trend":
        entry = trend & close_above & momentum_up
        exit_ = ~trend
        funnel = [("ready", ready), ("trend", ready & trend),
                  ("close_above_ema_slow", ready & trend & close_above),
                  ("momentum_above_threshold", ready & trend & close_above & momentum_up)]
    elif strategy == "trend_pullback":
        entry = trend & close_above & rsi_band & rsi_rising
        exit_ = (df["rsi"] > params.rsi_exit) | ~trend
        funnel = [("ready", ready), ("trend", ready & trend),
                  ("close_above_ema_slow", ready & trend & close_above),
                  ("rsi_in_pullback_band", ready & trend & close_above & rsi_band),
                  ("rsi_rising", ready & trend & close_above & rsi_band & rsi_rising)]
    elif strategy == "momentum":
        entry = momentum_up & close_above
        exit_ = (df["momentum"] < 0.0) | (df["close"] < df["ema_slow"])
        funnel = [("ready", ready), ("momentum_above_threshold", ready & momentum_up),
                  ("close_above_ema_slow", ready & momentum_up & close_above)]
    elif strategy == "mean_reversion":
        # F-015: this family is retired from the research plan. The oversold conjunct
        # below the slow EMA is nearly unsatisfiable (7/1768 bars per fold), and the
        # counter-trend variant measured under D-015 failed acceptance on both
        # windows. Kept unchanged so every stored result still reproduces.
        entry = (df["rsi"] < params.rsi_pullback_low) & close_above & rsi_rising
        exit_ = df["rsi"] > params.rsi_pullback_high
        funnel = [("ready", ready),
                  ("rsi_below_low", ready & (df["rsi"] < params.rsi_pullback_low)),
                  ("close_above_ema_slow", ready & (df["rsi"] < params.rsi_pullback_low) & close_above),
                  ("rsi_rising", ready & (df["rsi"] < params.rsi_pullback_low) & close_above & rsi_rising)]
    elif strategy == "breakout":
        entry = breakout & volume_ok
        exit_ = (df["close"] < df["exit_low"]) | (df["close"] < df["ema_slow"])
        funnel = [("ready", ready), ("close_above_prior_high", ready & breakout),
                  ("volume_ratio", ready & breakout & volume_ok)]
    else:
        raise ValueError(f"unknown strategy {strategy!r}")

    conditions = {
        "trend": trend, "close_above_ema_slow": close_above, "momentum_above_threshold": momentum_up,
        "rsi_in_pullback_band": rsi_band, "rsi_rising": rsi_rising,
        "volume_ratio": volume_ok, "close_above_prior_high": breakout,
    }
    return {"ready": ready, "entry": entry.fillna(False).astype(bool) & ready,
            "exit": exit_.fillna(False).astype(bool) & ready, "funnel": funnel,
            "conditions": conditions}


def signal_frame(frame: pd.DataFrame, strategy: str, params: QuantParams) -> pd.DataFrame:
    """Entry/exit booleans per closed signal bar plus the ATR the stops are sized from.

    Only complete buckets with every indicator defined can signal. Gates are
    conjunctive and written out so each rejection is explainable.
    """
    df = add_indicators(frame, params)
    parts = _signal_parts(df, strategy, params)
    df["entry"] = parts["entry"]
    df["exit"] = parts["exit"]
    df["atr_pct"] = (df["atr"] / df["close"]).where(df["atr"] > 0)
    return df


def gate_funnel(df: pd.DataFrame, strategy: str, params: QuantParams) -> dict[str, Any]:
    """Per-gate counts for an already-indicator'd frame.

    ``funnel`` is cumulative: each step is the conjunction of every gate before
    it, so a collapse between two steps names the filter that removed samples.
    ``conditions`` counts each raw predicate among ready bars, independent of the
    others. Both are measurements of the *same* signal frame the simulator used.
    """
    parts = _signal_parts(df, strategy, params)
    ready = parts["ready"]
    complete = df["complete"].fillna(False).astype(bool)
    indicators = df[["ema_fast", "ema_slow", "rsi", "atr", "momentum"]].notna().all(axis=1)
    return {
        "bars": int(len(df)),
        "complete_bars": int(complete.sum()),
        "indicator_ready_bars": int(indicators.sum()),
        "ready_bars": int(ready.sum()),
        "funnel": [{"gate": name, "passing": int(values.sum())} for name, values in parts["funnel"]],
        "conditions": {name: int((ready & values).sum()) for name, values in parts["conditions"].items()},
        "entry_signals": int(parts["entry"].sum()),
        "exit_signals": int(parts["exit"].sum()),
    }


STRATEGIES = ("buy_hold", "ema_trend", "trend_pullback", "momentum", "mean_reversion", "breakout")
