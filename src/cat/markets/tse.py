"""TSE (Tehran Stock Exchange) provider over the public TSETMC data service.

Contract source: live endpoints on ``https://cdn.tsetmc.com/api`` recorded on
2026-10-24 (fixtures in ``tests/fixtures/tse_*.json``). Field names are TSETMC's
own abbreviations and are mapped explicitly below rather than passed through.

Deliberate choices:

* **The instrument code is the canonical symbol.** ``insCode`` (17-18 digits) is
  unique and stable; the 4-character board ticker (``lva``, e.g. «خوساز») is a
  display alias and is resolved through ``universe_tse.json``.
* **One market-wide request feeds every symbol.** ``GetMarketWatch`` returns the
  last best bid/ask, the day's allowed price band and the base volume for the
  whole board, so a 100-symbol cycle costs a handful of HTTP calls instead of
  hundreds. It is cached briefly for the same reason.
* **Prices are integer Rials, quantity is integer shares.** No invented tick
  table: the exchange's own ``pMin``/``pMax`` band is what the gate uses, and the
  lot size is documented as a venue rule to confirm with the broker.
* **No second independent source.** Crypto has MEXC as a cross-check; TSETMC is
  the only public feed here, so ``single_source_tsetmc`` is recorded as a
  warning on every snapshot instead of pretending a check exists.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote as urlquote
from zoneinfo import ZoneInfo

from ..data import DataUnavailable, Kline, Quote, SymbolFilter
from ..http import DataError, HttpClient
from . import calendar
from .base import MarketSpec, no_cross, register

BASE = "https://cdn.tsetmc.com/api"
# The CDN answers JSON only for browser-like agents; the default python UA gets
# an HTML shell back (verified while building this module).
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

TIMEFRAME = "1d"
WEEK_TIMEFRAME = "1w"
BAR_SECONDS = 86400
LOT_SIZE = 100.0  # one lot = 100 shares; venue rule — confirm with the broker before live use
TEHRAN = ZoneInfo("Asia/Tehran")

MARKETWATCH_PATH = "/ClosingPrice/GetMarketWatch"
DAILY_PATH = "/ClosingPrice/GetClosingPriceDailyList/{ins}/{top}"
BEST_LIMITS_PATH = "/BestLimits/{ins}"
CLOSING_INFO_PATH = "/ClosingPrice/GetClosingPriceInfo/{ins}"
MARKET_OVERVIEW_PATH = "/MarketData/GetMarketOverview/{flow}"
SEARCH_PATH = "/Instrument/GetInstrumentSearch/{query}"

# market=0 + paperTypes[0]=1 was the combination that returned the ordinary-share
# board (1017 instruments on 2026-10-24). Kept as a constant so the provenance in
# universe_tse.json and the runtime query can never drift apart.
MARKETWATCH_PARAMS = {"market": 0, "paperTypes[0]": 1, "withBestLimits": "false", "hEven": 0, "RefID": 0}

# TSETMC field map, from the recorded marketwatch payload.
WATCH_FIELDS = {
    "insCode": "کد ابزار",
    "insID": "ISIN",
    "lva": "تیکر تابلو",
    "lvc": "نام کامل",
    "py": "قیمت پایانی دیروز",
    "pf": "قیمت-first (نخستین معامله)",
    "pcl": "آخرین قیمت معامله",
    "pdv": "قیمت متوسط weighted",
    "pmd": "بهترین تقاضا",
    "pmo": "بهترین عرضه",
    "pmn": "کمترین قیمتtoday",
    "pmx": "بیشترین قیمتtoday",
    "pMin": "کف مجاز امروز",
    "pMax": "سقف مجاز امروز",
    "qtj": "حجم امروز (سهم)",
    "qtc": "ارزش امروز (ریال)",
    "ztt": "تعداد معاملات",
    "bv": "حجم مبنا",
    "pe": "P/E",
    "csv": "گروه",
}


def canonical_instrument(value: Any) -> str:
    cleaned = str(value or "").strip()
    if cleaned.isdigit():
        return cleaned
    raise DataUnavailable(f"TSE instrument code must be numeric, got {value!r}")


def _epoch_of(d_even: Any, h_even: Any, fallback: float | None = None) -> float | None:
    """Combine TSETMC's ``dEven`` (YYYYMMDD) and ``hEven`` (HHMMSS) into UTC epoch.

    Returns ``None`` rather than inventing a time: the marketwatch payload has
    ``dEven = 0`` on every row, so the caller must supply the session date from
    the market overview. A quote whose timestamp we cannot establish must fail,
    not silently claim "now".
    """
    try:
        day = int(d_even)
        hhmmss_raw = int(h_even or 0)
        if day <= 0 or hhmmss_raw <= 0:
            return fallback
        hhmmss = f"{hhmmss_raw:06d}"
        stamp = datetime(
            day // 10000, (day // 100) % 100, day % 100,
            int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6]), tzinfo=TEHRAN,
        )
        return stamp.timestamp()
    except (TypeError, ValueError):
        return fallback


def _session_open_epoch(d_even: int) -> float:
    """A daily bar is anchored at the session open (09:00 Tehran) of its trade date."""
    try:
        day = datetime(d_even // 10000, (d_even // 100) % 100, d_even % 100, 9, 0, tzinfo=TEHRAN)
    except ValueError:
        return float(d_even)
    return day.timestamp()


def week_anchor(day: date) -> date:
    """Iranian trading week starts on Saturday: map Mon=0..Sun=6 to Saturday."""
    return day - timedelta(days=(day.weekday() - 5) % 7)


def weekly_from_daily(bars: list[Kline]) -> list[Kline]:
    """Aggregate daily bars into Saturday-anchored weekly bars.

    Arithmetic on real closes only — no interpolation, no invented intraday data.
    A week is ``closed`` when every bar inside it is closed *and* a later week
    exists, so the running week never enters the signal set.
    """
    groups: dict[int, list[Kline]] = {}
    for bar in bars:
        anchor = week_anchor(datetime.fromtimestamp(bar.open_time, tz=TEHRAN).date())
        key = int(datetime.combine(anchor, clock_time(9, 0), tzinfo=TEHRAN).timestamp())
        groups.setdefault(key, []).append(bar)
    keys = sorted(groups)
    weekly: list[Kline] = []
    for index, key in enumerate(keys):
        rows = sorted(groups[key], key=lambda bar: bar.open_time)
        weekly.append(
            Kline(
                open_time=key,
                open=rows[0].open,
                high=max(row.high for row in rows),
                low=min(row.low for row in rows),
                close=rows[-1].close,
                base_volume=sum(row.base_volume for row in rows),
                quote_volume=sum(row.quote_volume for row in rows),
                closed=all(row.closed for row in rows) and index < len(keys) - 1,
            )
        )
    return weekly


class InstrumentIndex:
    """Ticker/ISIN -> insCode resolver backed by the dated universe snapshot."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.meta: dict[str, Any] = {}
        self.rows: list[dict[str, Any]] = []
        self._by_code: dict[str, dict[str, Any]] = {}
        self._by_ticker: dict[str, dict[str, Any]] = {}
        self._by_isin: dict[str, dict[str, Any]] = {}
        self.loaded_at: float | None = None
        self.error: str | None = "universe_tse.json not loaded"

    def load(self) -> "InstrumentIndex":
        if self.path is None or not self.path.exists():
            self.error = "universe_tse.json not found; run python tools/build_tse_universe.py"
            return self
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.error = f"universe_tse.json unreadable: {type(exc).__name__}"
            return self
        instruments = payload.get("instruments") if isinstance(payload.get("instruments"), list) else []
        self.rows = [row for row in instruments if isinstance(row, dict) and row.get("insCode")]
        self.meta = {key: value for key, value in payload.items() if key != "instruments"}
        self._by_code, self._by_ticker, self._by_isin = {}, {}, {}
        for row in self.rows:
            code = str(row["insCode"])
            self._by_code[code] = row
            ticker = str(row.get("ticker") or "").strip()
            if ticker:
                self._by_ticker.setdefault(ticker.lower(), row)
            isin = str(row.get("isin") or "").strip()
            if isin:
                self._by_isin.setdefault(isin.upper(), row)
        self.loaded_at = payload.get("generated_at")
        self.error = None
        return self

    def resolve(self, value: str) -> dict[str, Any] | None:
        text = str(value or "").strip()
        if not text:
            return None
        if text.isdigit():
            return self._by_code.get(text)
        if text.upper().startswith("IRO"):
            return self._by_isin.get(text.upper())
        return self._by_ticker.get(text.lower())

    def row_for(self, code: str) -> dict[str, Any] | None:
        return self._by_code.get(str(code))

    def summary(self) -> dict[str, Any]:
        return {
            "available": bool(self.rows),
            "count": len(self.rows),
            "generated_at": self.loaded_at,
            "source": self.meta.get("source"),
            "digest": self.meta.get("digest"),
            "error": self.error,
        }


