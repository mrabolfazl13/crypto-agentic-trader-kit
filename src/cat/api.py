"""Local read-only-first HTTP API for the PAPER research platform.

Every endpoint follows ``docs/API_CONTRACT_FA.md``:
  * no route can create a live order or flip ``mode`` to LIVE;
  * absent data is ``null`` / ``"no_data"``, never a fabricated number;
  * every metric-bearing response carries a ``provenance`` object.

The app is loopback-only, has no authentication (it is a local dev service) and
is deliberately PAPER-first. The sibling research/dataset modules are imported
lazily inside the handlers so this file can be exercised before they exist.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field

from .backtest import run_backtest
from .config import Config, ConfigError, config_digest, load_config
from .data import DataError, DataUnavailable, Kline
from .engine import Engine
from .http import HttpClient
from .markets import calendar as tse_calendar
from .mirror import mirror_cycle
from .portfolio import load_state
from .report import DISCLAIMER, audit_day
from .strategies import params_from_dict
from .store import SCHEMA_VERSION, now_iso, open_store

# ---------------------------------------------------------------------------
# Secrets redaction
# ---------------------------------------------------------------------------

# Keys whose *value* must never leave the process (dropped entirely).
_DROP_KEYS = (
    "api_key", "apikey", "api_secret", "secret", "token", "password",
    "passphrase", "private_key", "seed", "seed_phrase", "authorization",
    "access_key", "secret_key",
)
# Keys whose value is masked but whose presence is still reported.
_MASK_KEYS = ("proxy",)


def _key_matches(key: str, needles: tuple[str, ...]) -> bool:
    lowered = str(key).lower()
    return any(needle in lowered for needle in needles)


def redact(value: Any) -> Any:
    """Recursively strip credentials and mask proxy values.

    Dictionaries lose any secret-looking key; proxy values become ``"***"``.
    The result is safe to return in a response body or write to a log line.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _key_matches(key, _DROP_KEYS):
                continue
            if _key_matches(key, _MASK_KEYS):
                out[key] = "***" if item else None
                continue
            out[key] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _iso_to_epoch(ts: Any) -> int | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    try:
        return int(datetime.fromisoformat(str(ts)).timestamp())
    except (ValueError, TypeError):
        return None


def _provenance(kind: str, source: str, window: str = "no_data") -> dict[str, Any]:
    if kind not in ("history", "simulated", "paper", "live"):
        kind = "paper"
    return {"kind": kind, "source": source, "window": window or "no_data", "generated_at": now_iso()}


def _normalize_symbol(symbol: str) -> str:
    return str(symbol).upper().replace("/", "_").replace("-", "_")


#: Top URL segments that belong to the server, never to a client-side route.
_SHELL_PASSTHROUGH_SEGMENTS = {"api", "assets"}


def _is_client_route(url_path: str) -> bool:
    """Only a client-side route may fall back to the SPA shell.

    An unknown API path or a missing hashed asset must answer 404 in its own
    language: a shell here makes the browser see "200 OK" with HTML and the
    dashboard silently renders empty cards instead of reporting a stale server.
    """
    clean = str(url_path).strip("/")
    if not clean:
        return True
    first = clean.split("/")[0].lower()
    return first not in _SHELL_PASSTHROUGH_SEGMENTS and "." not in Path(clean).name


# Short-lived cache for /market/candles so a symbol switch does not hit the exchange
# again. Keyed by (symbol, timeframe, limit); TTL is intentionally small.
_CANDLES_TTL = 20.0
_candles_lock = threading.Lock()
_candles_cache: dict[tuple[str, str, str, str, int], tuple[float, list[Kline], str]] = {}
_providers: dict[str, Any] = {}


def _gate_provider(config: Config):
    """The cached data provider for this workspace — built from the market spec.

    ``gate`` is the historical name of the primary provider slot; for
    ``trading.market: tse`` it is a TSETMC provider with the venue's own headers.
    """
    key = f"{config.root}#{config.market}"
    with _candles_lock:
        provider = _providers.get(key)
        if provider is None:
            client = HttpClient(proxy=config.proxy, headers=dict(config.spec.http_headers))
            provider = config.spec.build_provider(client, config)
            _providers[key] = provider
    return provider


def _venue_view_config(primary: Config, market: str) -> Config:
    """The config behind a read-only second-venue view of the same dashboard.

    The engine, the risk gate and the paper cycle keep running on ``primary`` —
    this object is only ever read by the display routes of one venue. A non-crypto
    board therefore needs its own ``config.<market>.yaml`` (PAPER by construction:
    :func:`cat.config.load_config` refuses LIVE on any other venue) instead of
    forcing the user to run a second server on a second port.
    """
    if primary.spec.name == market:
        return primary
    path = Path(primary.root) / f"config.{market}.yaml"
    if not path.exists():
        raise HTTPException(
            status_code=409,
            detail=(
                f"نمایش «{market}» در این سرویس پیکربندی نشده است. فایلی به نشانی {path.name} در ریشه‌ی پروژه "
                f"نیاز است؛ نمونه‌ی آماده: کپی {f'config.{market}.example.yaml'} به {path.name} و یک بار "
                "راه‌اندازی دوباره‌ی سرویس. موتور و ریسک‌گیت همچنان روی بازار اصلی می‌مانند."
            ),
        )
    try:
        view = load_config(path, root=primary.root)
    except ConfigError as exc:
        raise HTTPException(status_code=409, detail=f"پیکربندی {path.name} خوانده نشد: {exc}") from exc
    if view.spec.name != market:
        raise HTTPException(
            status_code=409,
            detail=f"{path.name} بازارِ «{view.market}» را انتخاب کرده؛ باید trading.market روی {market} باشد.",
        )
    return view


#: Read-only venues the dashboard can show beside the primary market.
SECONDARY_VIEWS = ("tse",)


def _resolve_symbol_config(primary: Config, symbol: str) -> tuple[Config, str]:
    """Which venue owns this symbol — the primary market first, then read-only views.

    A crypto symbol never reaches the second branch, so its behaviour and error
    text are exactly what they were. A TSE insCode is served when
    ``config.tse.yaml`` exists and refused by the primary's 404 when it does not.
    """
    try:
        return primary, _venue_symbol(primary, symbol)
    except HTTPException as primary_error:
        for market in SECONDARY_VIEWS:
            try:
                view = _venue_view_config(primary, market)
            except HTTPException:
                continue
            try:
                return view, _venue_symbol(view, symbol)
            except HTTPException:
                continue
        raise primary_error


def _profiles_config(primary: Config, market: str) -> Config:
    """Profile runs are stored per market; the primary owns its own, a venue view owns the rest."""
    if market == primary.market:
        return primary
    if market not in SECONDARY_VIEWS:
        raise HTTPException(status_code=400, detail="بازار پشتیبانی‌نشده است؛ فقط بازار اصلی و دیدگاه‌های ثبت‌شده.")
    return _venue_view_config(primary, market)


def _venue_symbol(config: Config, symbol: str) -> str:
    """Identity the venue understands, or a 404. Unknown TSE tickers are refused,
    never guessed — inventing an instrument code is worse than a missing chart."""
    from .data import DataUnavailable, symbol_key

    provider = _gate_provider(config)
    try:
        key = symbol_key(provider, _normalize_symbol(symbol))
    except DataUnavailable as exc:
        raise HTTPException(status_code=404, detail=f"نماد شناخته نشد: {exc}") from exc
    if key not in config.symbols:
        raise HTTPException(status_code=404, detail="نماد خارج از فهرست مجاز است؛ نمودار فقط جهان معاملاتی را نشان می‌دهد.")
    return key


