"""Month-long multi-strategy scan over the approved universe (read-only research).

Fetches closed 4h candles plus a daily confirmation series, runs each entry family
through the same backtest engine and Risk Gate, splits the window into train and
validation halves, and reports pooled measurements. It never places an order,
never writes config, and never changes a risk cap.
"""

from __future__ import annotations

import json
import time
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from statistics import median
from typing import Any, Callable

from .backtest import run_backtest
from .config import Config
from .data import DataQuality, GateProvider, Kline, MarketSnapshot, Quote, SymbolFilter, seconds_per_bar
from .http import HttpClient
from .indicators import ema
from .strategies import StrategyParams, default_params
from .strategy import Proposal, analyze
from .strategy_lab import LAB_STRATEGIES

DISCLAIMER = (
    "این اعداد بازده ثبت‌شده‌ی گذشته‌اند؛ نه پیش‌بینی، نه تضمین و نه مجوز معامله‌ی زنده. "
    "هر نماد در یک حساب مجزای ۱۰۰۰ دلاری شبیه‌سازی شده، بنابراین مجموع آن‌ها پرتفوی واقعی نیست."
)


@dataclass
class ScanOptions:
    days: int = 30
    warmup_bars: int = 180
    timeframe: str = "4h"
    strategies: tuple[str, ...] = tuple(LAB_STRATEGIES)
    assumed_spread_pct: float = 0.05
    cache_dir: Path | None = None
    refresh: bool = False
    improve: bool = True
    max_hold_bars: int = 0


def _now_bar_end(timeframe: str) -> int:
    step = seconds_per_bar(timeframe)
    return int(time.time()) // step * step


def _bar_row(bar: Kline) -> list[Any]:
    return [bar.open_time, bar.open, bar.high, bar.low, bar.close, bar.base_volume, bar.quote_volume, bar.closed]


def _bar_from_row(row: list[Any]) -> Kline:
    return Kline(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]), float(row[6]), bool(row[7]))


def _cache_path(config: Config, options: ScanOptions) -> Path:
    root = options.cache_dir or (Path(config.root) / "state" / "research")
    root.mkdir(parents=True, exist_ok=True)
    # The market belongs in the key: a 30d/1d scan of TSE must never be served
    # from the cache of a 30d/1d scan of Gate.
    return root / f"scan_{config.market}_{options.timeframe}_{options.days + options.warmup_bars // 6}d.json"


def _fetch_symbol(config: Config, provider: Any, symbol: str, options: ScanOptions, end: int) -> dict[str, Any]:
    step = seconds_per_bar(options.timeframe)
    start = end - (options.days * 86400) - options.warmup_bars * step - 5 * step
    higher_end = end
    # A weekly confirmation series needs ~60 closed weeks; a 300-day window only
    # yields ~43 of them, which silently skipped every TSE symbol. For every other
    # confirm timeframe the daily overlap must still cover the whole signal window
    # plus its warmup: a fixed 300 days starved the first ~65 days of a 365-day run.
    if config.confirm_timeframe == "1w":
        higher_days = 700
    else:
        higher_days = max(300, int((options.days * 86400 + options.warmup_bars * step) / 86400) + 90)
    higher_start = end - higher_days * 86400
    bars = [bar for bar in provider.klines_range(symbol, options.timeframe, start, end) if bar.closed]
    higher = [bar for bar in provider.klines_range(symbol, config.confirm_timeframe, higher_start, higher_end) if bar.closed]
    filters = provider.filters(symbol)
    return {
        "symbol": symbol,
        "bars": [_bar_row(bar) for bar in bars],
        "higher": [_bar_row(bar) for bar in higher],
        "filters": {
            "symbol": filters.symbol,
            "price_precision": filters.price_precision,
            "qty_precision": filters.qty_precision,
            "min_qty": filters.min_qty,
            "min_quote": filters.min_quote,
            "max_quote": filters.max_quote,
            "tradable": filters.tradable,
            "fee_pct": filters.fee_pct,
        },
    }