class TseProvider:
    """Public TSETMC REST read path (no credentials, no order placement)."""

    name = "tsetmc"
    base = BASE
    #: freshness/clock semantics live in the market spec; the provider only answers data.
    supports_live = False

    def __init__(
        self,
        client: HttpClient,
        *,
        index: InstrumentIndex | None = None,
        fee_pct: float = 0.0,
        watch_ttl_seconds: float = 20.0,
        filters_ttl_seconds: int = 3600,
    ):
        self.client = client
        self.index = index or InstrumentIndex()
        self.fee_pct = fee_pct
        self.watch_ttl_seconds = watch_ttl_seconds
        self.filters_ttl_seconds = filters_ttl_seconds
        self._lock = threading.Lock()
        self._watch: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._daily: dict[tuple[str, int], tuple[float, list[Kline]]] = {}
        self._filters_cache: dict[str, tuple[float, SymbolFilter]] = {}
        #: Session date (YYYYMMDD) that marketwatch rows omit; anchored from the
        #: market overview and cached with the same TTL as the board itself.
        self._board_date: tuple[float, int] | None = None

    # --- wiring helpers ---------------------------------------------------
    def canonical_symbol(self, symbol: str) -> str:
        """insCode wins; a board ticker or ISIN is resolved through the universe file."""
        text = str(symbol or "").strip()
        if text.isdigit():
            return text
        row = self.index.resolve(text)
        if row is not None:
            return str(row["insCode"])
        raise DataUnavailable(f"unknown TSE instrument {symbol!r}; not in universe_tse.json")

    def display(self, code: str) -> dict[str, Any]:
        row = self.index.row_for(code) or {}
        return {"insCode": str(code), "ticker": row.get("ticker"), "name": row.get("name"), "isin": row.get("isin")}

    def _url(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.client.get(f"{self.base}{path}", params)

    # --- market watch (one request, whole board) --------------------------
    def watch(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            cached = self._watch
        if cached is not None and self.watch_ttl_seconds > 0 and now - cached[0] < self.watch_ttl_seconds:
            return cached[1]
        payload = self._url(MARKETWATCH_PATH, MARKETWATCH_PARAMS)
        rows = payload.get("marketwatch") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise DataUnavailable("TSETMC marketwatch returned no rows")
        by_code: dict[str, dict[str, Any]] = {}
        for row in rows:
            if isinstance(row, dict) and row.get("insCode"):
                by_code[str(row["insCode"])] = row
        with self._lock:
            self._watch = (now, by_code)
        return by_code

    def watch_row(self, symbol: str) -> dict[str, Any]:
        code = self.canonical_symbol(symbol)
        row = self.watch().get(code)
        if row is None:
            raise DataUnavailable(f"instrument {code} absent from the current TSETMC board")
        return row

    def session_date(self) -> int:
        """Trading date of the current board, in TSETMC's YYYYMMDD form.

        ``GetMarketWatch`` rows carry ``dEven = 0`` (verified on the recorded
        fixture and again live), so pairing ``hEven`` with the wall clock would
        fabricate a timestamp. The overview's ``marketActivityDEven`` is the
        exchange's own answer; if the overview is unavailable we fall back to the
        last completed session (older is safe, newer is not).
        """
        now = time.monotonic()
        with self._lock:
            cached = self._board_date
        if cached is not None and self.watch_ttl_seconds > 0 and now - cached[0] < self.watch_ttl_seconds:
            return cached[1]
        try:
            day = int(self.market_state(1).get("activity_date") or 0)
        except (DataError, TypeError, ValueError):
            day = 0
        if day <= 0:
            closed = calendar.last_session_close(datetime.now(timezone.utc))
            day = int(closed.astimezone(TEHRAN).strftime("%Y%m%d"))
        with self._lock:
            self._board_date = (now, day)
        return day

    # --- quotes -----------------------------------------------------------
    def quote(self, symbol: str) -> Quote:
        row = self.watch_row(symbol)
        code = str(row["insCode"])
        bid = float(row.get("pmd") or 0.0)
        ask = float(row.get("pmo") or 0.0)
        if bid <= 0 or ask <= 0:
            raise DataUnavailable(f"no two-sided quote for {code} ({row.get('lva')})")
        if ask < bid:
            raise DataUnavailable(f"inverted book for {code}: bid {bid} > ask {ask}")
        ts = _epoch_of(row.get("dEven") or self.session_date(), row.get("hEven"))
        if ts is None:
            raise DataUnavailable(f"no session timestamp for {code}; refusing to invent one")
        return Quote(
            symbol=code, bid=bid, ask=ask, ts=ts, source="tsetmc",
            # marketwatch carries prices only; sizes stay 0 and the paper broker
            # then fills at the touch instead of pretending to know depth.
            bid_size=0.0, ask_size=0.0,
        )

    def book(self, symbol: str) -> list[dict[str, Any]]:
        """Real order-book depth (BestLimits) — for the dashboard, not for fills."""
        code = self.canonical_symbol(symbol)
        payload = self._url(BEST_LIMITS_PATH.format(ins=code))
        rows = payload.get("bestLimits") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise DataUnavailable(f"no book for {code}")
        # A level with no orders publishes price 0. That is an absent price, not a
        # free one, so it leaves the venue as null and the UI renders NO_DATA.
        return [
            {
                "level": row.get("number"),
                "bid_price": float(row.get("pMeDem") or 0.0) or None,
                "bid_qty": float(row.get("qTitMeDem") or 0.0),
                "bid_orders": int(row.get("zOrdMeDem") or 0),
                "ask_price": float(row.get("pMeOf") or 0.0) or None,
                "ask_qty": float(row.get("qTitMeOf") or 0.0),
                "ask_orders": int(row.get("zOrdMeOf") or 0),
            }
            for row in rows
            if isinstance(row, dict)
        ]

    def closing_info(self, symbol: str) -> dict[str, Any]:
        code = self.canonical_symbol(symbol)
        payload = self._url(CLOSING_INFO_PATH.format(ins=code))
        info = payload.get("closingPriceInfo") if isinstance(payload, dict) else None
        if not isinstance(info, dict):
            raise DataUnavailable(f"no closing info for {code}")
        return info

    # --- candles ----------------------------------------------------------
    def daily_rows(self, symbol: str, limit: int) -> list[dict[str, Any]]:
        code = self.canonical_symbol(symbol)
        payload = self._url(DAILY_PATH.format(ins=code, top=max(1, min(int(limit), 1000))))
        rows = payload.get("closingPriceDaily") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise DataUnavailable(f"no daily history for {code}")
        return [row for row in rows if isinstance(row, dict)]

    def klines(self, symbol: str, timeframe: str = TIMEFRAME, limit: int = 300) -> list[Kline]:
        if timeframe not in (TIMEFRAME, WEEK_TIMEFRAME):
            raise DataUnavailable(
                f"TSE publishes daily history only; {timeframe!r} is not available "
                f"(supported: {TIMEFRAME}, {WEEK_TIMEFRAME} aggregated from daily bars)"
            )
        bars = self._daily_klines(symbol, limit)
        return weekly_from_daily(bars) if timeframe == WEEK_TIMEFRAME else bars

    def _daily_klines(self, symbol: str, limit: int) -> list[Kline]:
        code = self.canonical_symbol(symbol)
        cache_key = (code, int(limit))
        now = time.monotonic()
        with self._lock:
            cached = self._daily.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]

        today = datetime.now(TEHRAN).date()
        in_progress = calendar.phase(datetime.now(timezone.utc))["state"] == "open"
        bars: list[Kline] = []
        for row in self.daily_rows(symbol, limit):
            d_even = int(row.get("dEven") or 0)
            if d_even <= 0:
                continue
            open_ = float(row.get("priceFirst") or 0.0)
            high = float(row.get("priceMax") or 0.0)
            low = float(row.get("priceMin") or 0.0)
            close = float(row.get("pClosing") or 0.0)
            if close <= 0 or open_ <= 0:
                continue
            # A bar is closed once the exchange has finalised the day (yClose) and
            # the session clock has passed it; today's running session stays open.
            closed = bool(row.get("yClose")) and not (in_progress and d_even == int(today.strftime("%Y%m%d")))
            bars.append(
                Kline(
                    open_time=int(_session_open_epoch(d_even)),
                    open=open_, high=high, low=low, close=close,
                    base_volume=float(row.get("qTotTran5J") or 0.0),
                    quote_volume=float(row.get("qTotCap") or 0.0),
                    closed=closed,
                )
            )
        if not bars:
            raise DataUnavailable(f"no usable daily bars for {code}")
        bars.sort(key=lambda bar: bar.open_time)
        with self._lock:
            self._daily[cache_key] = (now + self.watch_ttl_seconds, bars)
        return bars

    def klines_range(self, symbol: str, timeframe: str, start_ts: int, end_ts: int, page: int = 800) -> list[Kline]:
        """History over a window — TSETMC answers depth by count, so request enough bars."""
        if timeframe not in (TIMEFRAME, WEEK_TIMEFRAME):
            raise DataUnavailable(f"TSE publishes daily history only; {timeframe!r} is not available")
        sessions = max(30, int((end_ts - start_ts) // BAR_SECONDS) + 40)
        bars = self.klines(symbol, timeframe, min(sessions, 1000))
        bars = [bar for bar in bars if start_ts <= bar.open_time <= end_ts]
        if not bars:
            raise DataUnavailable(f"no TSE bars for {symbol} in the requested window")
        return bars

    # --- venue rules ------------------------------------------------------
    def price_band(self, symbol: str) -> dict[str, Any]:
        row = self.watch_row(symbol)
        yesterday = float(row.get("py") or 0.0)
        band_low = float(row.get("pMin") or 0.0)
        band_high = float(row.get("pMax") or 0.0)
        last = float(row.get("pcl") or row.get("pdv") or 0.0)
        return {
            "insCode": str(row["insCode"]),
            "ticker": row.get("lva"),
            "price_yesterday": yesterday or None,
            "band_low": band_low or None,
            "band_high": band_high or None,
            "band_pct": round((band_high / yesterday - 1) * 100, 3) if band_high and yesterday else None,
            "last_price": last or None,
            "base_volume": float(row.get("bv") or 0.0) or None,
            "group": str(row.get("csv") or "").strip() or None,
            "band_source": "tsetmc:marketwatch.pMin/pMax",
        }

    def filters(self, symbol: str) -> SymbolFilter:
        code = self.canonical_symbol(symbol)
        now = time.monotonic()
        if self.filters_ttl_seconds > 0:
            with self._lock:
                cached = self._filters_cache.get(code)
            if cached and cached[0] > now:
                return cached[1]
        band = self.price_band(code)
        tradable = bool(band["band_low"]) and bool(band["band_high"]) and bool(band["price_yesterday"])
        result = SymbolFilter(
            symbol=code,
            price_precision=0,   # quoted in whole Rials
            qty_precision=0,     # whole shares
            min_qty=LOT_SIZE,    # one lot
            min_quote=0.0,       # no minimum order value published by TSETMC
            max_quote=0.0,       # no per-order cap published by TSETMC
            tradable=tradable,
            fee_pct=self.fee_pct,
        )
        if self.filters_ttl_seconds > 0:
            with self._lock:
                self._filters_cache[code] = (now + self.filters_ttl_seconds, result)
        return result

    # --- clock + market state --------------------------------------------
    def server_time(self) -> float:
        """TSETMC has no time endpoint; the CDN's own HTTP ``Date`` header is used."""
        epoch = self.client.date_header_epoch(f"{self.base}{MARKETOVERVIEW_PATH.format(flow=1)}")
        if epoch is None:
            raise DataUnavailable("TSETMC did not return a usable Date header")
        return epoch

    def market_state(self, flow: int = 1) -> dict[str, Any]:
        payload = self._url(MARKET_OVERVIEW_PATH.format(flow=int(flow)))
        overview = payload.get("marketOverview") if isinstance(payload, dict) else None
        if not isinstance(overview, dict):
            raise DataUnavailable(f"no market overview for flow {flow}")
        return {
            "flow": int(flow),
            "index_last_value": float(overview.get("indexLastValue") or 0.0) or None,
            # ``indexChange`` is in index points (verified against the live board:
            # 8,173,767.36 with -98,676.99 = -1.19%), not a percentage.
            "index_change": float(overview.get("indexChange") or 0.0) or None,
            "index_equal_weighted": float(overview.get("indexEqualWeightedLastValue") or 0.0) or None,
            "trade_count": float(overview.get("marketActivityZTotTran") or 0.0) or None,
            "volume_shares": float(overview.get("marketActivityQTotTran") or 0.0) or None,
            "value_rial": float(overview.get("marketActivityQTotCap") or 0.0) or None,
            "market_value": float(overview.get("marketValue") or 0.0) or None,
            "activity_date": overview.get("marketActivityDEven"),
            "activity_time": overview.get("marketActivityHEven"),
            "state_code": overview.get("marketState"),
            "state_title": overview.get("marketStateTitle"),
        }

    def search(self, query: str) -> list[dict[str, Any]]:
        path = SEARCH_PATH.format(query=urlquote(str(query).strip(), safe=""))
        payload = self._url(path, {"instrumentType[0]": 1, "instrumentType[1]": 2})
        rows = payload.get("instrumentSearch") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        return [
            {
                "insCode": str(row.get("insCode")),
                "ticker": row.get("lVal18AFC"),
                "name": row.get("lVal30"),
                "isin": row.get("cIsin"),
                "market_flow": row.get("flow"),
            }
            for row in rows
            if isinstance(row, dict) and row.get("insCode")
        ]


def load_index(root: Path) -> InstrumentIndex:
    return InstrumentIndex(Path(root) / "universe_tse.json").load()


# --- spec wiring ----------------------------------------------------------

def _provider(client: HttpClient, config: Any) -> TseProvider:
    options = dict(getattr(config, "market_options", None) or {})
    return TseProvider(
        client,
        index=load_index(config.root),
        fee_pct=float(getattr(config, "taker_fee_pct", 0.0) or 0.0),
        watch_ttl_seconds=float(options.get("watch_ttl_seconds", 20.0)),
        filters_ttl_seconds=int(getattr(config, "filters_cache_seconds", 3600) or 0),
    )


def _clock(client: HttpClient, config: Any) -> dict[str, Any]:
    """TSETMC has no time endpoint — the CDN's own HTTP Date header is the reference."""
    url = f"{BASE}{MARKET_OVERVIEW_PATH.format(flow=1)}"
    server = client.date_header_epoch(url)
    local = datetime.now(timezone.utc).timestamp()
    if server is None:
        raise DataError("TSETMC returned no parseable Date header; cannot validate the clock")
    skew = local - server
    return {
        "server_time": server,
        "local_time": local,
        "skew_seconds": round(skew, 3),
        "acceptable": abs(skew) <= float(config.max_clock_skew_seconds),
        "source": "tsetmc:http-date-header",
    }


def _allowlist(spec: Any, config: Any) -> tuple[str, ...]:
    """The tradable set the risk gate enforces for TSE.

    ``markets.tse.symbols`` (insCodes or board tickers) wins; otherwise the
    ranked universe snapshot, capped by ``markets.tse.max_symbols``. The engine
    never scans more than this list, so a missing/empty snapshot is a hard stop,
    not a silent open market.
    """
    options = dict(getattr(config, "market_options", None) or {})
    configured = options.get("symbols")
    index = load_index(config.root)
    if not index.rows and not configured:
        raise DataError(
            f"TSE allowlist is empty and {index.path} is missing/unreadable ({index.error}); "
            "run python tools/build_tse_universe.py"
        )
    if configured:
        resolved: list[str] = []
        for item in configured:
            text = str(item).strip()
            if text.isdigit():
                resolved.append(text)
                continue
            row = index.resolve(text)
            if row is None:
                raise DataError(f"TSE symbol {item!r} is not in universe_tse.json; refusing to guess its insCode")
            resolved.append(str(row["insCode"]))
        return tuple(dict.fromkeys(resolved))
    cap = max(1, int(options.get("max_symbols", 40) or 40))
    return tuple(str(row["insCode"]) for row in index.rows[:cap])


SPEC = register(
    MarketSpec(
        name="tse",
        title="بورس ایران — TSETMC",
        currency="ریال",
        quantity_unit="سهم",
        timeframes=(TIMEFRAME, WEEK_TIMEFRAME),
        provider=_provider,
        cross=no_cross,
        clock=_clock,
        freshness_reference=calendar.staleness_reference,
        symbols=_allowlist,
        http_headers={"User-Agent": USER_AGENT},
        # Weekly confirmation keeps the three-way split honest on a daily-only feed.
        confirm_timeframe=WEEK_TIMEFRAME,
        # 0.325% is the combined brokerage figure TSETMC/brokers publish for a
        # normal board trade; it is a default, not a claim — config.markets.tse.fees wins.
        default_maker_pct=0.325,
        default_taker_pct=0.325,
        quality_warnings=("single_source_tsetmc",),
        gap_is_expected=calendar.bar_gap_ok,
        notes=(
            "جلسه‌ی شنبه تا چهارشنبه ۰۹:۰۰–۱۲:۳۰ به وقت تهران؛ ساعت تازگی داده در پایان جلسه می‌ایستد",
            "داده فقط روزانه (1d)؛ تایم‌فریم پایین‌تر از TSETMC منتشر نمی‌شود",
            "منبع دوم مستقل وجود ندارد؛ هر snapshot هشدار single_source_tsetmc می‌گیرد",
            "دامنه‌ی نوسان روزانه از خود صرافی (pMin/pMax) خوانده می‌شود، نه از جدول حدسی",
            "هیچ مسیر سفارش زنده‌ای برای بورس پیاده نشده (API رسمی کارگزار در دسترس نیست)",
        ),
    )
)
