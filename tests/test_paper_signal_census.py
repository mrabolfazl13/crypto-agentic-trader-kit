"""Tests for the regime-gate census: what the shipped classifier may see, and what blocks it.

Synthetic bars only — no lake, no network, no engine. These lock the two things
the census's numbers depend on: the slice handed to the classifier must stop at
the bar being classified, and the reported blocking predicate must be the part of
the shipped conjunction that actually failed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import paper_signal_census as census  # noqa: E402
from cat.strategies import default_params  # noqa: E402
from cat.strategy import analyze  # noqa: E402

STEP = census.TIMEFRAME_SECONDS["4h"]
DAY = census.DAY_SECONDS
START = datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() - 400 * STEP


def _frame(closes: list[float], *, step: int) -> pd.DataFrame:
    rows = []
    for index, close in enumerate(closes):
        end = int(START) + (index + 1) * step
        rows.append({"bar_end": end, "open": close, "high": close * 1.001, "low": close * 0.999,
                     "close": close, "volume": 1000.0, "bars": step // 60, "complete": True})
    return pd.DataFrame(rows)


def _series(*, bars: int, drift: float, wiggle: float) -> list[float]:
    """A close series with a fixed per-bar drift and an alternating intra-bar swing."""
    prices = []
    level = 100.0
    for index in range(bars):
        level *= (1.0 + drift)
        prices.append(level * (1.0 + wiggle if index % 2 == 0 else 1.0 - wiggle))
    return prices


def test_snapshot_stops_at_the_bar_being_classified():
    """The classifier must not receive the bar after the one it is judging."""
    bars = census._klines(_frame(_series(bars=60, drift=0.001, wiggle=0.0), step=STEP), step=STEP)
    daily = census._klines(_frame(_series(bars=10, drift=0.002, wiggle=0.0), step=DAY), step=DAY)
    ends = [bar.open_time + DAY for bar in daily]
    index = 40
    snap = census._snapshot("AAAUSDT", "4h", bars, daily, ends, index, window=30)
    assert snap.klines[-1].open_time == bars[index].open_time
    assert len(snap.klines) == 30
    # nothing in the slice may reach past the classified bar's own bucket
    assert max(bar.open_time for bar in snap.klines) <= bars[index].open_time
    assert all(bar.open_time + DAY <= bars[index].open_time + STEP for bar in snap.higher_klines)


def test_a_calm_uptrend_passes_every_part_of_the_shipped_conjunction():
    params = default_params()
    bars = census._klines(_frame(_series(bars=320, drift=0.0025, wiggle=0.0005), step=STEP), step=STEP)
    daily = census._klines(_frame(_series(bars=60, drift=0.0025, wiggle=0.0005), step=DAY), step=DAY)
    ends = [bar.open_time + DAY for bar in daily]
    snap = census._snapshot("AAAUSDT", "4h", bars, daily, ends, 319, window=300)
    analysis = analyze(snap, params)
    flags = census.predicates(analysis, params)
    assert analysis.regime == "TRENDING_UP", analysis.indicators
    assert all(flags[key] for key in ("vol_ok", "atr_pct_ok", "slope_up", "bullish_majority"))


def test_a_hot_market_is_blocked_on_volatility_before_slope_or_breadth():
    """The live cycles blamed ``regime_filter`` for ~93% of symbols; this is what that looks like.

    A choppy series has plenty of up-slope days but blows the per-bar realized-vol
    ceiling, so the conjunction fails at its first quantitative part.
    """
    params = default_params()
    bars = census._klines(_frame(_series(bars=320, drift=0.0025, wiggle=0.03), step=STEP), step=STEP)
    daily = census._klines(_frame(_series(bars=60, drift=0.0025, wiggle=0.005), step=DAY), step=DAY)
    ends = [bar.open_time + DAY for bar in daily]
    snap = census._snapshot("AAAUSDT", "4h", bars, daily, ends, 319, window=300)
    analysis = analyze(snap, params)
    flags = census.predicates(analysis, params)
    assert analysis.regime != "TRENDING_UP"
    assert not flags["vol_ok"]
    assert flags["slope_up"], "the series still trends; volatility alone is what blocks it"