def load_history(config: Config, symbols: list[str], options: ScanOptions, *, progress: Callable[[str], None] = print) -> dict[str, Any]:
    path = _cache_path(config, options)
    end = _now_bar_end(options.timeframe)
    step = seconds_per_bar(options.timeframe)
    if path.exists() and not options.refresh:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            stamp = int(cached.get("coverage_end") or 0)
            if stamp >= end - 2 * step and set(cached.get("symbols", {})) >= set(symbols):
                progress(f"cache: {path.name} ({len(cached['symbols'])} symbols, end {datetime.fromtimestamp(stamp, tz=timezone.utc):%Y-%m-%d %H:%M}Z)")
                return cached
        except (ValueError, OSError):
            pass

    spec = config.spec
    provider = spec.build_provider(HttpClient(proxy=config.proxy, headers=dict(spec.http_headers)), config)
    workers = max(1, min(8, int(getattr(config, "max_concurrency", 4) or 4)))
    progress(
        f"fetch: {len(symbols)} symbols × {options.timeframe} + {config.confirm_timeframe} "
        f"on {spec.name} via {provider.name} ({config.proxy or 'direct'}, workers={workers})"
    )
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_symbol, config, provider, symbol, options, end): symbol for symbol in symbols}
        for done, future in enumerate(as_completed(futures), 1):
            symbol = futures[future]
            try:
                results[symbol] = future.result()
            except Exception as exc:  # noqa: BLE001 - one symbol must not kill the scan
                errors[symbol] = f"{type(exc).__name__}: {exc}"
            if done % 20 == 0 or done == len(symbols):
                progress(f"fetch: {done}/{len(symbols)} done, {len(errors)} errors, {time.time() - started:.0f}s")

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coverage_end": end,
        "timeframe": options.timeframe,
        "days": options.days,
        "warmup_bars": options.warmup_bars,
        "source": f"{provider.name}:{config.market}",
        "market": config.market,
        "symbols": results,
        "errors": errors,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    progress(f"saved: {path} ({len(results)} symbols, {len(errors)} errors)")
    return payload


def _filters(raw: dict[str, Any]) -> SymbolFilter:
    return SymbolFilter(
        symbol=raw["symbol"], price_precision=int(raw["price_precision"]), qty_precision=int(raw["qty_precision"]),
        min_qty=float(raw["min_qty"]), min_quote=float(raw["min_quote"]), max_quote=float(raw["max_quote"]),
        tradable=bool(raw["tradable"]), fee_pct=float(raw["fee_pct"]),
    )


def _snapshot(symbol: str, timeframe: str, bars: list[Kline], higher: list[Kline], filters: SymbolFilter, spread_pct: float) -> MarketSnapshot:
    last = bars[-1]
    half = last.close * spread_pct / 200.0
    ts = last.open_time + seconds_per_bar(timeframe)
    quote = Quote(symbol=symbol, bid=last.close - half, ask=last.close + half, ts=ts, source="history")
    return MarketSnapshot(
        symbol=symbol, timeframe=timeframe, klines=bars, higher_klines=higher, quote=quote, cross_quote=None,
        filters=filters, quality=DataQuality(is_fresh=True, warnings=[], age_seconds=0.0), fetched_at=ts,
    )


def _signal_kwargs(config: Config, params: StrategyParams) -> dict[str, Any]:
    return {
        "confidence_threshold": config.confidence_threshold,
        "require_multiple_signals": config.require_multiple_signals,
        "no_trade_if_signals_conflict": config.no_trade_if_signals_conflict,
        "min_reward_risk_ratio": config.limits.min_reward_risk_ratio,
        "maker_fee_pct": config.maker_fee_pct,
        "taker_fee_pct": config.taker_fee_pct,
        "est_slippage_pct": config.limits.max_slippage_pct,
        "params": params,
    }


def _strategy_callable(name: str, extra: dict[str, Any]) -> Callable[..., Any]:
    fn = LAB_STRATEGIES[name]
    if not extra:
        return fn
    return partial(fn, **extra)


