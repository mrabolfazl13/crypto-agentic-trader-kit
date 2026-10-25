"""Backtest orchestration: data -> signals -> candidate trades -> portfolio metrics.

All evaluation is chronological. Windows are cut on timestamps, never shuffled,
and the hold-out slice is only ever read by the final evaluation. The engine
treats the live risk limits in ``config.yaml`` as read-only; sizing comes from
``quant.backtest`` and is never changed by a search.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

from .features import QuantParams, TIMEFRAME_SECONDS, gate_funnel, resample, signal_frame
from .settings import QuantSettings
from .sim import SimSettings, SignalTrade, equal_weight_buy_hold, generate_trades, simulate_portfolio
from .storage import StorageError, current_data_version, load_manifest, read_symbol

BAR_COLUMNS = ("ts", "open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class Window:
    start_ts: int
    end_ts: int

    @property
    def days(self) -> float:
        return round((self.end_ts - self.start_ts) / 86400.0, 2)

    def iso(self) -> dict[str, str]:
        return {"start": _iso(self.start_ts), "end": _iso(self.end_ts)}


def _iso(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")


def _month_start(month: str) -> int:
    from datetime import datetime, timezone

    year, number = (int(part) for part in month.split("-"))
    return int(datetime(year, number, 1, tzinfo=timezone.utc).timestamp())


def _month_end(month: str) -> int:
    from datetime import datetime, timezone

    year, number = (int(part) for part in month.split("-"))
    if number == 12:
        return int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
    return int(datetime(year, number + 1, 1, tzinfo=timezone.utc).timestamp())


def months_for_window(settings: QuantSettings, start_ts: int | None,
                      end_ts: int | None) -> list[str]:
    """Only the stored months that can contain bars in ``[start_ts, end_ts)``."""
    if start_ts is None and end_ts is None:
        return list(settings.months)
    months = []
    for month in settings.months:
        if end_ts is not None and _month_start(month) >= int(end_ts):
            continue
        if start_ts is not None and _month_end(month) <= int(start_ts):
            continue
        months.append(month)
    return months


def load_bars(settings: QuantSettings, symbol: str, *, start_ts: int | None = None,
              end_ts: int | None = None) -> pd.DataFrame:
    """Read one symbol's 1m bars, keeping gaps visible (no reindex, no fill)."""
    frame = read_symbol(settings, symbol, months=months_for_window(settings, start_ts, end_ts))
    if frame.empty:
        raise StorageError(f"{symbol}: stored dataset is empty")
    for column in BAR_COLUMNS:
        if column not in frame.columns:
            raise StorageError(f"{symbol}: missing column {column}")
    if start_ts is not None:
        frame = frame[frame["ts"] >= int(start_ts)]
    if end_ts is not None:
        frame = frame[frame["ts"] < int(end_ts)]
    frame = frame.sort_values("ts", kind="stable").drop_duplicates("ts", keep="last").reset_index(drop=True)
    if frame.empty:
        raise StorageError(f"{symbol}: no bars inside the requested window")
    # Fail closed on a corrupt lake: a NaN or non-positive price must stop the
    # run instead of being resampled into a plausible-looking candle.
    values = frame[list(BAR_COLUMNS[1:])].to_numpy(dtype="float64", copy=False)
    if not np.isfinite(values).all():
        raise StorageError(f"{symbol}: non-finite OHLCV inside the requested window")
    prices = frame[list(BAR_COLUMNS[1:5])].to_numpy(dtype="float64", copy=False)
    if (prices <= 0).any():
        raise StorageError(f"{symbol}: non-positive price inside the requested window")
    if (frame["volume"].to_numpy(dtype="float64") < 0).any():
        raise StorageError(f"{symbol}: negative volume inside the requested window")
    if (frame["ts"].to_numpy(dtype="int64") % 60 != 0).any():
        raise StorageError(f"{symbol}: timestamps are not on a 60-second grid")
    return frame


def simulate_settings(settings: QuantSettings, **overrides: Any) -> SimSettings:
    backtest = settings.backtest
    values = {
        "initial_capital": backtest.initial_capital,
        "fee_pct": backtest.fee_pct,
        "slippage_pct": backtest.slippage_pct,
        "position_pct": backtest.position_pct,
        "max_open_positions": backtest.max_open_positions,
        "max_hold_hours": backtest.max_hold_hours,
        "min_notional": 10.0,
    }
    values.update({key: value for key, value in overrides.items() if key in values})
    return SimSettings(**values)


