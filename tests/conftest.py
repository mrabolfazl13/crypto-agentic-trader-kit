"""Shared fixtures: an isolated workspace and synthetic market data.

Everything here is offline so CI never needs exchange access or credentials.
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cat.config import load_config  # noqa: E402
from cat.data import DataQuality, Kline, MarketSnapshot, Quote, SymbolFilter  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
STEP = 3600
UTC = timezone.utc


def make_klines(prices: list[float], *, start: int = 1_700_000_000, closed: bool = True) -> list[Kline]:
    bars: list[Kline] = []
    for index, close in enumerate(prices):
        open_ = prices[index - 1] if index else close
        high = max(open_, close) * 1.004
        low = min(open_, close) * 0.996
        bars.append(
            Kline(
                open_time=start + index * STEP,
                open=open_,
                high=high,
                low=low,
                close=close,
                base_volume=100.0,
                quote_volume=100.0 * close,
                closed=closed,
            )
        )
    return bars


def uptrend_prices(n: int = 260, base: float = 50_000.0) -> list[float]:
    """Steady climb with periodic pullbacks: trending, but not RSI-stretched."""
    prices = [base]
    for index in range(1, n):
        if index % 7 == 0:
            prices.append(prices[-1] * 0.994)
        else:
            prices.append(prices[-1] * 1.004)
    return prices


def make_snapshot(
    symbol: str = "BTC_USDT",
    prices: list[float] | None = None,
    *,
    bid: float | None = None,
    ask: float | None = None,
    spread_pct: float = 0.02,
    is_fresh: bool = True,
    warnings: list[str] | None = None,
    min_quote: float = 3.0,
    price_precision: int = 1,
    qty_precision: int = 6,
    higher_prices: list[float] | None = None,
    now: float | None = None,
) -> MarketSnapshot:
    prices = prices or uptrend_prices()
    bars = make_klines(prices)
    last = prices[-1]
    bid = bid if bid is not None else last * (1 - spread_pct / 200)
    ask = ask if ask is not None else last * (1 + spread_pct / 200)
    ts = now if now is not None else bars[-1].open_time + STEP
    higher = make_klines(higher_prices or uptrend_prices(120, base=40_000.0), start=bars[0].open_time - 4 * STEP)
    return MarketSnapshot(
        symbol=symbol,
        timeframe="1h",
        klines=bars,
        higher_klines=higher,
        quote=Quote(symbol=symbol, bid=bid, ask=ask, ts=ts, source="test", bid_size=5.0, ask_size=5.0),
        cross_quote=None,
        filters=SymbolFilter(
            symbol=symbol, price_precision=price_precision, qty_precision=qty_precision,
            min_qty=0.000001, min_quote=min_quote, max_quote=5_000_000.0, tradable=True, fee_pct=0.1,
        ),
        quality=DataQuality(is_fresh=is_fresh, warnings=warnings or []),
        fetched_at=ts,
    )


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    shutil.copytree(REPO / "schemas", tmp_path / "schemas")
    shutil.copy(REPO / "config.yaml", tmp_path / "config.yaml")
    (tmp_path / "state").mkdir()
    return tmp_path


@pytest.fixture()
def config(workspace: Path):
    return load_config(root=workspace)


@pytest.fixture()
def now() -> datetime:
    return datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