def breadth_series(data: dict[str, Any], symbols: list[str], step: int, period: int = 50) -> list[tuple[int, float]]:
    """Share of symbols trading above their own EMA50, per closed-bar timestamp."""
    counts: dict[int, list[bool]] = {}
    for symbol in symbols:
        raw = data.get(symbol)
        if not raw or len(raw["bars"]) < period + 1:
            continue
        closes = [float(row[4]) for row in raw["bars"]]
        series = ema(closes, period)
        if not series:
            continue
        offset = period - 1
        for index in range(offset, len(closes)):
            stamp = int(raw["bars"][index][0]) + step
            counts.setdefault(stamp, []).append(closes[index] > series[index - offset])
    return sorted((stamp, sum(values) / len(values)) for stamp, values in counts.items() if len(values) >= 10)


def breadth_at(series: list[tuple[int, float]], timestamp: float) -> float | None:
    """Latest breadth snapshot at or before the decision moment; None when unavailable."""
    if not series:
        return None
    index = bisect_right(series, (int(timestamp), float("inf"))) - 1
    return series[index][1] if index >= 0 else None


def _breadth_wrapper(fn: Callable[..., Any], series: list[tuple[int, float]], threshold: float) -> Callable[..., Any]:
    def wrapped(snapshot: MarketSnapshot, position: Any, **kw: Any) -> Proposal:
        if position is None:
            share = breadth_at(series, snapshot.fetched_at)
            if share is None or share < threshold:
                analysis = analyze(snapshot, kw.get("params") or default_params())
                reason = "market_breadth_unavailable" if share is None else "market_breadth"
                text = "breadth unavailable at this timestamp" if share is None else f"only {share:.0%} of the universe above EMA50 (< {threshold:.0%})"
                return Proposal("NO_TRADE", 0.0, [text], None, None, None, analysis, reason)
        return fn(snapshot, position, **kw)

    return wrapped


def _run_one(
    config: Config,
    symbol: str,
    bars: list[Kline],
    higher: list[Kline],
    filters: SymbolFilter,
    *,
    timeframe: str,
    warmup: int,
    signal: Callable[..., Any],
    params: StrategyParams,
    spread_pct: float,
) -> dict[str, Any]:
    result = run_backtest(
        config, symbol, bars=bars, higher_bars=higher, filters=filters,
        timeframe=timeframe, warmup=warmup, assumed_spread_pct=spread_pct,
        params=params, propose_fn=signal,
    )
    return {
        "symbol": symbol,
        "metrics": result.metrics,
        "buy_hold_pct": round(result.buy_hold_pct, 3),
        "trades": [
            {"symbol": trade.symbol, "pnl": round(trade.pnl, 6), "fees": round(trade.fees, 6), "reason": trade.reason,
             "entry_time": trade.entry_time, "exit_time": trade.exit_time,
             "entry": round(trade.entry, 8), "exit": round(trade.exit, 8), "qty": round(trade.qty, 8),
             "holding_hours": round(max(0.0, (trade.exit_time - trade.entry_time) / 3600.0), 2),
             "return_pct": round((trade.exit / trade.entry - 1) * 100.0, 3) if trade.entry else None}
            for trade in result.trades
        ],
        "rejected": result.rejected,
    }


