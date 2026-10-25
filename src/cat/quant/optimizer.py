"""Bounded parameter search with Optuna.

The search only touches strategy fields already listed in ``PARAM_BOUNDS``; risk
limits, sizing and capital stay owned by ``config.yaml``. Every trial is scored
on the **train/search window only**, so the validation slice the loop later
gates on has never been read by the search. (Ranking trials on validation and
then requiring validation to be positive is selection bias: the candidate's
score is the maximum of N draws from the same window it is judged on.)

Duplicate suggestions are reused instead of rerun, and hopeless searches stop
early so the loop does not burn its time budget on a dead hypothesis.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import optuna

from .engine import Window, chronological_splits, data_window, run_backtest, simulate_settings
from .features import PARAM_BOUNDS, QuantParams, params_from_dict
from .settings import QuantSettings

optuna.logging.set_verbosity(optuna.logging.WARNING)

# Fields that plausibly interact with each strategy family. Anything outside
# this map is left at its default, which keeps the search small and explainable.
SPACES: dict[str, tuple[str, ...]] = {
    "ema_trend": ("ema_fast", "ema_slow", "slope_lookback", "slope_threshold",
                  "atr_stop_mult", "atr_target_mult"),
    "trend_pullback": ("ema_slow", "rsi_pullback_low", "rsi_pullback_high", "rsi_exit",
                       "atr_stop_mult", "atr_target_mult"),
    "momentum": ("slope_lookback", "slope_threshold", "ema_slow", "atr_stop_mult", "atr_target_mult"),
    "mean_reversion": ("ema_slow", "rsi_pullback_low", "rsi_pullback_high", "atr_stop_mult",
                       "atr_target_mult"),
    "breakout": ("breakout_lookback", "exit_lookback", "volume_min_ratio", "atr_stop_mult",
                 "atr_target_mult"),
    "buy_hold": (),
}


@dataclass
class TrialResult:
    number: int
    params: dict[str, Any]
    score: float
    search: dict[str, Any]
    status: str = "completed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number, "params": self.params, "score": round(self.score, 4),
            "status": self.status,
            "search": {key: self.search.get(key) for key in ("trades", "net_return_pct",
                                                             "profit_factor",
                                                             "effective_drawdown_pct")},
        }


def _params_key(params: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(params, sort_keys=True).encode("utf-8")).hexdigest()


def _suggest(trial: optuna.Trial, name: str) -> Any:
    low, high = PARAM_BOUNDS[name]
    if name in {"ema_fast", "ema_slow", "rsi_period", "atr_period", "slope_lookback",
                "volume_avg_bars", "rsi_pullback_low", "rsi_pullback_high", "rsi_exit",
                "breakout_lookback", "exit_lookback"}:
        return trial.suggest_int(name, int(low), int(high))
    return trial.suggest_float(name, float(low), float(high))


def optimize(settings: QuantSettings, symbols: Iterable[str], *, strategy: str, timeframe: str,
             base: QuantParams | None = None, trials: int | None = None,
             seed: int = 42, progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Search the strategy's audited space; ranking is validation net return with a trade floor."""
    symbols = list(symbols)
    base = base or QuantParams()
    trials = int(trials or settings.loop.trials_per_experiment)
    window = data_window(settings, symbols)
    splits = chronological_splits(window, train=settings.walk_forward.train_fraction,
                                  validation=settings.walk_forward.validation_fraction)
    fields = SPACES.get(strategy, ())
    sim = simulate_settings(settings)
    min_trades = max(5, settings.loop.min_trades // 2)
    cache: dict[str, float] = {}
    records: list[TrialResult] = []
    recent: list[float] = []
    started = time.monotonic()
    stop_reason = ""

    def score_window(params: QuantParams, split: Window) -> dict[str, Any]:
        result = run_backtest(settings, symbols, strategy=strategy, timeframe=timeframe, params=params,
                              sim=sim, start_ts=split.start_ts, end_ts=split.end_ts)
        return result["metrics"]

    def objective(trial: optuna.Trial) -> float:
        candidate = {name: _suggest(trial, name) for name in fields}
        key = _params_key(candidate)
        if key in cache:
            return cache[key]
        try:
            params = params_from_dict(base, candidate)
        except ValueError as exc:
            records.append(TrialResult(trial.number, candidate, -1e9, {}, status=f"invalid: {exc}"))
            raise optuna.TrialPruned() from exc
        search_metrics = score_window(params, splits["train"])
        trades = int(search_metrics.get("trades") or 0)
        if trades < min_trades:
            # Absence of evidence must never win: a zero-trade variant is penalized.
            score = -100.0 + trades * 0.1
        else:
            score = float(search_metrics.get("net_return_pct") or 0.0)
            drawdown = float(search_metrics.get("effective_drawdown_pct") or 0.0)
            score -= drawdown * 0.25
        cache[key] = score
        records.append(TrialResult(trial.number, candidate, score, search_metrics))
        recent.append(score)
        if progress:
            progress({"phase": "trial", "trial": trial.number + 1, "trials": trials,
                      "score": round(score, 3), "trades": trades,
                      "params": candidate})
        # Early stop: the last three trials all showed no evidence at all.
        if len(recent) >= 3 and all(value <= -99 for value in recent[-3:]):
            stop_reason = "three consecutive trials without enough trades"
            trial.study.stop()
        return score

    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=seed, n_startup_trials=4))
    study.optimize(objective, n_trials=trials, catch=(Exception,))
    completed = [record for record in records if record.status == "completed"]
    best = max(completed, key=lambda record: record.score) if completed else None
    best_params = params_from_dict(base, best.params).as_dict() if best else base.as_dict()
    return {
        "strategy": strategy, "timeframe": timeframe, "fields": list(fields),
        "trials": [record.as_dict() for record in records],
        "best": {"params": best_params, "score": round(best.score, 4) if best else None},
        "stop_reason": stop_reason or ("trial budget reached" if len(records) >= trials else "search ended"),
        "window": {"train": splits["train"].iso(), "validation": splits["validation"].iso()},
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "note": "trials ranked on the train window only; validation and test are never read by this search",
    }