def symbol_candidates(settings: QuantSettings, symbol: str, *, strategy: str, timeframe: str,
                      params: QuantParams, sim: SimSettings, start_ts: int | None = None,
                      end_ts: int | None = None, warmup: bool = True) -> dict[str, Any]:
    """Candidate trades for one symbol. Warmup bars may precede ``start_ts``; entries may not."""
    warmup_seconds = TIMEFRAME_SECONDS[timeframe] * params.warmup_bars() if warmup else 0
    load_start = int(start_ts) - warmup_seconds if start_ts is not None else None
    bars = load_bars(settings, symbol, start_ts=load_start, end_ts=end_ts)
    signal_bars = resample(bars, timeframe)
    signals = signal_frame(signal_bars, strategy, params)
    # Diagnostics describe the evaluated window only: warm-up bars are loaded so
    # indicators are valid at the first evaluated bar, not to be counted as trades.
    diagnostic_bars = signals
    if start_ts is not None:
        diagnostic_bars = diagnostic_bars[diagnostic_bars["bar_end"] >= int(start_ts)]
    if end_ts is not None:
        diagnostic_bars = diagnostic_bars[diagnostic_bars["bar_end"] < int(end_ts)]
    gates = gate_funnel(diagnostic_bars, strategy, params)
    trades, rejections = generate_trades(bars, signals, symbol=symbol, strategy=strategy,
                                         params=params, settings=sim, entry_after_ts=start_ts)
    # The benchmark must measure the evaluated window, not the indicator warmup
    # that was loaded in front of it: otherwise every fold's buy & hold spans
    # extra days the strategy was never allowed to trade.
    benchmark_bars = bars if start_ts is None else bars[bars["ts"] >= int(start_ts)]
    closes = benchmark_bars["close"].to_numpy(dtype="float64") if not benchmark_bars.empty else None
    return {
        "symbol": symbol,
        "bars": int(len(bars)),
        "signal_bars": int(len(signals)),
        "entry_signals": int(signals["entry"].sum()),
        "candidates": trades,
        "rejections": rejections,
        "gates": gates,
        "first_ts": int(bars["ts"].iloc[0]),
        "last_ts": int(bars["ts"].iloc[-1]),
        "first_close": float(closes[0]) if closes is not None else None,
        "last_close": float(closes[-1]) if closes is not None else None,
        "gaps": int((signals["complete"] == False).sum()),  # noqa: E712 - explicit for pandas
    }


def _empty_diagnostics() -> dict[str, Any]:
    return {"bars": 0, "complete_bars": 0, "indicator_ready_bars": 0, "ready_bars": 0,
            "entry_signals": 0, "exit_signals": 0, "funnel": {}, "conditions": {}}


def _merge_diagnostics(total: dict[str, Any], part: dict[str, Any]) -> None:
    for key in ("bars", "complete_bars", "indicator_ready_bars", "ready_bars",
                "entry_signals", "exit_signals"):
        total[key] += int(part.get(key) or 0)
    for step in part.get("funnel") or []:
        gate = step["gate"]
        total["funnel"][gate] = total["funnel"].get(gate, 0) + int(step["passing"])
    for name, value in (part.get("conditions") or {}).items():
        total["conditions"][name] = total["conditions"].get(name, 0) + int(value)