def _pooled(rows: list[dict[str, Any]]) -> dict[str, Any]:
    trades = [trade for row in rows for trade in row["trades"]]
    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] <= 0]
    gross_win = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    nets = [float(row["metrics"]["net_return_pct"]) for row in rows]
    bhs = [float(row["buy_hold_pct"]) for row in rows]
    dds = [float(row["metrics"]["max_drawdown_pct"]) for row in rows]
    rejected: dict[str, int] = {}
    for row in rows:
        for key, value in row["rejected"].items():
            rejected[key] = rejected.get(key, 0) + value
    traded = [row for row in rows if row["trades"]]
    holds = sorted(
        (trade["exit_time"] - trade["entry_time"]) / 3600.0
        for trade in trades
        if trade.get("entry_time") and trade.get("exit_time") and trade["exit_time"] >= trade["entry_time"]
    )
    buckets = {"lt_1h": 0, "1_3h": 0, "3_8h": 0, "8_24h": 0, "gte_24h": 0}
    for hours in holds:
        if hours < 1:
            buckets["lt_1h"] += 1
        elif hours <= 3:
            buckets["1_3h"] += 1
        elif hours <= 8:
            buckets["3_8h"] += 1
        elif hours < 24:
            buckets["8_24h"] += 1
        else:
            buckets["gte_24h"] += 1
    return {
        "symbols": len(rows),
        "symbols_with_trades": len(traded),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 2) if trades else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "net_pnl_sum": round(sum(float(row["metrics"]["net_pnl"]) for row in rows), 2),
        "mean_net_return_pct": round(sum(nets) / len(nets), 3) if nets else None,
        "median_net_return_pct": round(median(nets), 3) if nets else None,
        "positive_symbols_pct": round(sum(1 for value in nets if value > 0) / len(nets) * 100.0, 1) if nets else None,
        "median_buy_hold_pct": round(median(bhs), 3) if bhs else None,
        "beat_buy_hold_pct": round(sum(1 for row in traded if float(row["metrics"]["net_return_pct"]) > float(row["buy_hold_pct"])) / len(traded) * 100.0, 1) if traded else None,
        "median_drawdown_pct": round(median(dds), 3) if dds else None,
        "fees_sum": round(sum(float(row["metrics"]["fees_paid"]) for row in rows), 2),
        "median_hold_hours": round(median(holds), 2) if holds else None,
        "mean_hold_hours": round(sum(holds) / len(holds), 2) if holds else None,
        "hold_under_3h_pct": round(sum(1 for hours in holds if hours <= 3) / len(holds) * 100.0, 1) if holds else None,
        "hold_buckets": buckets,
        "rejected": dict(sorted(rejected.items(), key=lambda item: item[1], reverse=True)[:8]),
    }


def _universe_volume(config: Config) -> dict[str, float]:
    path = Path(config.root) / "universe.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    volumes: dict[str, float] = {}
    for entry in payload.get("entries", []):
        symbol = str(entry.get("symbol") or "").replace("/", "_")
        if symbol:
            volumes[symbol] = float(entry.get("volume_24h_usd") or 0.0)
    return volumes


def _window_rows(
    config: Config,
    data: dict[str, Any],
    symbols: list[str],
    options: ScanOptions,
    *,
    name: str,
    extra: dict[str, Any] | None = None,
    params: StrategyParams | None = None,
    signal: Callable[..., Any] | None = None,
    segment: str,
    progress: Callable[[str], None] = print,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    params = params or default_params()
    # One implementation of the horizon rule: it lives in StrategyParams and is
    # applied by strategy.propose, so the scan cannot drift from the paper engine.
    if options.max_hold_bars > 0 and not params.max_hold_bars:
        params = params.replace(max_hold_bars=options.max_hold_bars)
    signal = signal or _strategy_callable(name, extra or {})
    step = seconds_per_bar(options.timeframe)
    test_bars = options.days * 86400 // step
    rows: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}
    for index, symbol in enumerate(symbols, 1):
        raw = data.get(symbol)
        if not raw:
            skipped[symbol] = "missing"
            continue
        bars = [_bar_from_row(row) for row in raw["bars"]]
        higher = [_bar_from_row(row) for row in raw["higher"]]
        if len(bars) < options.warmup_bars + 60 or len(higher) < 60:
            skipped[symbol] = f"insufficient_history:{len(bars)}/{len(higher)}"
            continue
        test_start = max(len(bars) - test_bars, options.warmup_bars)
        mid = test_start + (len(bars) - test_start) // 2
        if segment == "train":
            window = bars[: mid + 1]
            warmup = test_start
        else:
            window = bars
            warmup = mid
        if warmup >= len(window) - 2:
            skipped[symbol] = "empty_window"
            continue
        try:
            rows.append(_run_one(
                config, symbol, window, higher, _filters(raw["filters"]),
                timeframe=options.timeframe, warmup=warmup, signal=signal, params=params,
                spread_pct=options.assumed_spread_pct,
            ))
        except Exception as exc:  # noqa: BLE001
            skipped[symbol] = f"{type(exc).__name__}: {exc}"
        if index % 25 == 0:
            progress(f"  {name}/{segment}: {index}/{len(symbols)} symbols, {len(rows)} rows")
    return rows, skipped


