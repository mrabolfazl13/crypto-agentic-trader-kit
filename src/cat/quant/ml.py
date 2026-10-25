"""LightGBM experiment: predict a forward return, then trade it with the same execution model.

Discipline enforced here:

* features use only data up to the signal bar's close;
* the label is the forward return over ``horizon_hours``, so the last horizon of
  every segment is dropped (that is the purge — labels never cross a boundary);
* the model is trained on the first 80% of train, early-stops and picks the
  probability threshold on the last 20% of train, and the outer validation plus
  the test slice are scored only after every choice is frozen;
* the target is net-of-cost return, never raw accuracy: the threshold that wins
  is the one with the best validation net after fees and slippage.

LightGBM is imported lazily so the core engine and its tests stay dependency-light.
"""

from __future__ import annotations

import gc
import time
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .engine import (Window, aggregate_folds, data_window, load_bars, run_backtest,
                     simulate_settings, walk_forward_windows)
from .features import QuantParams, TIMEFRAME_SECONDS, add_indicators, resample
from .settings import QuantSettings
from .sim import SimSettings, SignalTrade, equal_weight_buy_hold, generate_trades, simulate_portfolio
from .storage import current_data_version

FEATURES = (
    "ret_1", "ret_3", "ret_6", "ret_12", "ema_fast_gap", "ema_slow_gap", "rsi_norm",
    "atr_pct", "volume_ratio", "momentum", "vol_24", "hour_sin", "hour_cos", "trend",
)


@dataclass(frozen=True)
class MLDataset:
    frame: pd.DataFrame
    label_threshold: float
    horizon_bars: int
    features: tuple[str, ...] = FEATURES


def _cost_threshold(settings: QuantSettings) -> float:
    """A move must clear both legs of fees and slippage to count as a positive label."""
    return (settings.backtest.fee_pct * 2 + settings.backtest.slippage_pct * 2) / 100.0