def _window_for(store, dataset_id: str | None) -> str:
    if not dataset_id:
        return "no_data"
    quality = store.dataset_quality(dataset_id)
    if not quality:
        return "no_data"
    return f"dataset:{dataset_id} bars:{quality.get('bars', 'no_data')}"


# ---------------------------------------------------------------------------
# Request models (strict validation)
# ---------------------------------------------------------------------------

class DatasetImport(BaseModel):
    path: str | None = None
    upload_name: str | None = None
    symbol: str
    timeframe: str | None = None
    origin: str = "csv"
    note: str = ""


class DatasetFetch(BaseModel):
    symbol: str
    timeframe: str
    days: int = Field(ge=1, le=1500)


class BacktestRequest(BaseModel):
    symbol: str
    timeframe: str | None = None
    days: int | None = Field(default=None, ge=1, le=1500)
    dataset_id: str | None = None
    params: dict[str, Any] | None = None
    strategy: str | None = None
    rsi_reset: float | None = Field(default=None, ge=1.0, le=99.0)
    warmup: int = Field(default=80, ge=1, le=2000)
    assumed_spread_pct: float = Field(default=0.05, ge=0.0, le=5.0)


class WalkForwardRequest(BaseModel):
    symbol: str
    timeframe: str | None = None
    days: int | None = Field(default=None, ge=1, le=1500)
    dataset_id: str | None = None
    grid: dict[str, list[Any]] = Field(default_factory=dict)
    folds: int = Field(default=4, ge=2, le=12)


class OptimizeRequest(BaseModel):
    symbol: str
    timeframe: str | None = None
    days: int | None = Field(default=None, ge=120, le=1500)
    dataset_id: str | None = None
    max_experiments: int = Field(default=3, ge=1, le=12)
    start_at: str | None = None
    folds: int = Field(default=3, ge=2, le=6)
    warmup: int = Field(default=80, ge=1, le=500)
    assumed_spread_pct: float = Field(default=0.05, ge=0.0, le=5.0)


class KillSwitchRequest(BaseModel):
    active: bool


class CycleRequest(BaseModel):
    dry_run: bool = False


def _grid_evaluations(grid: dict[str, list[Any]]) -> int:
    total = 1
    for values in grid.values():
        total *= max(1, len(values))
    return total


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

_CYCLE_LOCK = threading.Lock()