def latest_signals(config: Config, data: dict[str, Any], symbols: list[str], options: ScanOptions) -> dict[str, list[dict[str, Any]]]:
    """Entries that trigger on the last fully closed bar, with planned stop/target."""
    out: dict[str, list[dict[str, Any]]] = {name: [] for name in options.strategies}
    for symbol in symbols:
        raw = data.get(symbol)
        if not raw:
            continue
        bars = [_bar_from_row(row) for row in raw["bars"]]
        higher = [_bar_from_row(row) for row in raw["higher"]]
        if len(bars) < 90:
            continue
        filters = _filters(raw["filters"])
        snap = _snapshot(symbol, options.timeframe, bars, higher, filters, options.assumed_spread_pct)
        for name in options.strategies:
            fn = LAB_STRATEGIES[name]
            params = default_params()
            proposal = fn(snap, None, **_signal_kwargs(config, params))
            if proposal.action == "BUY":
                out[name].append({
                    "symbol": symbol,
                    "confidence": proposal.confidence,
                    "entry": round(proposal.entry or 0.0, 6),
                    "stop": round(proposal.stop or 0.0, 6),
                    "target": round(proposal.target or 0.0, 6),
                    "stop_pct": round((1 - (proposal.stop or 0.0) / (proposal.entry or 1)) * 100.0, 2),
                    "target_pct": round(((proposal.target or 0.0) / (proposal.entry or 1) - 1) * 100.0, 2),
                    "reasons": proposal.rationale[:3],
                    "bar_time": datetime.fromtimestamp(bars[-1].open_time, tz=timezone.utc).isoformat(timespec="minutes"),
                })
    for rows in out.values():
        rows.sort(key=lambda row: row["confidence"], reverse=True)
    return out


def _best_symbols(rows: list[dict[str, Any]], count: int = 5) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(rows, key=lambda row: float(row["metrics"]["net_return_pct"]))
    return ordered[-count:][::-1], ordered[:count]


def run_scan(config: Config, options: ScanOptions, *, progress: Callable[[str], None] = print) -> dict[str, Any]:
    symbols = [symbol.replace("/", "_") for symbol in config.symbols]
    if not symbols:
        raise ValueError("config has no symbols")
    data = load_history(config, symbols, options, progress=progress)
    available = [symbol for symbol in symbols if symbol in data.get("symbols", {})]
    volumes = _universe_volume(config)

    scan: dict[str, Any] = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "timeframe": options.timeframe,
            "days": options.days,
            "warmup_bars": options.warmup_bars,
            "max_hold_bars": options.max_hold_bars,
            "symbols_requested": len(symbols),
            "symbols_with_data": len(available),
            "data_errors": data.get("errors", {}),
            "source": data.get("source"),
            "coverage_end": data.get("coverage_end"),
            "disclaimer": DISCLAIMER,
        },
        "strategies": {},
        "symbols": {},
    }
    for name in options.strategies:
        progress(f"scan: {name}")
        signal = _strategy_callable(name, {})
        train, skipped_train = _window_rows(config, data["symbols"], available, options, name=name, signal=signal, segment="train", progress=progress)
        validation, skipped_val = _window_rows(config, data["symbols"], available, options, name=name, signal=signal, segment="validation", progress=progress)
        best, worst = _best_symbols(validation)
        scan["strategies"][name] = {
            "train": _pooled(train),
            "validation": _pooled(validation),
            "skipped": {**skipped_train, **skipped_val},
            "top_validation": [{"symbol": row["symbol"], **row["metrics"], "buy_hold_pct": row["buy_hold_pct"]} for row in best],
            "bottom_validation": [{"symbol": row["symbol"], **row["metrics"], "buy_hold_pct": row["buy_hold_pct"]} for row in worst],
        }

    scan["symbols"] = {row["symbol"]: row for row in _latest_rows(config, data["symbols"], available, options)}
    scan["latest"] = latest_signals(config, data["symbols"], available, options)

    if options.improve:
        scan["improvement"] = _run_improvements(config, data["symbols"], available, volumes, options, progress=progress)
    return scan


