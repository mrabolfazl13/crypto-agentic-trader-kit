from __future__ import annotations

import pytest

from cat.data import Kline
from cat.research import validate_research_bars
from conftest import make_klines


def test_research_accepts_closed_contiguous_ohlcv_history():
    bars = make_klines([100.0, 101.0, 99.0, 102.0])
    validate_research_bars(bars, "1h")


def test_research_rejects_missing_market_candles():
    bars = make_klines([100.0, 101.0, 102.0, 103.0])
    bars[2] = Kline(
        open_time=bars[2].open_time + 3600,
        open=bars[2].open,
        high=bars[2].high,
        low=bars[2].low,
        close=bars[2].close,
        base_volume=bars[2].base_volume,
        quote_volume=bars[2].quote_volume,
        closed=True,
    )
    with pytest.raises(ValueError, match="not gap-free"):
        validate_research_bars(bars, "1h")


def test_research_rejects_unclosed_final_candle():
    bars = make_klines([100.0, 101.0, 102.0])
    bars[-1] = Kline(**{**bars[-1].__dict__, "closed": False})
    with pytest.raises(ValueError, match="unclosed candle"):
        validate_research_bars(bars, "1h")


def test_research_rejects_non_finite_ohlcv_values():
    bars = make_klines([100.0, 101.0, 102.0])
    bars[-1] = Kline(**{**bars[-1].__dict__, "close": float("nan")})
    with pytest.raises(ValueError, match="non-finite"):
        validate_research_bars(bars, "1h")


def test_research_rejects_invalid_ohlc_range():
    bars = make_klines([100.0, 101.0, 102.0])
    bars[-1] = Kline(**{**bars[-1].__dict__, "high": bars[-1].close - 1.0})
    with pytest.raises(ValueError, match="invalid OHLC range"):
        validate_research_bars(bars, "1h")