def create_app(root: Path | None = None) -> FastAPI:
    app_root = Path(root) if root else Path(__file__).resolve().parents[2]
    store = open_store(app_root)
    app = FastAPI(title="Agentic Crypto Trader — PAPER API", version="1", docs_url="/docs")

    def get_config() -> Config:
        try:
            return load_config(root=app_root)
        except ConfigError as exc:
            raise HTTPException(status_code=503, detail=f"پیکربندی قابل‌بارگذاری نیست — fail-closed: {exc}")

    router = APIRouter(prefix="/api/v1")

    # ---- read endpoints ---------------------------------------------------
    @router.get("/health")
    def health() -> dict[str, Any]:
        config = get_config()
        kill = Path(config.kill_switch_file).exists()
        # Newest ingested candle across datasets — the header chip's "last data".
        # Never fabricated: null when nothing has been imported yet.
        rows = store._rows("SELECT MAX(open_time) AS newest FROM bars")
        newest = rows[0]["newest"] if rows else None
        cycle = store._rows("SELECT MAX(ts) AS last FROM portfolio_snapshots")
        return {
            "status": "ok",
            "mode": config.mode,
            "schema_version": SCHEMA_VERSION,
            "counts": store.counts(),
            "kill_switch": kill,
            "live_trading_enabled": False,
            "owner_confirmed": config.owner_confirmed,
            "last_data_at": datetime.fromtimestamp(newest, tz=timezone.utc).isoformat(timespec="seconds") if newest else None,
            "last_cycle_at": cycle[0]["last"] if cycle else None,
            "market": config.spec.describe(),
            "server_time": now_iso(),
        }

    @router.get("/config")
    def config_view() -> dict[str, Any]:
        config = get_config()
        raw = redact(copy.deepcopy(config.raw))
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        if isinstance(data, dict):
            data["proxy_configured"] = bool(config.proxy)
        return {
            "mode": config.mode,
            "market": config.spec.name,
            "market_spec": config.spec.describe(),
            "market_options": dict(config.market_options),
            "symbols": list(config.symbols),
            "timeframe": config.timeframe,
            "confirm_timeframe": config.confirm_timeframe,
            "exchange": config.exchange,
            "allocated_capital": config.allocated_capital,
            "max_open_positions": config.max_open_positions,
            "maker_fee_pct": config.maker_fee_pct,
            "taker_fee_pct": config.taker_fee_pct,
            "limits": asdict(config.limits),
            "strategy_params": config.strategy_params.as_dict(),
            "warnings": list(config.warnings),
            "config_digest": config_digest(config.raw),
            "data_concurrency": config.data_concurrency,
            "config": raw,
        }

    @router.get("/universe")
    def universe() -> dict[str, Any]:
        """The dated universe snapshot next to the effective allowlist.

        ``config.trading.symbols`` is the authority the risk gate enforces;
        ``universe.json`` records where that list came from. If the two drift,
        the response says so instead of pretending they match.
        """
        config = get_config()
        effective = list(config.symbols)
        base: dict[str, Any] = {"available": False, "count": len(effective), "symbols": effective}
        path = app_root / "universe.json"
        if not path.exists():
            return {**base, "error": "universe.json not found; using trading.symbols only"}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {**base, "error": f"universe.json unreadable: {type(exc).__name__}"}
        if not isinstance(payload, dict):
            return {**base, "error": "universe.json is not an object"}
        manifest_symbols = payload.get("symbols") if isinstance(payload.get("symbols"), list) else []
        normalised = tuple(
            str(item).upper().replace("/", "_").replace("-", "_") for item in manifest_symbols
        )
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
        excluded = payload.get("excluded") if isinstance(payload.get("excluded"), list) else []
        return {
            "available": True,
            "source": source,
            "generated_at": payload.get("generated_at"),
            "requested_count": payload.get("requested_count"),
            "count": len(effective),
            "manifest_count": payload.get("count"),
            "digest": payload.get("digest"),
            "symbols": effective,
            "manifest_symbols": manifest_symbols,
            "matches_config": normalised == tuple(effective),
            "entries": entries,
            "excluded": excluded,
            "skipped": payload.get("skipped") if isinstance(payload.get("skipped"), list) else [],
        }

    @router.get("/market/candles")
    def market_candles(
        symbol: str,
        timeframe: str | None = None,
        limit: int = Query(default=180, ge=20, le=500),
    ) -> dict[str, Any]:
        """Closed candles for one allow-listed symbol, read from its own venue.

        This endpoint never invents data: a symbol outside ``trading.symbols`` is
        refused, and an unreachable feed is a 503. Candles are cached in-process
        for a few seconds so switching symbols does not hammer the exchange.
        """
        config, normalized = _resolve_symbol_config(get_config(), symbol)
        provider = _gate_provider(config)
        tf = timeframe or config.timeframe
        if tf not in config.spec.timeframes:
            raise HTTPException(
                status_code=400,
                detail=f"تایم‌فریم {tf} برای بازار {config.spec.title} منتشر نمی‌شود؛ مجاز: {', '.join(config.spec.timeframes)}",
            )

        # The venue is part of the key: one process now answers for the primary
        # market and any read-only view, and a candle is only ever valid for its own feed.
        key = (str(app_root), config.market, normalized, tf, limit)
        monotonic_now = time.monotonic()
        with _candles_lock:
            cached = _candles_cache.get(key)
            if cached is not None and monotonic_now - cached[0] < _CANDLES_TTL:
                bars, fetched_at = cached[1], cached[2]
                cache_hit = True
        if cached is None or monotonic_now - cached[0] >= _CANDLES_TTL:
            try:
                bars = provider.klines(normalized, tf, limit=limit)
            except DataError as exc:
                raise HTTPException(status_code=503, detail=f"دریافت کندل از صرافی ممکن نشد: {exc}")
            fetched_at = now_iso()
            with _candles_lock:
                _candles_cache[key] = (monotonic_now, bars, fetched_at)
            cache_hit = False

        closed = [bar for bar in bars if bar.closed]
        first = closed[0].open_time if closed else None
        last = closed[-1].open_time if closed else None
        window = (
            f"{datetime.fromtimestamp(first, tz=timezone.utc).isoformat(timespec='seconds')} → "
            f"{datetime.fromtimestamp(last, tz=timezone.utc).isoformat(timespec='seconds')}"
            if first is not None and last is not None else "no_data"
        )
        return {
            "symbol": normalized,
            "timeframe": tf,
            "bars": [
                {
                    "t": bar.open_time, "open": bar.open, "high": bar.high, "low": bar.low,
                    "close": bar.close, "base_volume": bar.base_volume,
                    "quote_volume": bar.quote_volume, "closed": bar.closed,
                }
                for bar in bars
            ],
            "count": len(bars),
            "closed_count": len(closed),
            "provenance": {
                **_provenance("history", f"{provider.name}:{normalized}:{tf}", window),
                "fetched_at": fetched_at,
                "cached": cache_hit,
            },
        }

    # ---- بورس ایران: فقط خواندنی، بدون هیچ مسیر سفارش -------------------
    # The venue config comes from _venue_view_config(): a crypto-primary service
    # still serves this board from config.tse.yaml, and never lets it touch the engine.
    @router.get("/tse/market")
    def tse_market(flow: int = Query(default=1, ge=1, le=2)) -> dict[str, Any]:
        """Session state and the board summary, straight from TSETMC.

        ``session`` is computed from the recorded Tehran calendar, ``board`` is
        the exchange's own overview payload. Nothing here is estimated.
        """
        config = _venue_view_config(get_config(), "tse")
        provider = _gate_provider(config)
        session = tse_calendar.phase(datetime.now(timezone.utc))
        freshness_ref = config.spec.freshness_reference(datetime.now(timezone.utc).timestamp())
        try:
            board = provider.market_state(flow)
            rows = provider.watch()
        except DataError as exc:
            raise HTTPException(status_code=503, detail=f"تابلوی بورس در دسترس نیست: {exc}")
        return {
            "session": session,
            "freshness_reference": datetime.fromtimestamp(freshness_ref, tz=timezone.utc).isoformat(timespec="seconds"),
            "board": board,
            "board_size": len(rows),
            "allowlist": list(config.symbols),
            "currency": config.spec.currency,
            "quantity_unit": config.spec.quantity_unit,
            "provenance": {
                **_provenance("history", f"tsetmc:marketwatch+overview:{flow}", str(board.get("activity_date"))),
                "single_source": True,
            },
        }

    @router.get("/tse/instruments")
    def tse_instruments(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
        """Ranked instruments with their live board prices (last session values)."""
        config = _venue_view_config(get_config(), "tse")
        provider = _gate_provider(config)
        try:
            rows = provider.watch()
        except DataError as exc:
            raise HTTPException(status_code=503, detail=f"تابلوی بورس در دسترس نیست: {exc}")
        allowed = set(config.symbols)
        out = []
        for row in rows.values():
            code = str(row.get("insCode"))
            if allowed and code not in allowed:
                continue
            last = float(row.get("pcl") or row.get("pdv") or 0.0)
            yesterday = float(row.get("py") or 0.0)
            out.append({
                "ins_code": code,
                "ticker": str(row.get("lva") or "").strip() or None,
                "name": str(row.get("lvc") or "").strip() or None,
                "isin": str(row.get("insID") or "").strip() or None,
                "group": str(row.get("csv") or "").strip() or None,
                "last_price": last or None,
                "price_yesterday": yesterday or None,
                "change_pct": round((last / yesterday - 1) * 100, 3) if last and yesterday else None,
                "best_bid": float(row.get("pmd") or 0.0) or None,
                "best_ask": float(row.get("pmo") or 0.0) or None,
                "volume_shares": float(row.get("qtj") or 0.0) or None,
                "trade_value_rial": float(row.get("qtc") or 0.0) or None,
                "trade_count": float(row.get("ztt") or 0.0) or None,
                "base_volume": float(row.get("bv") or 0.0) or None,
                "band_low": float(row.get("pMin") or 0.0) or None,
                "band_high": float(row.get("pMax") or 0.0) or None,
                "session_date": row.get("dEven"),
                "session_time": row.get("hEven"),
            })
        out.sort(key=lambda item: item["trade_value_rial"] or 0.0, reverse=True)
        return {
            "count": len(out),
            "returned": min(limit, len(out)),
            "instruments": out[:limit],
            "universe": provider.index.summary(),
            "provenance": {**_provenance("history", "tsetmc:marketwatch", f"top:{limit}"), "single_source": True},
        }

    @router.get("/tse/instrument")
    def tse_instrument(symbol: str) -> dict[str, Any]:
        config = _venue_view_config(get_config(), "tse")
        provider = _gate_provider(config)
        code = _venue_symbol(config, symbol)
        try:
            band = provider.price_band(code)
        except DataError as exc:
            raise HTTPException(status_code=503, detail=f"داده‌ی این ابزار خوانده نشد: {exc}")
        # A one-sided board (no pmd/pmo) is absent data, not a service failure: the
        # band, venue rules and candles are still real, so the card renders with
        # quote=null plus the reason instead of throwing the whole view away.
        quote: Any = None
        quote_note: str | None = None
        try:
            quote = provider.quote(code)
        except DataUnavailable as exc:
            quote_note = str(exc)
        except DataError as exc:
            raise HTTPException(status_code=503, detail=f"قیمت این ابزار خوانده نشد: {exc}")
        return {
            "display": provider.display(code),
            "band": band,
            "quote": None if quote is None else {
                "symbol": quote.symbol, "bid": quote.bid, "ask": quote.ask,
                "mid": round(quote.mid, 4), "spread_pct": round(quote.spread_pct, 4),
                "ts": datetime.fromtimestamp(quote.ts, tz=timezone.utc).isoformat(timespec="seconds"),
                "source": quote.source,
            },
            "quote_note": quote_note,
            "venue_rules": {
                "lot_size": provider.filters(code).min_qty,
                "price_currency": config.spec.currency,
                "quantity_unit": config.spec.quantity_unit,
            },
            "provenance": {**_provenance("history", f"tsetmc:{code}", str(band.get("band_source"))), "single_source": True},
        }

    @router.get("/tse/book")
    def tse_book(symbol: str) -> dict[str, Any]:
        """Real order-book depth (BestLimits) — display only; the paper broker still fills at the touch."""
        config = _venue_view_config(get_config(), "tse")
        provider = _gate_provider(config)
        code = _venue_symbol(config, symbol)
        try:
            levels = provider.book(code)
        except DataUnavailable:
            levels = []
        except DataError as exc:
            raise HTTPException(status_code=503, detail=f"عمق بازار خوانده نشد: {exc}")
        note = None
        if not levels:
            note = "تابلو برای این نماد عمق بازار منتشر نکرده است (متوقف یا خارج از جلسه)"
        elif all(level["ask_price"] is None for level in levels):
            note = "در پنج سطح نخست هیچ سفارش فروشی ثبت نشده — صف خرید قفل است"
        elif all(level["bid_price"] is None for level in levels):
            note = "در پنج سطح نخست هیچ سفارش خریدی ثبت نشده — صف فروش قفل است"
        return {
            "symbol": code,
            "levels": levels,
            "count": len(levels),
            "note": note,
            "provenance": {**_provenance("history", f"tsetmc:{code}:bestlimits", f"levels:{len(levels)}"), "single_source": True},
        }

    @router.get("/profiles")
    def profiles(market: str = Query(default="crypto")) -> dict[str, Any]:
        """Stored staged experiments: one record per development round and market.

        Read-only. An empty list is the honest answer before the first run; it is
        never replaced with a sample result.
        """
        import importlib

        config = _profiles_config(get_config(), market)
        try:
            profiles_mod = importlib.import_module("cat.profiles")
        except ModuleNotFoundError:
            raise HTTPException(status_code=503, detail="ماژول پروفایل‌ها در دسترس نیست")
        runs = profiles_mod.list_runs(config)
        return {
            "market": market,
            "runs": runs,
            "count": len(runs),
            "note": None if runs else "هنوز هیچ اجرای پروفایلی ذخیره نشده است؛ اجرای python trader.py profiles آن را می‌سازد.",
            "provenance": _provenance("simulated", "profiles:backtest", "recent"),
        }

    @router.get("/profiles/{run_id}")
    def profile_run(run_id: str, market: str = Query(default="crypto")) -> dict[str, Any]:
        import importlib

        config = _profiles_config(get_config(), market)
        try:
            profiles_mod = importlib.import_module("cat.profiles")
        except ModuleNotFoundError:
            raise HTTPException(status_code=503, detail="ماژول پروفایل‌ها در دسترس نیست")
        payload = profiles_mod.load_run(config, run_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="این اجرای پروفایل پیدا نشد.")
        meta = payload.get("meta") or {}
        return {
            **payload,
            "provenance": _provenance("simulated", str(meta.get("source") or "profiles:backtest"),
                                      str(meta.get("coverage_end_iso") or "no_data")),
        }

    @router.get("/portfolio")
    def portfolio() -> dict[str, Any]:
        config = get_config()
        state_file = app_root / "state" / "account.json"
        acct = load_state(state_file, config.allocated_capital)
        has_positions = bool(acct.positions)
        positions = []
        cost_basis_value = 0.0
        for position in acct.positions.values():
            cost_basis_value += position.qty * position.avg_entry
            positions.append({
                "symbol": position.symbol, "qty": position.qty, "avg_entry": position.avg_entry,
                "stop": position.stop, "target": position.target, "opened_at": position.opened_at,
                "decision_id": position.decision_id, "mark": None, "unrealized": None,
            })
        equity = acct.cash + cost_basis_value
        # No live quote map is required here; without prices unrealized stays null
        # rather than being dressed up as a real mark.
        return {
            "mode": config.mode,
            "allocated_capital": round(config.allocated_capital, 6) if config.allocated_capital is not None else None,
            "allocated_source": "config.risk.allocated_capital",
            "equity": round(equity, 6),
            "equity_basis": "cost (no live quotes)" if has_positions else "cash",
            "cash": round(acct.cash, 6),
            "unrealized": None,
            "realized": round(acct.realized_pnl, 6),
            "day_pnl": round(acct.day_realized_pnl, 6),
            "week_pnl": round(acct.week_realized_pnl, 6),
            "exposure_pct": round(acct.exposure_pct({}), 4) if has_positions else 0.0,
            "drawdown_pct": round(acct.drawdown_pct({}), 4),
            "trades_today": acct.trades_today,
            "consecutive_errors": acct.consecutive_errors,
            "fees_paid": round(acct.fees_paid, 6),
            "closed_trades": len(acct.closed_trades),
            "positions": positions,
            "price_source": "none",
            "updated_at": now_iso(),
            "provenance": _provenance("paper", "engine", "account.json"),
        }

    @router.get("/performance")
    def performance(symbol: str | None = None, window: str | None = None, mode: str = "PAPER") -> dict[str, Any]:
        if symbol:
            rows = store.list_trades(mode=mode, symbol=symbol, limit=10000)
            metrics = _ledger_metrics(rows, mode)
        else:
            metrics = store.trade_ledger_stats(mode=mode)
        return {"metrics": metrics, "window": window or "all",
                "provenance": _provenance("paper", "engine", window or "all")}

    @router.get("/equity-curve")
    def equity_curve(mode: str = "PAPER") -> dict[str, Any]:
        points = store.equity_curve(mode=mode)
        return {"mode": mode, "points": points, "count": len(points),
                "provenance": _provenance("paper", "engine")}

    @router.get("/orders")
    def orders(status: str | None = None) -> dict[str, Any]:
        broker_file = app_root / "state" / "paper_broker.json"
        rows: list[dict[str, Any]] = []
        if broker_file.exists():
            import json

            try:
                payload = json.loads(broker_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
            for order in (payload.get("orders") or {}).values():
                if status and status != "all" and str(order.get("status", "")).lower() != status.lower():
                    continue
                rows.append(order)
        return {"mode": "PAPER", "orders": rows, "count": len(rows),
                "provenance": _provenance("paper", "engine", "paper_broker.json")}

    @router.get("/trades")
    def trades(mode: str = "PAPER", symbol: str | None = None, outcome: str | None = None,
               limit: int = Query(default=200, ge=1, le=5000), offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
        if outcome and outcome not in ("win", "loss"):
            raise HTTPException(status_code=422, detail="outcome فقط می‌تواند win یا loss باشد.")
        rows = store.list_trades(mode=mode, symbol=symbol, outcome=outcome, limit=limit, offset=offset)
        return {"mode": mode, "trades": rows, "count": len(rows),
                "provenance": _provenance("paper", "engine")}

    @router.get("/decisions")
    def decisions(date: str | None = None, symbol: str | None = None,
                  limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
        rows = store.list_decisions(date=date, symbol=symbol, limit=limit)
        return {"decisions": rows, "count": len(rows),
                "provenance": _provenance("paper", "engine", date or "recent")}

    @router.get("/datasets")
    def datasets() -> dict[str, Any]:
        rows = store.list_datasets()
        return {"datasets": rows, "count": len(rows),
                "provenance": _provenance("history", "csv|mexc|gate")}

    @router.get("/datasets/{dataset_id}/quality")
    def dataset_quality(dataset_id: str) -> dict[str, Any]:
        quality = store.dataset_quality(dataset_id)
        if quality is None:
            raise HTTPException(status_code=404, detail="مجموعه‌داده یافت نشد.")
        return {"quality": quality, "provenance": _provenance("history", quality.get("source", "csv"),
                                                             f"dataset:{dataset_id}")}

    @router.get("/datasets/{dataset_id}/bars")
    def dataset_bars(dataset_id: str, limit: int = Query(default=500, ge=1, le=5000),
                    overlays: bool = False) -> dict[str, Any]:
        """Candles for one dataset, oldest→newest, with EMA overlays on request.

        Absent dataset is a 404, never an empty list dressed up as "no trades".
        """
        quality = store.dataset_quality(dataset_id)
        if quality is None:
            raise HTTPException(status_code=404, detail="مجموعه‌داده یافت نشد.")
        import importlib

        try:
            datasets_mod = importlib.import_module("cat.datasets")
        except ModuleNotFoundError:
            raise HTTPException(status_code=503, detail="ماژول داده در دسترس نیست — سیستم fail-open نیست")
        try:
            bars = datasets_mod.load_klines(store, dataset_id)
        except (DataUnavailable, DataError, datasets_mod.DatasetError):
            raise HTTPException(status_code=503, detail="کندل‌ها خوانده نشدند — سیستم fail-closed است")
        tail = bars[-limit:]
        payload = {
            "dataset_id": dataset_id,
            "symbol": quality.get("symbol"),
            "timeframe": quality.get("timeframe"),
            "total_bars": len(bars),
            "returned_bars": len(tail),
            "bars": [{"t": bar.open_time, "open": bar.open, "high": bar.high, "low": bar.low,
                      "close": bar.close, "base_volume": bar.base_volume,
                      "quote_volume": bar.quote_volume, "closed": bar.closed} for bar in tail],
            "provenance": _provenance("history", quality.get("source", "csv"), f"dataset:{dataset_id}"),
        }
        if overlays:
            from .indicators import closes, ema

            # Compute on the whole dataset, then take the tail: an EMA seeded from
            # the visible window alone would be a different (wrong) line.
            full = closes(bars)
            payload["overlays"] = []
            for label, period in (("ema_fast(20)", 20), ("ema_slow(50)", 50)):
                series = ema(full, period)
                visible = series[-limit:] if series else []
                payload["overlays"].append({"label": label, "points": [None] * (len(tail) - len(visible)) + list(visible)})
        return payload

    @router.get("/analysis")
    def analysis(symbol: str | None = None, timeframe: str | None = None, dataset_id: str | None = None) -> dict[str, Any]:
        config = get_config()
        import importlib

        try:
            datasets_mod = importlib.import_module("cat.datasets")
        except ModuleNotFoundError:
            raise HTTPException(status_code=503, detail="ماژول داده در دسترس نیست — سیستم fail-closed است")
        pair = _normalize_symbol(symbol or (config.symbols[0] if config.symbols else ""))
        tf = timeframe or config.timeframe
        try:
            if dataset_id:
                bars = datasets_mod.load_klines(store, dataset_id)
            else:
                gate = _make_gate(config)
                days = 60
                datasets_mod.ingest_exchange(store, gate, pair, tf, days)
                fresh = [d for d in store.list_datasets() if d["symbol"] == pair and d["timeframe"] == tf]
                if not fresh:
                    raise DataUnavailable("no dataset")
                bars = datasets_mod.load_klines(store, fresh[0]["id"])
            if not bars:
                return {"symbol": pair, "timeframe": tf, "analysis": "no_data",
                        "provenance": _provenance("history", "csv")}
            snapshot = _snapshot_from_bars(pair, tf, bars, config)
            from .strategy import analyze
            result = analyze(snapshot, config.strategy_params)
            return {"symbol": pair, "timeframe": tf,
                    "analysis": {"regime": result.regime, "summary": result.summary,
                                 "bullish": result.bullish, "bearish": result.bearish,
                                 "indicators": result.indicators, "scenarios": result.scenarios,
                                 "volume_state": result.volume_state, "invalidation": result.invalidation},
                    "provenance": _provenance("history", "csv")}
        except DataUnavailable:
            raise HTTPException(status_code=503, detail="داده در دسترس نیست — سیستم fail-closed است")

    @router.get("/runs")
    def runs(kind: str | None = None, symbol: str | None = None) -> dict[str, Any]:
        rows = store.list_runs(kind=kind, symbol=symbol)
        return {"runs": rows, "count": len(rows),
                "provenance": _provenance("simulated", "engine")}

    @router.get("/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="اجرا یافت نشد.")
        return {"run": run, "provenance": _provenance("simulated", "engine",
                                                     f"{run.get('window_start')} → {run.get('window_end')}")}

    @router.get("/strategies")
    def strategies() -> dict[str, Any]:
        rows = store.list_strategy_versions()
        return {"strategies": rows, "count": len(rows),
                "provenance": _provenance("simulated", "engine")}

    @router.get("/risk")
    def risk() -> dict[str, Any]:
        config = get_config()
        acct = load_state(app_root / "state" / "account.json", config.allocated_capital)
        limits = config.limits
        allocated = acct.allocated_capital
        daily_limit = allocated * limits.max_daily_loss_pct / 100.0
        weekly_limit = allocated * limits.max_weekly_loss_pct / 100.0
        drawdown = acct.drawdown_pct({})
        return {
            "mode": config.mode,
            "limits": {
                "max_account_allocation_pct": limits.max_account_allocation_pct,
                "max_position_pct_of_allocated_capital": limits.max_position_pct_of_allocated_capital,
                "max_risk_per_trade_pct": limits.max_risk_per_trade_pct,
                "max_daily_loss_pct": limits.max_daily_loss_pct,
                "max_weekly_loss_pct": limits.max_weekly_loss_pct,
                "max_drawdown_pct": limits.max_drawdown_pct,
                "max_trades_per_day": limits.max_trades_per_day,
                "stop_on_consecutive_errors": limits.stop_on_consecutive_errors,
            },
            "usage": {
                "allocated_capital": allocated,
                "day_realized_pnl": round(acct.day_realized_pnl, 6),
                "daily_loss_limit": round(daily_limit, 6),
                "week_realized_pnl": round(acct.week_realized_pnl, 6),
                "weekly_loss_limit": round(weekly_limit, 6),
                "trades_today": acct.trades_today,
                "max_trades_per_day": limits.max_trades_per_day,
                "open_positions": len(acct.positions),
                "max_open_positions": config.max_open_positions,
                "drawdown_pct": round(drawdown, 4),
                "max_drawdown_pct": limits.max_drawdown_pct,
                "consecutive_errors": acct.consecutive_errors,
            },
            "kill_switch": Path(config.kill_switch_file).exists(),
            "provenance": _provenance("paper", "engine", acct.day),
        }

    @router.get("/logs")
    def logs(level: str | None = None) -> dict[str, Any]:
        rows = store.list_logs(level=level)
        return {"logs": rows, "count": len(rows)}

    @router.get("/reports/{day}")
    def report(day: str) -> dict[str, Any]:
        config = get_config()
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="قالب تاریخ باید YYYY-MM-DD باشد.")
        stats = audit_day(app_root / "state" / "audit" / "events.jsonl", day)
        decisions = store.list_decisions(date=day)
        acct = load_state(app_root / "state" / "account.json", config.allocated_capital)
        lines = [
            f"# گزارش روزانه — {day}",
            "",
            f"- حالت: {config.mode}",
            f"- وضعیت kill switch: {'فعال — توقف امن' if Path(config.kill_switch_file).exists() else 'خاموش (در دسترس)'}",
            f"- تصمیم‌های ثبت‌شده: {len(decisions)} (تأیید Risk Gate: {stats['approved']})",
            f"- سفارش / پرشدن / لغو: {stats['orders']} / {stats['fills']} / {stats['cancels']}",
            f"- سود/زیان تحقق‌یافته امروز: {acct.day_realized_pnl:+.4f} USDT",
            f"- خطاهای پیاپی: {acct.consecutive_errors} از {config.limits.stop_on_consecutive_errors}",
            "",
            "## تصمیم‌های ردشده و علت",
        ]
        if stats["rejects"]:
            lines += [f"- {reason}: {count}" for reason, count in stats["rejects"].most_common(10)]
        else:
            lines.append("- در این روز هیچ تصمیم ردشده‌ای ثبت نشده است.")
        lines += ["", DISCLAIMER, ""]
        return {"day": day, "markdown": "\n".join(lines),
                "provenance": _provenance("paper", "engine", day)}

    # ---- write endpoints (research / paper only) --------------------------
    @router.post("/datasets/import")
    def dataset_import(body: DatasetImport) -> dict[str, Any]:
        import importlib

        try:
            datasets_mod = importlib.import_module("cat.datasets")
        except ModuleNotFoundError:
            raise HTTPException(status_code=503, detail="ماژول داده در دسترس نیست — سیستم fail-closed است")
        source_path = body.path or body.upload_name
        if not source_path:
            raise HTTPException(status_code=422, detail="یا path یا upload_name لازم است.")
        path = Path(source_path)
        if not path.is_absolute():
            path = app_root / path
        try:
            result = datasets_mod.ingest_csv(store, path, symbol=body.symbol,
                                             timeframe=body.timeframe, note=body.note)
        except DataUnavailable:
            raise HTTPException(status_code=503, detail="داده در دسترس نیست — سیستم fail-closed است")
        except (ValueError, datasets_mod.DatasetError) as exc:
            raise HTTPException(status_code=422, detail=f"اعتبارسنجی داده ناموفق: {exc}")
        return {**result, "provenance": _provenance("history", "csv", _window_for(store, result.get("dataset_id")))}

    @router.post("/datasets/fetch")
    def dataset_fetch(body: DatasetFetch) -> dict[str, Any]:
        config = get_config()
        import importlib

        try:
            datasets_mod = importlib.import_module("cat.datasets")
        except ModuleNotFoundError:
            raise HTTPException(status_code=503, detail="ماژول داده در دسترس نیست — سیستم fail-closed است")
        gate = _make_gate(config)
        try:
            result = datasets_mod.ingest_exchange(store, gate, _normalize_symbol(body.symbol),
                                                  body.timeframe, body.days)
        except (DataUnavailable, DataError):
            store.log("error", "api", "dataset_fetch data unavailable")
            raise HTTPException(status_code=503, detail="داده در دسترس نیست — سیستم fail-closed است")
        except (ValueError, datasets_mod.DatasetError) as exc:
            raise HTTPException(status_code=422, detail=f"دریافت داده ناموفق: {exc}")
        return {**result, "provenance": _provenance("history", "gate", _window_for(store, result.get("dataset_id")))}

    @router.post("/runs/backtest")
    def runs_backtest(body: BacktestRequest) -> dict[str, Any]:
        config = get_config()
        _guard_paper(config)
        symbol = _normalize_symbol(body.symbol)
        symbol = _require_symbol(config, symbol)
        tf = body.timeframe or config.timeframe
        if not body.dataset_id and not body.days:
            raise HTTPException(status_code=422, detail="یا dataset_id یا days باید داده شود.")
        import importlib

        try:
            datasets_mod = importlib.import_module("cat.datasets")
        except ModuleNotFoundError:
            raise HTTPException(status_code=503, detail="ماژول داده در دسترس نیست — سیستم fail-closed است")

        # A named family comes from the same lab the staged profiles measure, so the
        # number a user sees here is produced by the audited entry rules, not a copy.
        # Validated before any data is fetched so an unknown name never reaches the engine.
        strategy = (body.strategy or "").strip()
        propose_fn = None
        label_prefix = ""
        if strategy:
            from .strategy_lab import LAB_STRATEGIES

            if strategy not in LAB_STRATEGIES:
                raise HTTPException(
                    status_code=422,
                    detail=f"استراتژی ناشناخته: {strategy}. مجاز: {', '.join(sorted(LAB_STRATEGIES))}",
                )
            if body.rsi_reset is not None and strategy != "pullback_rsi":
                raise HTTPException(status_code=422, detail="rsi_reset فقط برای استراتژی pullback_rsi معنا دارد.")
            propose_fn = LAB_STRATEGIES[strategy]
            if body.rsi_reset is not None:
                from functools import partial

                propose_fn = partial(propose_fn, rsi_reset=body.rsi_reset)
            label_prefix = f"{strategy}|"
        try:
            bars, higher, filters, dataset_id = _series_for(
                store, config, datasets_mod, symbol, tf, body.days, body.dataset_id)
            # 60 is the threshold the engine itself uses (backtest.py: higher is
            # passed to the confirm series only when len >= 60); a stricter check
            # here rejected valid windows whose resampled confirm series had 60-79 bars.
            if len(bars) < 120 or len(higher) < 60:
                raise HTTPException(status_code=422,
                                    detail=f"داده کافی نیست: {len(bars)} کندل سیگنال، {len(higher)} کندل تأیید.")
            params = params_from_dict(body.params, base=config.strategy_params)
            result = run_backtest(config, symbol, bars=bars, higher_bars=higher, filters=filters,
                                  timeframe=tf, warmup=body.warmup, assumed_spread_pct=body.assumed_spread_pct,
                                  params=params, propose_fn=propose_fn)
            run_id = store.record_run(
                kind="backtest", symbol=symbol, timeframe=tf, params_label=label_prefix + params.label(),
                params=params.tuning(), dataset_id=dataset_id,
                window=(result.start, result.end), metrics=result.metrics,
                trades=result.trades, rejections=result.rejected, equity_curve=result.equity_curve,
            )
        except HTTPException:
            raise
        except (DataUnavailable, DataError):
            store.log("error", "api", "backtest data unavailable")
            raise HTTPException(status_code=503, detail="داده در دسترس نیست — سیستم fail-closed است")
        except (ValueError, getattr(datasets_mod, "DatasetError", ValueError)) as exc:
            raise HTTPException(status_code=422, detail=f"بک‌تست ناموفق: {exc}")
        return {"run_id": run_id, "symbol": symbol, "timeframe": tf, "strategy": strategy or "trend_v1",
                "metrics": result.metrics,
                "params": result.params, "provenance": _provenance("simulated", "engine", f"{result.start} → {result.end}")}

    @router.post("/runs/walk-forward")
    def runs_walk_forward(body: WalkForwardRequest) -> dict[str, Any]:
        config = get_config()
        _guard_paper(config)
        symbol = _normalize_symbol(body.symbol)
        symbol = _require_symbol(config, symbol)
        tf = body.timeframe or config.timeframe
        evaluations = _grid_evaluations(body.grid)
        if evaluations > 60:
            raise HTTPException(status_code=422,
                                detail=f"شبکه بیش از حد بزرگ است: {evaluations} ارزیابی (سقف ۶۰).")
        import importlib

        try:
            datasets_mod = importlib.import_module("cat.datasets")
            research_mod = importlib.import_module("cat.research")
        except ModuleNotFoundError:
            raise HTTPException(status_code=503, detail="ماژول تحقیق در دسترس نیست — سیستم fail-closed است")
        try:
            bars, higher, filters, used_dataset = _series_for(store, config, datasets_mod, symbol, tf,
                                                              body.days, body.dataset_id)
            if not body.grid:
                raise HTTPException(status_code=422, detail="grid نمی‌تواند خالی باشد.")
            result = research_mod.run_research(store, config, symbol=symbol, timeframe=tf,
                                               bars=bars, higher_bars=higher, filters=filters,
                                               grid=body.grid, folds=body.folds, dataset_id=used_dataset)
        except HTTPException:
            raise
        except (DataUnavailable, DataError):
            store.log("error", "api", "walk-forward data unavailable")
            raise HTTPException(status_code=503, detail="داده در دسترس نیست — سیستم fail-closed است")
        except (ValueError, getattr(datasets_mod, "DatasetError", ValueError)) as exc:
            raise HTTPException(status_code=422, detail=f"walk-forward ناموفق: {exc}")
        prov = result.pop("provenance", None) if isinstance(result, dict) else None
        window = f"{prov['window']}" if isinstance(prov, dict) else f"days:{body.days}"
        return {**(result if isinstance(result, dict) else {"result": result}),
                "provenance": _provenance("simulated", "engine", window)}

    @router.get("/optimizer")
    def optimizer_list(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        from . import optimizer

        return {"runs": optimizer.list_states(app_root, limit=limit),
                "hypotheses": [{"id": item["id"], "question": item["question"],
                                "grid": item["grid"]} for item in optimizer.PLAN],
                "disclaimer": optimizer.DISCLAIMER_FA}

    @router.get("/optimizer/{run_id}")
    def optimizer_detail(run_id: str) -> dict[str, Any]:
        from . import optimizer

        try:
            return optimizer.load_state(app_root, run_id)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="حلقه‌ی بهینه‌سازی پیدا نشد.")

    @router.post("/optimizer/start")
    def optimizer_start(body: OptimizeRequest) -> dict[str, Any]:
        config = get_config()
        _guard_paper(config)
        symbol = _require_symbol(config, _normalize_symbol(body.symbol))
        tf = body.timeframe or config.timeframe
        if not body.dataset_id and not body.days:
            raise HTTPException(status_code=422, detail="یا dataset_id یا days باید داده شود.")
        import importlib

        try:
            datasets_mod = importlib.import_module("cat.datasets")
            optimizer_mod = importlib.import_module("cat.optimizer")
        except ModuleNotFoundError:
            raise HTTPException(status_code=503, detail="ماژول بهینه‌سازی در دسترس نیست — fail-closed")
        try:
            _bars, _higher, _filters, dataset_id = _series_for(
                store, config, datasets_mod, symbol, tf, body.days, body.dataset_id)
            state = optimizer_mod.start(
                config, store, symbol=symbol, timeframe=tf, dataset_id=dataset_id,
                max_experiments=body.max_experiments, folds=body.folds,
                warmup=body.warmup, spread_pct=body.assumed_spread_pct,
                start_at=body.start_at,
            )
        except HTTPException:
            raise
        except (DataUnavailable, DataError):
            store.log("error", "api", "optimizer data unavailable")
            raise HTTPException(status_code=503, detail="داده در دسترس نیست — سیستم fail-closed است")
        except (ValueError, datasets_mod.DatasetError) as exc:
            raise HTTPException(status_code=422, detail=f"شروع بهینه‌سازی ناموفق: {exc}")
        store.log("info", "api", f"optimizer started {state['run_id']} on {symbol} {tf}",
                  {"dataset_id": dataset_id, "max_experiments": body.max_experiments})
        return state

    @router.post("/optimizer/{run_id}/stop")
    def optimizer_stop(run_id: str) -> dict[str, Any]:
        config = get_config()
        _guard_paper(config)
        from . import optimizer

        try:
            state = optimizer.stop(config, run_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="حلقه‌ی بهینه‌سازی پیدا نشد.")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        store.log("info", "api", f"optimizer stop requested for {run_id}")
        return state

    @router.post("/optimizer/{run_id}/resume")
    def optimizer_resume(run_id: str) -> dict[str, Any]:
        config = get_config()
        _guard_paper(config)
        from . import optimizer

        try:
            state = optimizer.resume(config, store, run_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="حلقه‌ی بهینه‌سازی پیدا نشد.")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        store.log("info", "api", f"optimizer resumed {run_id}")
        return state

    @router.get("/optimizer/{run_id}/export")
    def optimizer_export(run_id: str, format: str = Query(default="json")) -> Response:
        from . import optimizer

        try:
            state = optimizer.load_state(app_root, run_id)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="حلقه‌ی بهینه‌سازی پیدا نشد.")
        if format == "csv":
            return Response(content=optimizer.export_csv(state), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{run_id}.csv"'})
        if format != "json":
            raise HTTPException(status_code=422, detail="format باید json یا csv باشد.")
        return Response(content=json.dumps(state, ensure_ascii=False, indent=2),
                        media_type="application/json; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'})

    @router.post("/cycles/paper")
    def cycles_paper(body: CycleRequest) -> dict[str, Any]:
        config = get_config()
        _guard_paper(config)
        with _CYCLE_LOCK:
            try:
                engine = Engine(config)
            except (DataUnavailable, DataError, ConnectionError, OSError) as exc:
                store.log("error", "api", f"engine build failed: {type(exc).__name__}")
                raise HTTPException(status_code=503, detail=f"داده در دسترس نیست — سیستم fail-closed است (mode={config.mode})")

            try:
                result = engine.run_cycle(now=datetime.now(timezone.utc), dry_run=body.dry_run)
            except (DataUnavailable, DataError, ConnectionError, OSError) as exc:
                store.log("error", "api", f"cycle data error: {type(exc).__name__}")
                raise HTTPException(status_code=503, detail=f"داده در دسترس نیست — سیستم fail-closed است (mode={config.mode})")

            outcomes = list(getattr(result, "outcomes", []) or [])
            errors = list(getattr(result, "errors", []) or [])
            kill = bool(getattr(result, "kill_switch", False))

            # Data genuinely unreachable -> 503, never a pretend-success.
            if not outcomes and errors and not kill:
                store.log("error", "api", f"cycle unreachable: {errors[:3]}")
                raise HTTPException(status_code=503,
                                    detail=f"داده در دسترس نیست — سیستم fail-closed است (mode={config.mode})")

            if not body.dry_run:
                mirror_cycle(store, engine, config, result, source="paper")

            store.log("info", "api", f"paper cycle: {result.summary()}")
            return {
                "summary": result.summary(),
                "mode": getattr(result, "mode", config.mode),
                "kill_switch": kill,
                "safe_state": bool(getattr(result, "safe_state", False)),
                "config_digest": getattr(result, "config_digest", config_digest(config.raw)),
                "clock": getattr(result, "clock", {}),
                "errors": errors,
                "outcomes": [_outcome_dict(o) for o in outcomes],
                "equity": getattr(result, "equity", None),
                "unrealized": getattr(result, "unrealized", None),
                "drawdown_pct": getattr(result, "drawdown_pct", None),
                "mirrored": not body.dry_run,
                "provenance": _provenance("paper", "engine", getattr(result, "started_at", now_iso())),
            }

    @router.post("/kill-switch")
    def kill_switch(body: KillSwitchRequest) -> dict[str, Any]:
        config = get_config()
        path = Path(config.kill_switch_file)
        digest = config_digest(config.raw)
        if body.active:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{now_iso()}\n", encoding="utf-8")
            store.record_config_event(actor="api", digest=digest, change="kill_switch_enabled",
                                      note=f"file={path.name} mode={config.mode}")
            store.log("warning", "api", f"kill switch enabled by api actor; mode={config.mode}")
            effect = "توقف امن فعال شد — هیچ سفارش جدیدی ثبت نمی‌شود؛ موقعیت‌های باز به‌زور بسته نمی‌شوند."
        else:
            if path.exists():
                path.unlink()
            store.record_config_event(actor="api", digest=digest, change="kill_switch_disabled",
                                      note=f"file={path.name} mode={config.mode}")
            store.log("info", "api", f"kill switch released by api actor; mode={config.mode}")
            effect = "توقف برداشته شد؛ سیستم در همان حالت PAPER ادامه می‌دهد (هیچ مسیر سرویس زنده وجود ندارد)."
        return {"active": body.active, "effect": effect, "kill_switch_file": path.name,
                "mode": config.mode, "provenance": _provenance("paper", "engine")}

    # ---- wire the router --------------------------------------------------
    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}" for e in exc.errors())
        return JSONResponse(status_code=422, content={"detail": f"ورودی نامعتبر است: {problems}"})

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        try:
            store.log("error", "api", f"unhandled {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001 - logging must never mask the response
            pass
        return JSONResponse(status_code=500, content={"detail": "خطای داخلی — سیستم fail-closed است."})

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    dist = app_root / "web" / "dist"
    if dist.exists():
        class SpaStaticFiles(StaticFiles):
            """Serve the SPA shell for client-side routes; missing assets and API paths still 404."""

            async def get_response(self, path: str, scope):  # type: ignore[override]
                # ``path`` arrives normalised with OS separators (``api\v1\...`` on Windows),
                # so the API-vs-route decision has to come from the URL, not from it.
                url_path = str(scope.get("path", ""))
                try:
                    response = await super().get_response(path, scope)
                except StarletteHTTPException as exc:
                    if exc.status_code == 404 and _is_client_route(url_path):
                        response = await super().get_response("index.html", scope)
                    else:
                        raise
                # The shell must revalidate so a new build is picked up on a plain reload;
                # hashed assets can be cached forever.
                if "." not in Path(path).name or Path(path).name == "index.html":
                    response.headers["Cache-Control"] = "no-store, must-revalidate"
                else:
                    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                return response

        app.mount("/", SpaStaticFiles(directory=str(dist), html=True), name="dashboard")

    app.state.root = app_root
    app.state.store = store
    return app


# ---------------------------------------------------------------------------
# Helpers that need the config / store closures kept out of the factory
# ---------------------------------------------------------------------------

def _ledger_metrics(rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    """Mirror of Store.trade_ledger_stats so a symbol filter matches exactly."""
    wins = [t for t in rows if (t["net"] or 0) > 0]
    losses = [t for t in rows if (t["net"] or 0) <= 0]
    gross_win = sum(t["net"] or 0 for t in wins)
    gross_loss = abs(sum(t["net"] or 0 for t in losses))
    return {
        "mode": mode,
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(rows) * 100.0, 2) if rows else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "avg_win": round(gross_win / len(wins), 4) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 4) if losses else None,
        "net_pnl": round(gross_win - gross_loss, 4),
        "fees": round(sum(t["fees"] or 0 for t in rows), 4),
    }


def _guard_paper(config: Config) -> None:
    if config.mode != "PAPER":
        raise HTTPException(
            status_code=409,
            detail=f"حالت {config.mode} است؛ اجرای زنده در این نسخه سیم‌کشی نشده و این endpoint فقط PAPER است.",
        )


def _require_symbol(config: Config, symbol: str) -> str:
    """Resolve the venue's identity for a requested symbol, then enforce the allowlist.

    Crypto keeps its literal ``BASE_QUOTE`` behaviour; a TSE board ticker resolves
    to its numeric insCode through the universe snapshot. An unresolvable symbol
    falls through to the plain allowlist check, which is a 422 — never a guess.
    """
    from .data import DataUnavailable, symbol_key

    try:
        resolved = symbol_key(_gate_provider(config), symbol)
    except DataUnavailable:
        resolved = symbol
    if resolved not in config.symbols:
        raise HTTPException(
            status_code=422,
            detail=f"نماد {symbol} در فهرست مجاز نیست (تعداد نمادهای مجاز: {len(config.symbols)}).",
        )
    return resolved


def _make_gate(config: Config):
    return _gate_provider(config)


def _snapshot_from_bars(symbol: str, timeframe: str, bars: list[Kline], config: Config):
    from .data import DataQuality, MarketSnapshot, Quote, SymbolFilter

    last = bars[-1]
    half = last.close * 0.0001
    quote = Quote(symbol=symbol, bid=last.close - half, ask=last.close + half,
                  ts=last.open_time, source="csv")
    filters = SymbolFilter(symbol=symbol, price_precision=2, qty_precision=6, min_qty=0.0,
                           min_quote=0.0, max_quote=0.0, tradable=True, fee_pct=config.taker_fee_pct)
    return MarketSnapshot(symbol=symbol, timeframe=timeframe, klines=bars, higher_klines=None,
                          quote=quote, cross_quote=None, filters=filters,
                          quality=DataQuality(is_fresh=True, warnings=[]), fetched_at=last.open_time)


def _series_for(store, config: Config, datasets_mod, symbol: str, timeframe: str,
               days: int | None, dataset_id: str | None):
    """Return (bars, higher_bars, filters, dataset_id) from a dataset or a fetch."""
    if dataset_id:
        bars = datasets_mod.load_klines(store, dataset_id)
    elif days:
        gate = _make_gate(config)
        created = datasets_mod.ingest_exchange(store, gate, symbol, timeframe, days)
        dataset_id = created.get("dataset_id") if isinstance(created, dict) else None
        bars = datasets_mod.load_klines(store, dataset_id)
    else:
        raise ValueError("dataset_id یا days لازم است")
    # The confirmation series must be the real confirm timeframe, not an alias of
    # the signal bars: slope/volatility over 200 daily bars is a different signal
    # than over 200 four-hour bars. Aggregating the already-validated history is
    # arithmetic on real data, so it is used instead of inventing a series.
    higher = list(bars)
    confirm_tf = config.confirm_timeframe
    if confirm_tf and confirm_tf != timeframe:
        try:
            aggregated = datasets_mod.resample(bars, confirm_tf)
            if len(aggregated) >= 60:
                higher = aggregated
        except Exception:  # a coarser-to-finer request is invalid; keep the signal series
            higher = list(bars)
    filters = _filters_from_bars(symbol, config)
    return bars, higher, filters, dataset_id


def _filters_from_bars(symbol: str, config: Config):
    from .data import SymbolFilter

    return SymbolFilter(symbol=symbol, price_precision=2, qty_precision=6, min_qty=0.0,
                        min_quote=0.0, max_quote=0.0, tradable=True, fee_pct=config.taker_fee_pct)


def _outcome_dict(outcome: Any) -> dict[str, Any]:
    return {
        "symbol": getattr(outcome, "symbol", ""),
        "action": getattr(outcome, "action", ""),
        "approved": bool(getattr(outcome, "approved", False)),
        "reasons": list(getattr(outcome, "reasons", []) or []),
        "order_id": getattr(outcome, "order_id", None),
        "filled_qty": getattr(outcome, "filled_qty", 0.0),
        "fill_price": getattr(outcome, "fill_price", None),
        "decision_id": getattr(outcome, "decision_id", None),
        "details": getattr(outcome, "details", {}) or {},
    }


app = create_app()