def dataset_for_segment(settings: QuantSettings, symbol: str, *, timeframe: str, params: QuantParams,
                        start_ts: int | None = None, end_ts: int | None = None,
                        horizon_hours: int | None = None,
                        with_labels: bool = True) -> MLDataset:
    step = TIMEFRAME_SECONDS[timeframe]
    horizon_bars = max(1, int(round((horizon_hours or settings.ml.horizon_hours) * 3600 / step)))
    warmup = step * params.warmup_bars() + step * max(24, horizon_bars)
    load_start = int(start_ts) - warmup if start_ts is not None else None
    bars = load_bars(settings, symbol, start_ts=load_start, end_ts=end_ts)
    frame = add_indicators(resample(bars, timeframe), params)
    close = frame["close"].astype("float64")
    frame["atr_pct"] = (frame["atr"] / close).where(frame["atr"] > 0)
    for span in (1, 3, 6, 12):
        frame[f"ret_{span}"] = close.pct_change(span)
    frame["ema_fast_gap"] = frame["ema_fast"] / close - 1.0
    frame["ema_slow_gap"] = frame["ema_slow"] / close - 1.0
    frame["rsi_norm"] = frame["rsi"] / 100.0
    frame["vol_24"] = frame["ret_1"].rolling(24, min_periods=12).std()
    hours = (frame["bar_end"] // 3600) % 24
    frame["hour_sin"] = np.sin(2 * np.pi * hours / 24.0)
    frame["hour_cos"] = np.cos(2 * np.pi * hours / 24.0)
    frame["trend"] = (frame["ema_fast"] > frame["ema_slow"]).astype("int8")

    threshold = _cost_threshold(settings)
    frame["label"] = (close.shift(-horizon_bars) / close - 1.0 > threshold).astype("int8")
    valid = frame["complete"].fillna(False).astype(bool)
    valid &= frame[list(FEATURES)].notna().all(axis=1)
    if start_ts is not None:
        valid &= frame["bar_end"] >= int(start_ts)
    if end_ts is not None:
        # Purge: a label needs horizon_bars into the future, and it must stay inside the segment.
        valid &= frame["bar_end"] + horizon_bars * step <= int(end_ts)
    if with_labels:
        valid &= frame["label"].notna()
    else:
        # Prediction needs the bar itself, not its future.
        valid &= frame["bar_end"] + step <= int(end_ts) if end_ts is not None else True
    return MLDataset(frame.loc[valid].reset_index(drop=True), threshold, horizon_bars)


def build_training_set(settings: QuantSettings, symbols: Iterable[str], *, timeframe: str,
                       params: QuantParams, window: Window, horizon_hours: int | None = None,
                       progress=None) -> pd.DataFrame:
    frames = []
    symbols = list(symbols)
    for index, symbol in enumerate(symbols, start=1):
        try:
            data = dataset_for_segment(settings, symbol, timeframe=timeframe, params=params,
                                       start_ts=window.start_ts, end_ts=window.end_ts,
                                       horizon_hours=horizon_hours)
        except Exception as exc:  # noqa: BLE001 - report and keep the rest
            if progress:
                progress({"phase": "ml_data", "symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if data.frame.empty:
            continue
        part = data.frame[["bar_end", "label", *FEATURES]].copy()
        part["symbol"] = symbol
        rows = len(part)
        frames.append(part)
        del data, part
        if progress:
            progress({"phase": "ml_data", "done": index, "total": len(symbols), "symbol": symbol,
                      "rows": rows})
        # The host is memory-tight: return each symbol's frame before the next load.
        gc.collect()
    if not frames:
        raise ValueError("no labelled rows survived validation; refusing to train on nothing")
    combined = pd.concat(frames, ignore_index=True)
    frames.clear()
    gc.collect()
    return combined


def train_model(train: pd.DataFrame, validation: pd.DataFrame, *, seed: int = 42,
                horizon_bars: int = 4) -> dict[str, Any]:
    """Binary LightGBM with early stopping; metrics are on the validation slice only."""
    import lightgbm as lgb

    if len(train) < 500 or train["label"].nunique() < 2:
        raise ValueError(f"training set too weak: {len(train)} rows, labels {train['label'].unique()}")
    model = lgb.LGBMClassifier(
        objective="binary", n_estimators=400, learning_rate=0.05, num_leaves=31,
        min_child_samples=50, subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
        reg_lambda=1.0, random_state=seed, n_jobs=1, verbosity=-1,
    )
    model.fit(train[list(FEATURES)], train["label"],
              eval_set=[(validation[list(FEATURES)], validation["label"])],
              eval_metric="auc", callbacks=[lgb.early_stopping(50, verbose=False)])
    probabilities = model.predict_proba(validation[list(FEATURES)])[:, 1]
    labels = validation["label"].to_numpy(dtype=int)
    from sklearn.metrics import roc_auc_score, log_loss
    metrics = {
        "train_rows": int(len(train)), "validation_rows": int(len(validation)),
        "positive_rate": round(float(train["label"].mean()), 4),
        "validation_auc": round(float(roc_auc_score(labels, probabilities)), 4)
        if len(set(labels)) > 1 else None,
        "validation_logloss": round(float(log_loss(labels, probabilities, labels=[0, 1])), 4),
        "best_iteration": int(getattr(model, "best_iteration_", 0) or model.n_estimators),
        "feature_importance": dict(sorted(
            zip(FEATURES, [round(float(value), 2) for value in model.feature_importances_]),
            key=lambda item: item[1], reverse=True)[:8]),
        "note": "classification metrics only; the trade decision is made on net return",
    }
    return {"model": model, "metrics": metrics}


def predict_frame(settings: QuantSettings, symbol: str, *, timeframe: str, params: QuantParams,
                  model: Any, threshold: float, start_ts: int | None = None,
                  end_ts: int | None = None, horizon_hours: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (signal frame for the simulator, raw 1m bars) for one symbol."""
    data = dataset_for_segment(settings, symbol, timeframe=timeframe, params=params, start_ts=start_ts,
                               end_ts=end_ts, horizon_hours=horizon_hours, with_labels=False)
    frame = data.frame.copy()
    if frame.empty:
        return frame, pd.DataFrame()
    frame["probability"] = model.predict_proba(frame[list(FEATURES)])[:, 1]
    frame["entry"] = (frame["probability"] >= threshold) & (frame["ema_fast"] > frame["ema_slow"])
    frame["exit"] = ((frame["probability"] < max(0.5, threshold - 0.10))
                     | (frame["ema_fast"] < frame["ema_slow"]))
    frame["complete"] = True
    warmup = TIMEFRAME_SECONDS[timeframe] * params.warmup_bars() * 2
    bars = load_bars(settings, symbol, start_ts=(int(start_ts) - warmup) if start_ts else None,
                     end_ts=end_ts)
    return frame, bars


def ml_backtest(settings: QuantSettings, symbols: Iterable[str], *, timeframe: str, params: QuantParams,
                model: Any, threshold: float, sim: SimSettings | None = None,
                start_ts: int | None = None, end_ts: int | None = None,
                horizon_hours: int | None = None, progress=None) -> dict[str, Any]:
    sim = sim or simulate_settings(settings)
    candidates: list[SignalTrade] = []
    rejections: dict[str, int] = {}
    closes: dict[str, tuple[float, float]] = {}
    errors: dict[str, str] = {}
    symbols = list(symbols)
    for index, symbol in enumerate(symbols, start=1):
        try:
            signals, bars = predict_frame(settings, symbol, timeframe=timeframe, params=params,
                                          model=model, threshold=threshold, start_ts=start_ts,
                                          end_ts=end_ts, horizon_hours=horizon_hours)
            if bars.empty or signals.empty:
                continue
            trades, rejected = generate_trades(bars, signals, symbol=symbol, strategy="ema_trend",
                                               params=params, settings=sim, entry_after_ts=start_ts)
        except Exception as exc:  # noqa: BLE001
            errors[symbol] = f"{type(exc).__name__}: {exc}"
            continue
        for reason, count in rejected.items():
            rejections[reason] = rejections.get(reason, 0) + int(count)
        candidates.extend(trades)
        window = bars[bars["ts"] >= (start_ts or 0)]
        if end_ts is not None:
            window = window[window["ts"] < end_ts]
        if not window.empty:
            closes[symbol] = (float(window["close"].iloc[0]), float(window["close"].iloc[-1]))
        del signals, bars
        gc.collect()
        if progress:
            progress({"phase": "ml_backtest", "done": index, "total": len(symbols), "symbol": symbol})
    result = simulate_portfolio(candidates, settings=sim, rejections=rejections)
    benchmark = equal_weight_buy_hold(closes, fee_pct=sim.fee_pct, slippage_pct=sim.slippage_pct)
    metrics = result["metrics"]
    if benchmark.get("return_pct") is not None:
        metrics["benchmark_return_pct"] = benchmark["return_pct"]
        metrics["excess_return_pct"] = round(metrics["net_return_pct"] - benchmark["return_pct"], 4)
    return {"metrics": metrics, "trades": result["trades"], "benchmark": benchmark,
            "errors": errors, "threshold": threshold}


def _inner_split(window: Window) -> tuple[Window, Window]:
    """Carve an inner validation slice out of train for early stopping/threshold.

    The outer validation slice must stay unread until the loop's gate, otherwise
    choosing a threshold on it turns the gate's "positive validation" into a
    best-of-four selection.
    """
    span = window.end_ts - window.start_ts
    if span < 2 * 86400:
        raise ValueError("train window is too short to carve an inner validation slice")
    split = window.start_ts + int(span * 0.8)
    return Window(window.start_ts, split), Window(split, window.end_ts)


def choose_threshold(settings: QuantSettings, symbols: Iterable[str], *, timeframe: str,
                     params: QuantParams, model: Any, thresholds: Iterable[float],
                     window: Window, sim: SimSettings | None = None) -> dict[str, Any]:
    """Pick the threshold with the best net return on the **inner** validation slice."""
    results = []
    for threshold in thresholds:
        result = ml_backtest(settings, symbols, timeframe=timeframe, params=params, model=model,
                             threshold=float(threshold), sim=sim, start_ts=window.start_ts,
                             end_ts=window.end_ts)
        metrics = result["metrics"]
        score = float(metrics.get("net_return_pct") or 0.0) if metrics["trades"] >= 5 else -100.0
        results.append({"threshold": float(threshold), "score": round(score, 4),
                        "trades": metrics["trades"], "net_return_pct": metrics.get("net_return_pct")})
    best = max(results, key=lambda row: row["score"])
    return {"thresholds": results, "best": best}


def run_ml_experiment(settings: QuantSettings, symbols_search: list[str], symbols_full: list[str], *,
                      timeframe: str, train: Window, validation: Window, test: Window,
                      params: QuantParams | None = None, horizon_hours: int | None = None,
                      seed: int = 42, progress=None) -> dict[str, Any]:
    """Train once, choose the threshold on an inner train slice, score the hold-out once."""
    started = time.monotonic()
    params = params or QuantParams()
    inner_train, inner_validation = _inner_split(train)
    train_rows = build_training_set(settings, symbols_search, timeframe=timeframe, params=params,
                                    window=inner_train, horizon_hours=horizon_hours, progress=progress)
    validation_rows = build_training_set(settings, symbols_search, timeframe=timeframe, params=params,
                                         window=inner_validation, horizon_hours=horizon_hours)
    trained = train_model(train_rows, validation_rows, seed=seed)
    threshold = choose_threshold(settings, symbols_search, timeframe=timeframe, params=params,
                                 model=trained["model"], thresholds=(0.50, 0.55, 0.60, 0.65),
                                 window=inner_validation)
    selected = float(threshold["best"]["threshold"])
    validation_result = ml_backtest(settings, symbols_full, timeframe=timeframe, params=params,
                                    model=trained["model"], threshold=selected,
                                    start_ts=validation.start_ts, end_ts=validation.end_ts)
    test_result = ml_backtest(settings, symbols_full, timeframe=timeframe, params=params,
                              model=trained["model"], threshold=selected,
                              start_ts=test.start_ts, end_ts=test.end_ts)
    return {
        "kind": "ml_lgbm", "timeframe": timeframe, "params": params.as_dict(),
        "model_metrics": trained["metrics"], "threshold": threshold,
        "selected_threshold": selected, "validation": validation_result,
        "test": test_result, "windows": {"train": train.iso(), "validation": validation.iso(),
                                          "test": test.iso()},
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "note": "threshold chosen on the inner train slice; test scored once; features are past-only",
    }


def _purged_windows(pre_test: Window, test: Window, step: int, horizon_bars: int) -> tuple[Window, Window]:
    """Split the pre-test history 80/20 and keep the label horizon away from the test slice."""
    inner_end = test.start_ts - horizon_bars * step
    if inner_end <= pre_test.start_ts:
        raise ValueError("pre-test history is shorter than one label horizon; refusing to train")
    split = pre_test.start_ts + int((inner_end - pre_test.start_ts) * 0.8)
    split -= split % step
    return Window(pre_test.start_ts, split), Window(split, inner_end)


def walk_forward_ml(settings: QuantSettings, symbols_train: Iterable[str], symbols_score: Iterable[str],
                    *, timeframe: str, params: QuantParams | None = None, folds: int | None = None,
                    test_days: int | None = None, horizon_hours: int | None = None, seed: int = 42,
                    window: Window | None = None, progress=None) -> dict[str, Any]:
    """Retrain on every fold: nothing after a fold's test start is used to fit it.

    Each fold trains on the 80% before its test slice, early-stops and picks the
    probability threshold on the remaining 20%, then scores only the test slice.
    """
    params = params or QuantParams()
    folds = int(folds or settings.walk_forward.folds)
    test_days = int(test_days or settings.walk_forward.test_days)
    horizon_hours = int(horizon_hours or settings.ml.horizon_hours)
    step = TIMEFRAME_SECONDS[timeframe]
    horizon_bars = max(1, int(round(horizon_hours * 3600 / step)))
    symbols_train = list(symbols_train)
    symbols_score = list(symbols_score)
    window = window or data_window(settings, sorted(set(symbols_train) | set(symbols_score)))
    plans = walk_forward_windows(window, folds=folds, test_days=test_days)
    records: list[dict[str, Any]] = []
    for plan in plans:
        if progress:
            progress({"phase": "ml_fold", "fold": plan["fold"], "of": folds})
        inner_train, inner_validation = _purged_windows(plan["train"], plan["test"], step, horizon_bars)
        train_rows = build_training_set(settings, symbols_train, timeframe=timeframe, params=params,
                                        window=inner_train, horizon_hours=horizon_hours)
        validation_rows = build_training_set(settings, symbols_train, timeframe=timeframe,
                                             params=params, window=inner_validation,
                                             horizon_hours=horizon_hours)
        trained = train_model(train_rows, validation_rows, seed=seed, horizon_bars=horizon_bars)
        picked = choose_threshold(settings, symbols_train[:10], timeframe=timeframe, params=params,
                                  model=trained["model"], thresholds=(0.50, 0.55, 0.60),
                                  window=inner_validation)
        threshold = float(picked["best"]["threshold"])
        scored = ml_backtest(settings, symbols_score, timeframe=timeframe, params=params,
                             model=trained["model"], threshold=threshold,
                             start_ts=plan["test"].start_ts, end_ts=plan["test"].end_ts,
                             horizon_hours=horizon_hours)
        records.append({
            "fold": plan["fold"], "train": inner_train.iso(), "validation": inner_validation.iso(),
            "test": plan["test"].iso(), "metrics": scored["metrics"],
            "benchmark": scored["benchmark"], "trades": len(scored["trades"]),
            "threshold": threshold, "model": trained["metrics"],
            "errors": scored["errors"],
        })
        del train_rows, validation_rows, trained, picked, scored
        gc.collect()
    return {"kind": "ml_lgbm", "timeframe": timeframe, "params": params.as_dict(),
            "window": window.iso(), "folds": records, "aggregate": aggregate_folds(records),
            "data_version": current_data_version(settings),
            "note": "every fold is refit on earlier data only; the fold test slice is never trained on"}


def run_ml_walk_forward(settings: QuantSettings, symbols_train: list[str], symbols_score: list[str], *,
                        timeframe: str, train: Window, validation: Window, test: Window,
                        params: QuantParams | None = None, horizon_hours: int | None = None,
                        seed: int = 42, progress=None) -> dict[str, Any]:
    """Loop-shaped ML experiment: hold-out scored once, folds kept before the holdout."""
    started = time.monotonic()
    params = params or QuantParams()
    horizon_hours = int(horizon_hours or settings.ml.horizon_hours)
    inner_train, inner_validation = _inner_split(train)
    train_rows = build_training_set(settings, symbols_train, timeframe=timeframe, params=params,
                                    window=inner_train, horizon_hours=horizon_hours, progress=progress)
    validation_rows = build_training_set(settings, symbols_train, timeframe=timeframe, params=params,
                                         window=inner_validation, horizon_hours=horizon_hours)
    trained = train_model(train_rows, validation_rows, seed=seed)
    picked = choose_threshold(settings, symbols_train[:10], timeframe=timeframe, params=params,
                              model=trained["model"], thresholds=(0.50, 0.55, 0.60, 0.65),
                              window=inner_validation)
    threshold = float(picked["best"]["threshold"])
    validation_result = ml_backtest(settings, symbols_score, timeframe=timeframe, params=params,
                                    model=trained["model"], threshold=threshold,
                                    start_ts=validation.start_ts, end_ts=validation.end_ts,
                                    horizon_hours=horizon_hours)
    folds = walk_forward_ml(settings, symbols_train, symbols_score, timeframe=timeframe, params=params,
                            horizon_hours=horizon_hours, seed=seed,
                            window=Window(train.start_ts, test.start_ts), progress=progress)
    # Hold-out is evaluated after every choice above has been frozen.
    test_result = ml_backtest(settings, symbols_score, timeframe=timeframe, params=params,
                              model=trained["model"], threshold=threshold,
                              start_ts=test.start_ts, end_ts=test.end_ts,
                              horizon_hours=horizon_hours)
    return {
        "kind": "ml_lgbm", "timeframe": timeframe, "params": params.as_dict(),
        "model_metrics": trained["metrics"], "threshold": picked,
        "selected_threshold": threshold, "validation": validation_result,
        "walk_forward": folds, "test": test_result,
        "windows": {"train": train.iso(), "validation": validation.iso(), "test": test.iso()},
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "note": "threshold on the inner train slice; folds refit before the holdout; test scored once",
    }
