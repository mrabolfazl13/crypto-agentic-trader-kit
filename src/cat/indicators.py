"""Indicators computed only from bars that are already closed (no look-ahead)."""

from __future__ import annotations

from .data import Kline


def closes(bars: list[Kline]) -> list[float]:
    return [b.close for b in bars]


def sma(values: list[float], period: int) -> list[float]:
    if period <= 0 or len(values) < period:
        return []
    out: list[float] = []
    window = sum(values[:period])
    out.append(window / period)
    for index in range(period, len(values)):
        window += values[index] - values[index - period]
        out.append(window / period)
    return out


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0 or len(values) < period:
        return []
    multiplier = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for value in values[period:]:
        out.append(value * multiplier + out[-1] * (1 - multiplier))
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    if len(values) <= period:
        return []
    gains, losses = [], []
    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = [_rsi_point(avg_gain, avg_loss)]
    for index in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
        out.append(_rsi_point(avg_gain, avg_loss))
    return out


def _rsi_point(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(bars: list[Kline], period: int = 14) -> list[float]:
    if len(bars) <= period:
        return []
    true_ranges: list[float] = []
    for index in range(1, len(bars)):
        prev_close = bars[index - 1].close
        bar = bars[index]
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close)))
    out = [sum(true_ranges[:period]) / period]
    for value in true_ranges[period:]:
        out.append((out[-1] * (period - 1) + value) / period)
    return out


def realized_vol_pct(returns: list[float], window: int = 30) -> float | None:
    if len(returns) < window:
        return None
    sample = returns[-window:]
    mean = sum(sample) / len(sample)
    variance = sum((value - mean) ** 2 for value in sample) / (len(sample) - 1)
    return variance ** 0.5  # inputs are already percent returns


def pct_changes(values: list[float]) -> list[float]:
    return [
        (values[index] - values[index - 1]) / values[index - 1] * 100.0
        for index in range(1, len(values))
        if values[index - 1] != 0
    ]


def slope(values: list[float], window: int = 10) -> float | None:
    """Least-squares slope normalised by the mean: % change per bar."""
    if len(values) < window:
        return None
    sample = values[-window:]
    n = len(sample)
    mean_x = (n - 1) / 2
    mean_y = sum(sample) / n
    denominator = sum((i - mean_x) ** 2 for i in range(n))
    if denominator == 0 or mean_y == 0:
        return None
    covariance = sum((i - mean_x) * (sample[i] - mean_y) for i in range(n))
    return covariance / denominator / mean_y * 100.0