def run_backtest(settings: QuantSettings, symbols: Iterable[str], *, strategy: str, timeframe: str,
                 params: QuantParams, sim: SimSettings | None = None, start_ts: int | None = None,
                 end_ts: int | None = None, progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Portfolio backtest over the given symbols, with an equal-weight benchmark."""
    symbols = list(symbols)
    sim = sim or simulate_settings(settings)
    started = time.monotonic()
    candidates: list[SignalTrade] = []
    per_symbol: list[dict[str, Any]] = []
    rejections: dict[str, int] = {}
    diagnostics = _empty_diagnostics()
    closes: dict[str, tuple[float, float]] = {}
    errors: dict[str, str] = {}
    skipped_before_window = 0

    for index, symbol in enumerate(symbols, start=1):
        try:
            data = symbol_candidates(settings, symbol, strategy=strategy, timeframe=timeframe,
                                     params=params, sim=sim, start_ts=start_ts, end_ts=end_ts)
        except StorageError as exc:
            errors[symbol] = str(exc)
            continue
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not hide the rest
            errors[symbol] = f"{type(exc).__name__}: {exc}"
            continue
        for reason, count in data["rejections"].items():
            rejections[reason] = rejections.get(reason, 0) + int(count)
        _merge_diagnostics(diagnostics, data["gates"])
        if data["candidates"]:
            candidates.extend(data["candidates"])
        if start_ts is not None and data["last_ts"] < start_ts:
            skipped_before_window += 1
        if data["first_close"] is not None and data["last_close"] is not None:
            closes[symbol] = (data["first_close"], data["last_close"])
        per_symbol.append({
            "symbol": symbol, "bars": data["bars"], "signal_bars": data["signal_bars"],
            "entry_signals": data["entry_signals"], "candidates": len(data["candidates"]),
            "incomplete_buckets": data["gaps"],
            "funnel": {step["gate"]: step["passing"] for step in data["gates"]["funnel"]},
        })
        if progress:
            progress({"phase": "backtest", "done": index, "total": len(symbols), "symbol": symbol,
                      "candidates": len(candidates)})

    portfolio = simulate_portfolio(candidates, settings=sim, rejections=rejections)
    benchmark = equal_weight_buy_hold(closes, fee_pct=sim.fee_pct, slippage_pct=sim.slippage_pct)
    metrics = portfolio["metrics"]
    metrics["signal_diagnostics"] = diagnostics
    if benchmark.get("return_pct") is not None:
        metrics["benchmark_return_pct"] = benchmark["return_pct"]
        metrics["excess_return_pct"] = round(metrics["net_return_pct"] - benchmark["return_pct"], 4)
    by_symbol: dict[str, dict[str, Any]] = {}
    for trade in portfolio["trades"]:
        row = by_symbol.setdefault(trade["symbol"], {"trades": 0, "net": 0.0, "wins": 0})
        row["trades"] += 1
        row["net"] = round(row["net"] + trade["net"], 6)
        row["wins"] += 1 if trade["net"] > 0 else 0
    for row in by_symbol.values():
        row["win_rate_pct"] = round(row["wins"] / row["trades"] * 100.0, 2) if row["trades"] else None

    return {
        "strategy": strategy,
        "timeframe": timeframe,
        "params": params.as_dict(),
        "params_label": params.label(),
        "window": {"start_ts": start_ts, "end_ts": end_ts,
                   "start": _iso(start_ts) if start_ts else None, "end": _iso(end_ts) if end_ts else None},
        "symbols_requested": len(symbols),
        "symbols_evaluated": len(per_symbol),
        "metrics": metrics,
        "benchmark": benchmark,
        "per_symbol": sorted(per_symbol, key=lambda row: row["symbol"]),
        "trades_by_symbol": by_symbol,
        "trades": portfolio["trades"],
        "equity_curve": portfolio["equity_curve"],
        "errors": errors,
        "data_version": current_data_version(settings),
        "sim": {"initial_capital": sim.initial_capital, "fee_pct": sim.fee_pct,
                "slippage_pct": sim.slippage_pct, "position_pct": sim.position_pct,
                "max_open_positions": sim.max_open_positions, "max_hold_hours": sim.max_hold_hours},
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "note": "PAPER backtest on stored 1m bars; net of fees and slippage; not a forecast",
    }


def data_window(settings: QuantSettings, symbols: Iterable[str] | None = None) -> Window:
    """First/last stored timestamp across the requested symbols (from parquet metadata rows)."""
    manifest = load_manifest(settings)
    records = manifest.get("symbols") if isinstance(manifest.get("symbols"), dict) else {}
    chosen = [records[symbol] for symbol in (symbols or records) if symbol in records]
    starts = [int(month["first_ts"]) for record in chosen
              for month in (record.get("months") or {}).values() if month.get("first_ts")]
    ends = [int(month["last_ts"]) for record in chosen
            for month in (record.get("months") or {}).values() if month.get("last_ts")]
    if not starts or not ends:
        raise StorageError("no stored months to derive a data window from")
    return Window(min(starts), max(ends) + 60)


def chronological_splits(window: Window, *, train: float = 0.55, validation: float = 0.20) -> dict[str, Window]:
    """Train / validation / test by time. The test slice is the last one and never moves."""
    span = window.end_ts - window.start_ts
    if span <= 0:
        raise ValueError("data window is empty")
    train_end = window.start_ts + int(span * train)
    validation_end = window.start_ts + int(span * (train + validation))
    return {
        "train": Window(window.start_ts, train_end),
        "validation": Window(train_end, validation_end),
        "test": Window(validation_end, window.end_ts),
    }


def walk_forward_windows(window: Window, *, folds: int, test_days: int) -> list[dict[str, Window]]:
    """Rolling windows where each test slice is followed by nothing it has already seen.

    The training window only warms indicators; parameters are never fitted here.
    """
    if folds < 2:
        raise ValueError("walk-forward needs at least 2 folds")
    if test_days < 1:
        raise ValueError("test_days must be at least 1")
    step = test_days * 86400
    total = window.end_ts - window.start_ts
    if total < folds * step:
        raise ValueError(f"window is {total / 86400:.1f} days but {folds} folds x {test_days} days need "
                         f"{folds * test_days} days")
    first_test = window.end_ts - folds * step
    out = []
    for index in range(folds):
        test = Window(first_test + index * step, first_test + (index + 1) * step)
        out.append({"fold": index + 1, "train": Window(window.start_ts, test.start_ts), "test": test})
    return out


def aggregate_folds(folds: list[dict[str, Any]]) -> dict[str, Any]:
    if not folds:
        return {"folds": 0, "note": "no folds evaluated"}
    returns = [float(fold["metrics"]["net_return_pct"]) for fold in folds]
    factors = [fold["metrics"]["profit_factor"] for fold in folds
               if isinstance(fold["metrics"].get("profit_factor"), (int, float))]
    trades = [int(fold["metrics"]["trades"]) for fold in folds]
    positive = sum(1 for value in returns if value > 0)
    return {
        "folds": len(folds),
        "positive_folds": positive,
        "positive_fold_pct": round(positive / len(folds) * 100.0, 2),
        "net_return_pct_sum": round(float(sum(returns)), 4),
        "net_return_pct_median": round(float(np.median(returns)), 4),
        "net_return_pct_min": round(min(returns), 4),
        "trades": sum(trades),
        "profit_factor_mean": round(float(np.mean(factors)), 4) if factors else None,
        "max_drawdown_pct": max(fold["metrics"]["max_drawdown_pct"] for fold in folds),
        # The gate compares risk, so folds report the honest (MAE-aware) figure as
        # well as the realized one; a fold with open losers must not look flat.
        "max_effective_drawdown_pct": max(
            float(fold["metrics"].get("effective_drawdown_pct")
                  or fold["metrics"].get("max_drawdown_pct") or 0.0) for fold in folds),
        "benchmark_return_pct_sum": round(sum(float(fold.get("benchmark", {}).get("return_pct") or 0.0)
                                               for fold in folds), 4),
        "note": "each fold is scored only on its own unseen test slice",
    }


def walk_forward(settings: QuantSettings, symbols: Iterable[str], *, strategy: str, timeframe: str,
                 params: QuantParams, folds: int | None = None, test_days: int | None = None,
                 sim: SimSettings | None = None, window: Window | None = None,
                 progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    symbols = list(symbols)
    folds = int(folds or settings.walk_forward.folds)
    test_days = int(test_days or settings.walk_forward.test_days)
    sim = sim or simulate_settings(settings)
    # `window` lets the acceptance loop stop the folds before the final holdout,
    # so the gates never score a slice that is later reported as untouched.
    window = window or data_window(settings, symbols)
    plans = walk_forward_windows(window, folds=folds, test_days=test_days)
    records: list[dict[str, Any]] = []
    for plan in plans:
        result = run_backtest(settings, symbols, strategy=strategy, timeframe=timeframe, params=params,
                              sim=sim, start_ts=plan["test"].start_ts, end_ts=plan["test"].end_ts,
                              progress=progress)
        records.append({"fold": plan["fold"], "test": plan["test"].iso(), "train": plan["train"].iso(),
                        "metrics": result["metrics"], "benchmark": result["benchmark"],
                        "trades": len(result["trades"])})
    return {"strategy": strategy, "timeframe": timeframe, "params": params.as_dict(),
            "params_label": params.label(), "window": window.iso(), "folds": records,
            "aggregate": aggregate_folds(records), "data_version": current_data_version(settings),
            "note": "walk-forward on unseen test slices; historical evidence, not a forecast"}
