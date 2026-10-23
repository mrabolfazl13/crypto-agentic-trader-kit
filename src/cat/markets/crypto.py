"""The crypto (Gate spot) market spec — a thin adapter over the existing path.

Nothing is reimplemented here: every callable forwards to the same
``GateProvider`` / ``MexcCrossCheck`` / gate clock the engine used before the
market layer existed, so enabling ``trading.market: crypto`` is byte-for-byte the
behaviour of the previous build.
"""

from __future__ import annotations

from typing import Any

from ..data import GateProvider, MexcCrossCheck
from ..http import HttpClient, check_clock_skew
from .base import MarketSpec, register

GATE_TIME_URL = "https://api.gateio.ws/api/v4/spot/time"
GATE_TIME_PATH = ("server_time",)


def _provider(client: HttpClient, config: Any) -> GateProvider:
    return GateProvider(client, filters_ttl_seconds=config.filters_cache_seconds)


def _cross(client: HttpClient, config: Any) -> MexcCrossCheck | None:
    return MexcCrossCheck(client) if config.fallback_exchange == "mexc" else None


def _clock(client: HttpClient, config: Any) -> dict[str, Any]:
    return check_clock_skew(client, GATE_TIME_URL, GATE_TIME_PATH, config.max_clock_skew_seconds)


SPEC = register(
    MarketSpec(
        name="crypto",
        title="کریپتو — Gate spot",
        currency="USDT",
        quantity_unit="توکن",
        timeframes=("15m", "1h", "4h", "1d"),
        provider=_provider,
        cross=_cross,
        clock=_clock,
        notes=("۲۴/۷؛ مرجع تازگی داده ساعت واقعی است", "cross-check مستقل: MEXC bookTicker"),
    )
)
