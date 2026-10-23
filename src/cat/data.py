"""Market data providers and the validation gate every cycle must pass."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from .http import DataError, HttpClient, utc_now

TIMEFRAMES = {
    "15m": (900, "15m", "15m"),
    "1h": (3600, "1h", "60m"),
    "4h": (14400, "4h", "4h"),
    "1d": (86400, "1d", "1d"),
}


class DataUnavailable(DataError):
    pass


@dataclass(frozen=True)
class Kline:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    base_volume: float
    quote_volume: float
    closed: bool


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    ts: float
    source: str
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> float:
        if self.mid <= 0:
            return math.inf
        return (self.ask - self.bid) / self.mid * 100.0


def estimate_slippage_pct(quote: Quote, cap_pct: float) -> float:
    """Cost of crossing to the touch plus a small impact allowance.

    Derived from the live book, not assumed, and never above the config cap.
    """
    half_spread = quote.spread_pct / 2.0
    return round(min(cap_pct, half_spread + 0.02), 6)


@dataclass(frozen=True)
class SymbolFilter:
    symbol: str
    price_precision: int
    qty_precision: int
    min_qty: float
    min_quote: float
    max_quote: float
    tradable: bool
    fee_pct: float

    def round_price(self, price: float) -> float:
        factor = 10 ** self.price_precision
        return math.floor(price * factor) / factor

    def round_price_up(self, price: float) -> float:
        """Buy limits round up to the tick, or they stop being marketable."""
        factor = 10 ** self.price_precision
        return math.ceil(price * factor - 1e-9) / factor

    def round_qty(self, qty: float) -> float:
        factor = 10 ** self.qty_precision
        return math.floor(qty * factor) / factor


@dataclass
class DataQuality:
    is_fresh: bool
    warnings: list[str] = field(default_factory=list)
    age_seconds: float | None = None
    sources_agree: bool = True

    def as_schema(self) -> dict[str, Any]:
        return {"is_fresh": self.is_fresh, "warnings": list(self.warnings)}


@dataclass
class MarketSnapshot:
    symbol: str
    timeframe: str
    klines: list[Kline]
    higher_klines: list[Kline] | None
    quote: Quote
    cross_quote: Quote | None
    filters: SymbolFilter
    quality: DataQuality
    fetched_at: float

    @property
    def last_price(self) -> float:
        return self.quote.mid


def seconds_per_bar(timeframe: str) -> int:
    try:
        return TIMEFRAMES[timeframe][0]
    except KeyError as exc:
        raise DataUnavailable(f"unsupported timeframe {timeframe}") from exc


class GateProvider:
    """Public Gate.io spot REST (no API key required)."""

    name = "gate"
    base = "https://api.gateio.ws"

    def __init__(self, client: HttpClient, *, filters_ttl_seconds: float = 3600.0):
        self.client = client
        self.filters_ttl_seconds = max(0.0, float(filters_ttl_seconds))
        self._filters: dict[str, tuple[float, SymbolFilter]] = {}
        self._filters_lock = threading.Lock()

    def klines(self, symbol: str, timeframe: str, limit: int = 300) -> list[Kline]:
        pair = normalize_pair(symbol)
        tf = TIMEFRAMES[timeframe][1]
        payload = self.client.get(
            f"{self.base}/api/v4/spot/candlesticks",
            {"currency_pair": pair, "interval": tf, "limit": min(limit, 1000)},
        )
        if not isinstance(payload, list) or not payload:
            raise DataUnavailable(f"no klines for {pair}")
        step = seconds_per_bar(timeframe)
        bars: list[Kline] = []
        for row in payload:
            try:
                ts, quote_vol, close, high, low, open_, base_vol, closed = row[:8]
                bars.append(
                    Kline(
                        open_time=int(float(ts)),
                        open=float(open_),
                        high=float(high),
                        low=float(low),
                        close=float(close),
                        base_volume=float(base_vol),
                        quote_volume=float(quote_vol),
                        closed=str(closed).lower() == "true",
                    )
                )
            except (TypeError, ValueError, IndexError) as exc:
                raise DataUnavailable(f"malformed kline row for {pair}: {row}") from exc
        bars.sort(key=lambda b: b.open_time)
        return bars

    def klines_range(self, symbol: str, timeframe: str, start_ts: int, end_ts: int, page: int = 800) -> list[Kline]:
        """Walk backwards over `from`/`to` windows so long histories are possible."""
        step = seconds_per_bar(timeframe)
        collected: dict[int, Kline] = {}
        cursor_end = end_ts
        for _ in range(60):
            if cursor_end <= start_ts:
                break
            window_start = max(start_ts, cursor_end - (page - 2) * step)
            rows = self.client.get(
                f"{self.base}/api/v4/spot/candlesticks",
                {"currency_pair": normalize_pair(symbol), "interval": TIMEFRAMES[timeframe][1],
                 "from": window_start, "to": cursor_end, "limit": page},
            )
            if not isinstance(rows, list) or not rows:
                break
            bars = [
                Kline(open_time=int(float(r[0])), open=float(r[5]), high=float(r[3]), low=float(r[4]),
                      close=float(r[2]), base_volume=float(r[6]), quote_volume=float(r[1]),
                      closed=str(r[7]).lower() == "true")
                for r in rows
            ]
            for bar in bars:
                collected[bar.open_time] = bar
            oldest = min(bar.open_time for bar in bars)
            if window_start == start_ts:
                break
            cursor_end = oldest - 1
        return [collected[key] for key in sorted(collected)]

    def quote(self, symbol: str) -> Quote:
        pair = normalize_pair(symbol)
        payload = self.client.get(f"{self.base}/api/v4/spot/tickers", {"currency_pair": pair})
        if not isinstance(payload, list) or not payload:
            raise DataUnavailable(f"no ticker for {pair}")
        row = payload[0]
        bid, ask = float(row["highest_bid"]), float(row["lowest_ask"])
        if bid <= 0 or ask <= 0:
            raise DataUnavailable(f"empty book for {pair}")
        return Quote(
            symbol=pair, bid=bid, ask=ask, ts=utc_now().timestamp(), source="gate",
            bid_size=float(row.get("highest_size") or 0), ask_size=float(row.get("lowest_size") or 0),
        )

    def filters(self, symbol: str) -> SymbolFilter:
        pair = normalize_pair(symbol)
        now = time.monotonic()
        if self.filters_ttl_seconds > 0:
            with self._filters_lock:
                cached = self._filters.get(pair)
            if cached and cached[0] > now:
                return cached[1]
        row = self.client.get(f"{self.base}/api/v4/spot/currency_pairs/{pair}")
        if not isinstance(row, dict):
            raise DataUnavailable(f"no pair metadata for {pair}")
        result = SymbolFilter(
            symbol=pair,
            price_precision=int(row.get("precision", 2)),
            qty_precision=int(row.get("amount_precision", 8)),
            min_qty=float(row.get("min_base_amount", 0) or 0),
            min_quote=float(row.get("min_quote_amount", 0) or 0),
            max_quote=float(row.get("max_quote_amount", 0) or 0),
            tradable=str(row.get("trade_status", "tradable")) == "tradable",
            fee_pct=float(row.get("fee", 0.2)) if row.get("fee") not in (None, "") else 0.2,
        )
        if self.filters_ttl_seconds > 0:
            with self._filters_lock:
                self._filters[pair] = (now + self.filters_ttl_seconds, result)
        return result

    def server_time(self) -> float:
        return float(self.client.get(f"{self.base}/api/v4/spot/time")["server_time"]) / 1000.0


class MexcCrossCheck:
    """Independent second source so a single broken feed cannot drive orders."""

    name = "mexc"
    base = "https://api.mexc.com"

    def __init__(self, client: HttpClient):
        self.client = client

    def quote(self, symbol: str) -> Quote:
        pair = normalize_pair(symbol).replace("_", "").upper()
        row = self.client.get(f"{self.base}/api/v3/ticker/bookTicker", {"symbol": pair})
        if not isinstance(row, dict):
            raise DataUnavailable(f"no bookTicker for {pair}")
        return Quote(
            symbol=pair, bid=float(row["bidPrice"]), ask=float(row["askPrice"]),
            ts=utc_now().timestamp(), source="mexc",
            bid_size=float(row.get("bidQty") or 0), ask_size=float(row.get("askQty") or 0),
        )

    def klines(self, symbol: str, timeframe: str, limit: int = 300) -> list[Kline]:
        pair = normalize_pair(symbol).replace("_", "").upper()
        tf = TIMEFRAMES[timeframe][2]
        payload = self.client.get(
            f"{self.base}/api/v3/klines", {"symbol": pair, "interval": tf, "limit": min(limit, 1000)}
        )
        if not isinstance(payload, list) or not payload:
            raise DataUnavailable(f"no klines for {pair}")
        bars = [
            Kline(
                open_time=int(row[0]) // 1000,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                base_volume=float(row[5]),
                quote_volume=float(row[7]) if len(row) > 7 else 0.0,
                closed=True,
            )
            for row in payload
        ]
        bars.sort(key=lambda b: b.open_time)
        # MEXC returns the in-progress bar as the last row; treat it as unclosed.
        step = seconds_per_bar(timeframe)
        bars[-1] = replace(bars[-1], closed=bars[-1].open_time + step <= utc_now().timestamp())
        return bars


def normalize_pair(symbol: str) -> str:
    cleaned = symbol.upper().replace("/", "_").replace("-", "_")
    parts = cleaned.split("_")
    if len(parts) != 2:
        raise DataUnavailable(f"symbol must look like BASE_QUOTE, got {symbol}")
    return f"{parts[0]}_{parts[1]}"


def validate_klines(bars: list[Kline], timeframe: str, now: float, staleness_limit: float) -> tuple[list[Kline], list[str]]:
    """Return usable closed bars plus the problems found; problems fail the gate."""
    warnings: list[str] = []
    step = seconds_per_bar(timeframe)
    if not bars:
        return [], ["no_data"]

    times = [b.open_time for b in bars]
    if len(set(times)) != len(times):
        warnings.append("duplicate_timestamps")
    if any(b.high < b.low for b in bars):
        warnings.append("impossible_ohlc")
    if any(b.close <= 0 or b.open <= 0 or b.high <= 0 or b.low <= 0 for b in bars):
        warnings.append("non_positive_prices")

    usable = [b for b in bars if b.closed]
    if not usable:
        return [], ["no_closed_bars"]

    for index in range(1, len(usable)):
        if usable[index].open_time - usable[index - 1].open_time != step:
            warnings.append("time_gap")
            break

    newest_close = usable[-1].open_time + step
    age = now - newest_close
    if age > max(staleness_limit, step * 2.5):
        warnings.append("stale_klines")
    return usable, warnings


def fetch_snapshot(
    gate: GateProvider,
    cross: MexcCrossCheck | None,
    symbol: str,
    timeframe: str,
    *,
    higher_timeframe: str | None = "4h",
    limit: int = 300,
    staleness_seconds: float = 120.0,
    max_price_disagreement_pct: float = 0.5,
) -> MarketSnapshot:
    now = utc_now().timestamp()
    warnings: list[str] = []
    bars_raw = gate.klines(symbol, timeframe, limit)
    usable, bar_warnings = validate_klines(bars_raw, timeframe, now, staleness_seconds)
    warnings.extend(bar_warnings)

    quote = gate.quote(symbol)
    filters = gate.filters(symbol)
    higher = None
    if higher_timeframe:
        try:
            higher_raw = gate.klines(symbol, higher_timeframe, limit)
            higher, higher_warnings = validate_klines(higher_raw, higher_timeframe, now, staleness_seconds)
            warnings.extend(f"higher_{w}" for w in higher_warnings)
        except DataUnavailable as exc:
            warnings.append(f"higher_timeframe_unavailable:{exc}")

    quote_age = now - quote.ts
    if quote_age > staleness_seconds:
        warnings.append("stale_quote")

    cross_quote = None
    sources_agree = True
    if cross is not None:
        try:
            cross_quote = cross.quote(symbol)
            divergence = abs(quote.mid - cross_quote.mid) / cross_quote.mid * 100.0
            if divergence > max_price_disagreement_pct:
                sources_agree = False
                warnings.append(f"source_price_disagreement:{divergence:.3f}%")
        except Exception as exc:  # cross-check is advisory, never blocks
            warnings.append(f"cross_check_unavailable:{type(exc).__name__}")

    if not filters.tradable:
        warnings.append("symbol_not_tradable")

    quality = DataQuality(
        is_fresh=not any(w in warnings for w in ("stale_klines", "stale_quote", "no_data", "duplicate_timestamps", "impossible_ohlc", "non_positive_prices"))
        and sources_agree,
        warnings=warnings,
        age_seconds=round(quote_age, 3),
        sources_agree=sources_agree,
    )
    return MarketSnapshot(
        symbol=normalize_pair(symbol),
        timeframe=timeframe,
        klines=usable,
        higher_klines=higher,
        quote=quote,
        cross_quote=cross_quote,
        filters=filters,
        quality=quality,
        fetched_at=now,
    )