def _latest_rows(config: Config, data: dict[str, Any], symbols: list[str], options: ScanOptions) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    step = seconds_per_bar(options.timeframe)
    test_bars = options.days * 86400 // step
    for symbol in symbols:
        raw = data.get(symbol)
        if not raw or len(raw["bars"]) < 90:
            continue
        bars = [_bar_from_row(row) for row in raw["bars"]]
        start = max(0, len(bars) - test_bars)
        base = bars[start].close
        last_close = bars[-1].close
        prior = bars[-2].close
        rows.append({
            "symbol": symbol,
            "last_close": last_close,
            "last_bar_change_pct": round((last_close - prior) / prior * 100.0, 3) if prior else None,
            "buy_hold_pct": round((last_close - base) / base * 100.0, 3) if base else None,
        })
    return rows


def _run_improvements(
    config: Config,
    data: dict[str, Any],
    available: list[str],
    volumes: dict[str, float],
    options: ScanOptions,
    *,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    base_params = default_params()
    ranked = [name for name in options.strategies if name != "trend_v1"] or list(options.strategies)
    breadth = breadth_series(data, available, seconds_per_bar(options.timeframe))
    candidates: list[dict[str, Any]] = []

    def add(label: str, name: str, *, extra: dict[str, Any] | None = None, params: StrategyParams | None = None,
            symbols: list[str] | None = None, wrap: float | None = None) -> None:
        signal = _strategy_callable(name, extra or {})
        if wrap is not None:
            signal = _breadth_wrapper(signal, breadth, wrap)
        candidates.append({
            "label": label, "name": name, "params": params, "symbols": symbols or available, "signal": signal,
        })

    for name in ranked:
        add(f"{name}:base", name)
        add(f"{name}:atr_0.8-4.0", name, extra={"min_atr_pct": 0.8, "max_atr_pct": 4.0})
        add(f"{name}:stop2_target4", name, params=replace(base_params, atr_stop_mult=2.0, atr_target_mult=4.0))
        if breadth:
            add(f"{name}:breadth50", name, wrap=0.50)
            add(f"{name}:breadth50+stop2_target4", name, params=replace(base_params, atr_stop_mult=2.0, atr_target_mult=4.0), wrap=0.50)
    if volumes:
        liquid = sorted(available, key=lambda symbol: volumes.get(symbol, 0.0), reverse=True)[:50]
        for name in ranked:
            add(f"{name}:top50_liquidity", name, symbols=liquid)
            add(f"{name}:top50+atr_0.8-4.0", name, extra={"min_atr_pct": 0.8, "max_atr_pct": 4.0}, symbols=liquid)

    results: list[dict[str, Any]] = []
    for candidate in candidates:
        rows, _ = _window_rows(
            config, data, candidate["symbols"], options, name=candidate["name"], params=candidate["params"],
            signal=candidate["signal"], segment="train", progress=progress,
        )
        summary = _pooled(rows)
        results.append({"label": candidate["label"], "name": candidate["name"], "train": summary})
        progress(f"improve: {candidate['label']} train trades={summary['trades']} pf={summary['profit_factor']} net={summary['mean_net_return_pct']}")
    eligible = [row for row in results if row["train"]["trades"] >= 20 and row["train"]["profit_factor"] is not None]
    if not eligible:
        return {"candidates": results, "chosen": None, "note": "هیچ کاندیدایی حداقل ۲۰ معامله در train نداشت", "breadth": _breadth_summary(breadth)}
    chosen = max(eligible, key=lambda row: (row["train"]["profit_factor"] or 0.0, row["train"]["mean_net_return_pct"] or -99))
    spec = next(candidate for candidate in candidates if candidate["label"] == chosen["label"])
    validation, _ = _window_rows(
        config, data, spec["symbols"], options, name=spec["name"], params=spec["params"],
        signal=spec["signal"], segment="validation", progress=progress,
    )
    chosen["validation"] = _pooled(validation)
    progress(f"improve: chose {chosen['label']} on train; validation pf={chosen['validation']['profit_factor']} net={chosen['validation']['mean_net_return_pct']}")
    return {"candidates": results, "chosen": chosen, "breadth": _breadth_summary(breadth)}


def _breadth_summary(series: list[tuple[int, float]]) -> dict[str, Any] | None:
    if not series:
        return None
    values = [share for _, share in series]
    latest_stamp, latest = series[-1]
    return {
        "points": len(series),
        "latest": round(latest, 3),
        "latest_at": datetime.fromtimestamp(latest_stamp, tz=timezone.utc).isoformat(timespec="minutes"),
        "median": round(median(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "above_50_pct": round(sum(1 for value in values if value >= 0.5) / len(values) * 100.0, 1),
    }


def _fmt(value: Any, suffix: str = "", *, signed: bool = False) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:+.2f}{suffix}" if signed else f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def render_report(scan: dict[str, Any]) -> str:
    meta = scan["meta"]
    lines = [
        f"# اسکن {meta['days']} روزه‌ی چند استراتژی ({meta['timeframe']})",
        "",
        f"- داده: `{meta['source']}` · آخرین کندل بسته: `{datetime.fromtimestamp(int(meta['coverage_end']), tz=timezone.utc):%Y-%m-%d %H:%M}Z`",
        f"- نمادها: {meta['symbols_with_data']} از {meta['symbols_requested']} · تولید: {meta['generated_at']}",
    ]
    max_hold = int(meta.get("max_hold_bars") or 0)
    if max_hold:
        hours = max_hold * seconds_per_bar(meta["timeframe"]) / 3600
        lines.append(f"- سقف نگه‌داری: {max_hold} کندل ({hours:.0f} ساعت) — خروج زمانی اجباری")
    lines += [
        f"- ⚠️ {DISCLAIMER}",
        "",
        "## مقایسه‌ی استراتژی‌ها (نمادهای جداگانه، سرمایه‌ی مجزای ۱۰۰۰ USDT)",
        "",
        "| استراتژی | بخش | معامله | نماد معامله‌شده | برد% | PF | میانگین بازده% | میانه بازده% | نماد سودده% | میانه B&H% | جلوتر از B&H% | حداکثر DD میانه% | میانه نگه‌داری (ساعت) | ≤۳ ساعت% |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    labels = {"trend_v1": "trend v1 (EMA)", "breakout_30": "breakout 30", "momentum_20": "momentum 20", "pullback_rsi": "pullback RSI"}
    halves = {True: "train (نیمه‌ی اول بازه)", False: "validation (نیمه‌ی دوم بازه)"}
    for name, block in scan["strategies"].items():
        for segment in ("train", "validation"):
            row = block[segment]
            lines.append(
                f"| {labels.get(name, name)} | {halves[segment == 'train']} "
                f"| {row['trades']} | {row['symbols_with_trades']} | {_fmt(row['win_rate_pct'])} "
                f"| {_fmt(row['profit_factor'])} | {_fmt(row['mean_net_return_pct'], signed=True)} "
                f"| {_fmt(row['median_net_return_pct'], signed=True)} "
                f"| {_fmt(row['positive_symbols_pct'])} | {_fmt(row['median_buy_hold_pct'], signed=True)} "
                f"| {_fmt(row['beat_buy_hold_pct'])} | {_fmt(row['median_drawdown_pct'])} "
                f"| {_fmt(row.get('median_hold_hours'))} | {_fmt(row.get('hold_under_3h_pct'))} |"
            )
    lines += ["", "### مدت نگه‌داری معامله‌ها (validation)", ""]
    bucket_labels = [("lt_1h", "<۱ ساعت"), ("1_3h", "۱–۳ ساعت"), ("3_8h", "۳–۸ ساعت"), ("8_24h", "۸–۲۴ ساعت"), ("gte_24h", "≥۲۴ ساعت")]
    lines += ["| استراتژی | " + " | ".join(label for _, label in bucket_labels) + " |", "|---|" + "---|" * len(bucket_labels)]
    for name, block in scan["strategies"].items():
        buckets = block["validation"].get("hold_buckets") or {}
        lines.append(f"| {labels.get(name, name)} | " + " | ".join(str(buckets.get(key, 0)) for key, _ in bucket_labels) + " |")
    lines += ["", "### دلایل رد شدن (پنج مورد اول)", ""]
    for name, block in scan["strategies"].items():
        rejected = block["validation"]["rejected"]
        top = ", ".join(f"{key}={value}" for key, value in list(rejected.items())[:5]) or "—"
        lines.append(f"- **{labels.get(name, name)}** — validation: {top}")

    improvement = scan.get("improvement")
    if improvement:
        breadth = improvement.get("breadth")
        if breadth:
            lines += [
                "",
                "## وسعت بازار (سهم نمادهای بالای EMA50 خودشان)",
                "",
                f"- نقاط: {breadth['points']} · آخرین: {breadth['latest']:.0%} در {breadth['latest_at']}",
                f"- میانه: {breadth['median']:.0%} · کمینه: {breadth['min']:.0%} · بیشینه: {breadth['max']:.0%} · "
                f"سهم زمان بالای ۵۰٪: {breadth['above_50_pct']}%",
            ]
        lines += ["", "## فاز بهبود (انتخاب روی train، داوری روی validation)", ""]
        if improvement.get("chosen"):
            chosen = improvement["chosen"]
            lines += [
                f"- کاندید انتخاب‌شده: `{chosen['label']}`",
                f"- train: معامله {chosen['train']['trades']} · PF {_fmt(chosen['train']['profit_factor'])} · "
                f"میانگین بازده {_fmt(chosen['train']['mean_net_return_pct'], signed=True)}%",
                f"- validation: معامله {chosen['validation']['trades']} · PF {_fmt(chosen['validation']['profit_factor'])} · "
                f"میانگین بازده {_fmt(chosen['validation']['mean_net_return_pct'], signed=True)}% · "
                f"نماد سودده {_fmt(chosen['validation']['positive_symbols_pct'])}%",
                f"- مدت نگه‌داری در validation: میانه {_fmt(chosen['validation'].get('median_hold_hours'))} ساعت · "
                f"≤۳ ساعت {_fmt(chosen['validation'].get('hold_under_3h_pct'))}% · "
                f"≥۲۴ ساعت {_fmt((chosen['validation'].get('hold_buckets') or {}).get('gte_24h'))} معامله",
                "",
                "| کاندید | معامله train | PF train | بازده train% |",
                "|---|---|---|---|",
            ]
            for row in improvement["candidates"]:
                lines.append(
                    f"| {row['label']} | {row['train']['trades']} | {_fmt(row['train']['profit_factor'])} "
                    f"| {_fmt(row['train']['mean_net_return_pct'], signed=True)} |"
                )
        else:
            lines.append(f"- {improvement.get('note')}")

    latest = scan.get("latest") or {}
    if latest:
        lines += ["", "## سیگنال روی آخرین کندل بسته (برنامه‌ی فرضی، نه سفارش)", ""]
        for name, rows in latest.items():
            lines.append(f"### {labels.get(name, name)} — {len(rows)} سیگنال ورود")
            if not rows:
                lines.append("هیچ نمادی شرایط ورود را ندارد.")
                continue
            lines += ["| نماد | اطمینان | ورود | حد ضرر | هدف | ریسک% | پاداش% |", "|---|---|---|---|---|---|---|"]
            for row in rows[:10]:
                lines.append(f"| {row['symbol']} | {row['confidence']:.2f} | {row['entry']} | {row['stop']} | {row['target']} | {row['stop_pct']} | {row['target_pct']} |")
            lines.append("")
    return "\n".join(lines)


def write_report(scan: dict[str, Any], out: Path | None, config: Config) -> tuple[Path, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    days = int(scan["meta"]["days"])
    reports = Path(config.root) / "state" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    markdown = out or reports / f"month_scan_{days}d_{stamp}.md"
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(render_report(scan), encoding="utf-8")
    payload = markdown.with_suffix(".json")
    payload.write_text(json.dumps(scan, ensure_ascii=False, default=str), encoding="utf-8")
    return markdown, payload
