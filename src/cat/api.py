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
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field

from .backtest import run_backtest
from .config import Config, ConfigError, config_digest, load_config
from .data import DataError, DataUnavailable, Kline
from .engine import Engine
from .http import HttpClient
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
    warmup: int = Field(default=80, ge=1, le=2000)
    assumed_spread_pct: float = Field(default=0.05, ge=0.0, le=5.0)


class WalkForwardRequest(BaseModel):
    symbol: str
    timeframe: str | None = None
    days: int | None = Field(default=None, ge=1, le=1500)
    dataset_id: str | None = None
    grid: dict[str, list[Any]] = Field(default_factory=dict)
    folds: int = Field(default=4, ge=2, le=12)


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
            "symbols": list(config.symbols),
            "timeframe": config.timeframe,
            "confirm_timeframe": config.confirm_timeframe,
            "exchange": config.exchange,
            "allocated_capital": config.allocated_capital,
            "maker_fee_pct": config.maker_fee_pct,
            "taker_fee_pct": config.taker_fee_pct,
            "limits": asdict(config.limits),
            "strategy_params": config.strategy_params.as_dict(),
            "warnings": list(config.warnings),
            "config_digest": config_digest(config.raw),
            "config": raw,
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
    def decisions(date: str | None = None, symbol: str | None = None) -> dict[str, Any]:
        rows = store.list_decisions(date=date, symbol=symbol)
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
        _require_symbol(config, symbol)
        tf = body.timeframe or config.timeframe
        if not body.dataset_id and not body.days:
            raise HTTPException(status_code=422, detail="یا dataset_id یا days باید داده شود.")
        import importlib

        try:
            datasets_mod = importlib.import_module("cat.datasets")
        except ModuleNotFoundError:
            raise HTTPException(status_code=503, detail="ماژول داده در دسترس نیست — سیستم fail-closed است")
        try:
            bars, higher, filters, dataset_id = _series_for(
                store, config, datasets_mod, symbol, tf, body.days, body.dataset_id)
            if len(bars) < 120 or len(higher) < 80:
                raise HTTPException(status_code=422,
                                    detail=f"داده کافی نیست: {len(bars)} کندل سیگنال، {len(higher)} کندل تأیید.")
            params = params_from_dict(body.params, base=config.strategy_params)
            result = run_backtest(config, symbol, bars=bars, higher_bars=higher, filters=filters,
                                  timeframe=tf, warmup=body.warmup, assumed_spread_pct=body.assumed_spread_pct, params=params)
            run_id = store.record_run(
                kind="backtest", symbol=symbol, timeframe=tf, params_label=params.label(),
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
        return {"run_id": run_id, "symbol": symbol, "timeframe": tf, "metrics": result.metrics,
                "params": result.params, "provenance": _provenance("simulated", "engine", f"{result.start} → {result.end}")}

    @router.post("/runs/walk-forward")
    def runs_walk_forward(body: WalkForwardRequest) -> dict[str, Any]:
        config = get_config()
        _guard_paper(config)
        symbol = _normalize_symbol(body.symbol)
        _require_symbol(config, symbol)
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
            """Serve the SPA shell for client-side routes; missing assets still 404."""

            async def get_response(self, path: str, scope):  # type: ignore[override]
                try:
                    return await super().get_response(path, scope)
                except StarletteHTTPException as exc:
                    if exc.status_code == 404 and "." not in Path(path).name:
                        return await super().get_response("index.html", scope)
                    raise

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


def _require_symbol(config: Config, symbol: str) -> None:
    if symbol not in config.symbols:
        raise HTTPException(
            status_code=422,
            detail=f"نماد {symbol} در فهرست مجاز ({', '.join(config.symbols)}) نیست.",
        )


def _make_gate(config: Config):
    from .data import GateProvider

    return GateProvider(HttpClient(proxy=config.proxy))


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
