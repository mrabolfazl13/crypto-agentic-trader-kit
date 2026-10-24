"""Strategy Lab: measure variants out-of-sample and keep the champion unless data wins.

Public surface for the API/CLI layer: `split_bars`, `evaluate`, `walk_forward`,
`grid_search`, `acceptance`, `run_research`, `PARAM_SPACE_HINTS`.

Every number comes from `backtest.run_backtest`, a PAPER simulation on closed candles: this
module has no order authority, cannot place or size an order and treats `config.limits` as
read-only. Out-of-sample figures are historical evidence, never a forecast, a win-rate
promise or a profit guarantee. A candidate replaces the champion only when `acceptance()`
passes; a rejection with stored reasons is the normal outcome.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from itertools import product
from typing import Any

from .backtest import run_backtest
from .config import Config, RiskLimits
from .data import Kline, SymbolFilter, seconds_per_bar
from .strategies import StrategyParams, default_params

MIN_SEGMENT_BARS = 120
MIN_FOLD_TEST_BARS = 60
MAX_GRID_EVALS = 60
DD_TOLERANCE_PP = 1.0
OOS_NOTE = "out-of-sample figures only: historical simulation with fees and slippage, not a forecast"
RISK_EVIDENCE_KEYS = ("sized_positions_recorded", "drawdown_within_config_limit",
                      "risk_limits_untouched", "grid_targets_strategy_fields_only")

# Documented bounds validated up front; an out-of-bounds grid value is refused, never clamped.
PARAM_SPACE_HINTS: dict[str, tuple[float, float]] = {
    "ema_fast": (5.0, 60.0), "ema_slow": (20.0, 240.0), "rsi_period": (5.0, 40.0),
    "atr_period": (5.0, 40.0), "atr_stop_mult": (0.5, 4.0), "atr_target_mult": (1.0, 8.0),
    "min_bars": (50.0, 300.0), "vol_lookback": (10.0, 120.0), "slope_lookback": (10.0, 120.0),
    "volume_avg_bars": (10.0, 120.0), "rsi_bullish_low": (20.0, 60.0), "rsi_bullish_high": (45.0, 85.0),
    "rsi_stretched": (60.0, 95.0), "rsi_weak": (15.0, 50.0), "slope_threshold": (0.0, 1.0),
    "vol_threshold": (0.0, 10.0), "atr_pct_threshold": (0.1, 10.0), "volume_collapse_ratio": (0.0, 1.0),
    "volume_weak_ratio": (0.0, 2.0),
}

_IDENTITY_FIELDS = ("name", "version", "note")
# Every risk knob belongs to config.yaml and the risk engine; none of them is ever a grid target.
_RISK_FORBIDDEN = set(RiskLimits.__dataclass_fields__) | {"allocated_capital", "capital", "equity"}


def _family(strategy: str | None) -> tuple[Any, str]:
    """Resolve a lab entry-trigger family by name and the label prefix that keeps it visible in every run."""
    if strategy is None or str(strategy).strip() == "":
        return None, ""
    from .strategy_lab import LAB_STRATEGIES

    name = str(strategy).strip()
    if name not in LAB_STRATEGIES:
        raise ValueError(f"unknown strategy family {name!r}: choose from {', '.join(sorted(LAB_STRATEGIES))}")
    return LAB_STRATEGIES[name], f"{name}|"


def _iso(ts: int | float) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="minutes")


def _tunable(base: StrategyParams) -> dict[str, Any]:
    return {key: value for key, value in base.tuning().items() if key not in _IDENTITY_FIELDS}


def _coerce(key: str, value: Any, current: Any) -> Any:
    if isinstance(current, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key} expects true/false, got {value!r}")
        return value
    if isinstance(current, (int, float)):
        try:
            return type(current)(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} expects a number, got {value!r}") from exc
    return str(value)


def _numbers(metrics: dict[str, Any], *keys: str) -> bool:
    return all(isinstance(metrics.get(k), (int, float)) and not isinstance(metrics.get(k), bool) for k in keys)


def bar_spacings(bars: list[Kline]) -> list[int]:
    return [int(bars[i].open_time) - int(bars[i - 1].open_time) for i in range(1, len(bars))]


def _segment_report(bars: list[Kline], step: int | None) -> dict[str, Any]:
    if not bars:
        return {"bars": 0, "start": None, "end": None, "gap_free": False}
    return {"bars": len(bars), "start": _iso(bars[0].open_time), "end": _iso(bars[-1].open_time),
            "gap_free": bool(step) and all(gap == step for gap in bar_spacings(bars)),
            "first_open_time": int(bars[0].open_time), "last_open_time": int(bars[-1].open_time)}


@dataclass(frozen=True)
class Split:
    """Chronological, non-overlapping segments: train, then validation, then the untouched test hold-out."""

    train: list[Kline]
    validation: list[Kline]
    test: list[Kline]
    step: int | None = None

    def as_dict(self) -> dict[str, Any]:
        parts = (self.train, self.validation, self.test) + ((),)
        out: dict[str, Any] = {}
        for name, segment, following in zip(("train", "validation", "test"), parts, parts[1:]):
            report = _segment_report(segment, self.step)
            report["contiguous_with_next"] = bool(following and self.step and int(following[0].open_time)
                                                  - int(segment[-1].open_time) == self.step)
            out[name] = report
        return out


def split_bars(bars: list[Kline], *, train: float = 0.6, validation: float = 0.2,
               min_bars: int = MIN_SEGMENT_BARS) -> Split:
    if not isinstance(bars, list) or not bars:
        raise ValueError("no bars to split: research needs a closed-candle history")
    times = [int(bar.open_time) for bar in bars]
    if any(times[i] <= times[i - 1] for i in range(1, len(times))):
        raise ValueError("bars must be strictly chronological with unique timestamps")
    if not (0.0 < train < 1.0 and 0.0 < validation < 1.0 and train + validation < 1.0):
        raise ValueError("split fractions must keep all of train/validation/test, with train+validation < 1")
    if min_bars < 1:
        raise ValueError("min_bars must be at least 1")

    gaps = Counter(gap for gap in bar_spacings(bars) if gap > 0)
    step = gaps.most_common(1)[0][0] if gaps else None
    total = len(bars)
    train_end, val_end = int(total * train), int(total * (train + validation))
    segments = (bars[:train_end], bars[train_end:val_end], bars[val_end:])
    sizes = tuple(len(segment) for segment in segments)
    if any(size < min_bars for size in sizes):
        raise ValueError(f"insufficient data for honest evaluation: every segment needs at least {min_bars} bars, "
                         f"got train={sizes[0]} validation={sizes[1]} test={sizes[2]} of {total} total")
    return Split(train=segments[0], validation=segments[1], test=segments[2], step=step)


def _sample_series(points: list[tuple[int, float]], max_points: int = 90) -> list[list[float]]:
    """Down-sample a mark-to-market series for storage; first and last points always survive."""
    if len(points) <= max_points:
        return [[float(ts), round(float(value), 4)] for ts, value in points]
    stride = (len(points) - 1) / (max_points - 1)
    picked: list[list[float]] = []
    last_index = -1
    for step_index in range(max_points):
        index = min(len(points) - 1, int(round(step_index * stride)))
        if index == last_index:
            continue
        picked.append([float(points[index][0]), round(float(points[index][1]), 4)])
        last_index = index
    return picked


def evaluate(config: Config, symbol: str, *, bars: list[Kline], higher_bars: list[Kline],
             filters: SymbolFilter, params: StrategyParams, warmup: int = 80,
             assumed_spread_pct: float = 0.05, timeframe: str | None = None,
             strategy: str | None = None) -> dict[str, Any]:
    """One engine run. `strategy` swaps only the entry-trigger family from `strategy_lab`;
    sizing, risk gate and exits stay the audited ones, and the family is kept in the label."""
    if len(bars) <= warmup:
        raise ValueError(f"evaluation window has {len(bars)} bars but warmup={warmup}: no bar could be decided")
    propose_fn, prefix = _family(strategy)
    result = run_backtest(config, symbol, bars=bars, higher_bars=higher_bars, filters=filters, timeframe=timeframe,
                          warmup=warmup, assumed_spread_pct=assumed_spread_pct, params=params,
                          propose_fn=propose_fn)
    initial = float(config.allocated_capital or 1000.0)
    equity = [(int(ts), float(value)) for ts, value in (result.equity_curve or [])]
    equity_pct = [(ts, value / initial * 100.0) for ts, value in equity] if equity and initial > 0 else []
    benchmark_pct: list[tuple[int, float]] = []
    anchor = float(bars[warmup].close)
    if equity_pct and anchor > 0:
        closes_by_ts = {int(bar.open_time): float(bar.close) for bar in bars}
        benchmark_pct = [(ts, closes_by_ts[ts] / anchor * 100.0) for ts, _value in equity_pct
                         if ts in closes_by_ts and closes_by_ts[ts] > 0]
    metrics = dict(result.metrics or {})
    metrics.setdefault("trades", len(result.trades))
    return {"metrics": metrics, "buy_hold_pct": result.buy_hold_pct, "trades": len(result.trades),
            "rejected": dict(result.rejected or {}), "params_label": prefix + (result.params_label or params.label()),
            "strategy": prefix.rstrip("|"), "start": result.start, "end": result.end, "bars": result.bars,
            "equity_curve": _sample_series(equity_pct), "benchmark_curve": _sample_series(benchmark_pct)}


def aggregate_folds(folds: list[dict[str, Any]]) -> dict[str, Any]:
    if not folds:
        raise ValueError("no folds to aggregate")
    metrics: list[dict[str, Any]] = []
    for fold in folds:
        row = fold.get("test_metrics") if isinstance(fold, dict) else None
        if not isinstance(row, dict) or not _numbers(row, "net_pnl", "trades", "max_drawdown_pct", "net_return_pct"):
            raise ValueError("fold metrics are incomplete; refusing to aggregate weak data")
        metrics.append(row)
    returns = sorted(float(m["net_return_pct"]) for m in metrics)
    factors = [float(m["profit_factor"]) for m in metrics if isinstance(m.get("profit_factor"), (int, float))]
    middle = len(returns) // 2
    median = returns[middle] if len(returns) % 2 else (returns[middle - 1] + returns[middle]) / 2.0
    return {"folds": len(folds), "net_pnl": round(sum(float(m["net_pnl"]) for m in metrics), 4),
            "trades": sum(int(m["trades"]) for m in metrics),
            "max_drawdown_pct": round(max(float(m["max_drawdown_pct"]) for m in metrics), 3),
            "profit_factor_mean": round(sum(factors) / len(factors), 3) if factors else None,
            "net_return_pct_median": round(median, 4), "net_return_pct_min": round(returns[0], 4),
            "net_return_pct_max": round(returns[-1], 4), "positive_folds": sum(1 for value in returns if value > 0),
            "note": OOS_NOTE}


def walk_forward(config: Config, symbol: str, *, bars: list[Kline], higher_bars: list[Kline],
                 filters: SymbolFilter, params: StrategyParams, folds: int = 4, warmup: int = 80,
                 assumed_spread_pct: float = 0.05, timeframe: str | None = None,
                 min_test_bars: int = MIN_FOLD_TEST_BARS, strategy: str | None = None) -> dict[str, Any]:
    """Rolling walk-forward: each fold is scored only on the unseen slice that follows its training window.

    The `warmup` seed bars taken from the tail of the training window only feed the indicators:
    run_backtest starts deciding at index `warmup`, so no trade of a fold can execute before that
    fold's test slice begins. Training windows expand, test windows never overlap.
    """
    if folds < 2:
        raise ValueError("walk-forward needs at least 2 folds; a single window proves nothing")
    if not bars:
        raise ValueError("no bars for walk-forward")
    total, first_cut = len(bars), warmup + MIN_SEGMENT_BARS
    region = total - first_cut
    if region < folds * min_test_bars:
        raise ValueError(f"insufficient data for {folds} walk-forward folds: need at least "
                         f"{first_cut + folds * min_test_bars} bars, got {total}")
    size, records, train_start = region // folds, [], _iso(bars[0].open_time)
    _, prefix = _family(strategy)
    for index in range(folds):
        cut = first_cut + index * size
        end = total if index == folds - 1 else first_cut + (index + 1) * size
        seed, test = bars[max(0, cut - warmup):cut], bars[cut:end]
        run = evaluate(config, symbol, bars=seed + test, higher_bars=higher_bars, filters=filters, params=params,
                       warmup=len(seed), assumed_spread_pct=assumed_spread_pct, timeframe=timeframe,
                       strategy=strategy)
        records.append({"fold": index + 1, "train_start": train_start, "train_bars": cut, "seed_bars": len(seed),
                        "train_end": _iso(bars[cut - 1].open_time), "test_start": _iso(test[0].open_time),
                        "test_end": _iso(test[-1].open_time), "test_bars": len(test), "test_metrics": run["metrics"],
                        "buy_hold_pct": run["buy_hold_pct"], "trades": run["trades"], "rejected": run["rejected"],
                        "params_label": run["params_label"]})
    return {"folds": records, "aggregate": aggregate_folds(records),
            "consistency": {"positive_folds": sum(1 for r in records if r["test_metrics"]["net_return_pct"] > 0),
                            "total_folds": len(records)}, "params_label": prefix + params.label(),
            "timeframe": timeframe or config.timeframe, "note": OOS_NOTE}


def validate_grid(grid: dict[str, Any], base: StrategyParams) -> list[str]:
    if not isinstance(grid, dict):
        raise ValueError("grid must be a mapping of strategy field to candidate values")
    if not grid:
        return []  # no parameter search: measure the current build (used when only the family changes)
    tunable = _tunable(base)
    keys = sorted(str(key) for key in grid)
    for key in keys:
        if key in _RISK_FORBIDDEN:
            raise ValueError(f"{key} is a risk limit owned by config.yaml, not a tunable strategy field")
        if key in _IDENTITY_FIELDS:
            raise ValueError(f"{key} identifies a version and cannot be searched; tune indicator fields instead")
        if key not in tunable:
            raise ValueError(f"unknown strategy parameter {key!r}: not a field of StrategyParams")
        values = grid[key]
        if isinstance(values, (str, bytes)) or not isinstance(values, Iterable) or not list(values):
            raise ValueError(f"grid values for {key} must be a non-empty list")
        if key in PARAM_SPACE_HINTS:
            low, high = PARAM_SPACE_HINTS[key]
            for value in values:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{key} is numeric; grid values must be numbers within [{low}, {high}]")
                if not low <= float(value) <= high:
                    raise ValueError(f"{key}={value} is outside the documented bounds [{low}, {high}]")
    return keys


def _variant(base: StrategyParams, spec: dict[str, Any]) -> StrategyParams:
    tunable = _tunable(base)
    return base.replace(**{key: _coerce(key, value, tunable[key]) for key, value in spec.items()})


def params_from_report(report: dict[str, Any] | None, base: StrategyParams) -> StrategyParams:
    """Rebuild a variant from a grid or stored-version dict; unknown keys are ignored, identity restored."""
    if not isinstance(report, dict):
        return base
    tunable = _tunable(base)
    changes = {key: _coerce(key, value, tunable[key]) for key, value in report.items() if key in tunable}
    return base.replace(**{field: str(report.get(field) or getattr(base, field))
                           for field in ("name", "version", "note")}, **changes)


def _rank_key(entry: dict[str, Any]) -> tuple[int, float, float, str]:
    """Never-trade variants rank last, even above losing ones.

    A flat 0.0% with zero trades is the absence of evidence, not a neutral result:
    the 400-day BTC optimizer wasted three experiments because a silent variant
    outranked every trading one and then failed the gate with 0 out-of-sample trades.
    """
    metrics = entry["metrics"]
    factor = metrics.get("profit_factor")
    measurable = 1 if int(metrics.get("trades") or 0) > 0 else 0
    return (measurable, float(metrics.get("net_return_pct") or 0.0),
            float(factor) if isinstance(factor, (int, float)) else -1.0, entry["params_label"])


def grid_search(config: Config, symbol: str, *, bars: list[Kline], higher_bars: list[Kline],
                filters: SymbolFilter, grid: dict[str, list], top: int = 5, params: StrategyParams | None = None,
                warmup: int = 80, assumed_spread_pct: float = 0.05, timeframe: str | None = None,
                max_evals: int = MAX_GRID_EVALS, split: Split | None = None,
                strategy: str | None = None) -> list[dict[str, Any]]:
    """Bounded search over StrategyParams fields: fit on the train slice, re-score each one on validation.

    Risk caps and position sizing are never searched or mutated: only tuning fields vary while the
    engine keeps sizing exactly as config already enforces it. `max_evals` counts variants; each
    variant costs two engine runs, one per window, and the hold-out is never read here.
    An empty grid measures the current build once — the way to audit a different entry-trigger family.
    """
    base = params or config.strategy_params or default_params()
    keys = validate_grid(grid, base)
    _fn, prefix = _family(strategy)
    parts = split or split_bars(bars)
    combos = [{}] if not keys else [dict(zip(keys, values)) for values in product(*(list(grid[key]) for key in keys))]
    if len(combos) > max_evals:
        raise ValueError(f"grid too large: {len(combos)} evaluations exceeds max_evals={max_evals}; narrow the grid")
    results: list[dict[str, Any]] = []
    for spec in combos:
        variant = _variant(base, spec)
        train = evaluate(config, symbol, bars=parts.train, higher_bars=higher_bars, filters=filters, params=variant,
                         warmup=warmup, assumed_spread_pct=assumed_spread_pct, timeframe=timeframe, strategy=strategy)
        validation = evaluate(config, symbol, bars=parts.validation, higher_bars=higher_bars, filters=filters,
                              params=variant, warmup=min(warmup, len(parts.validation) - 1),
                              assumed_spread_pct=assumed_spread_pct, timeframe=timeframe, strategy=strategy)
        results.append({"params_label": prefix + variant.label(), "params": variant.as_dict(),
                        "strategy": prefix.rstrip("|"), "metrics": train["metrics"],
                        "train": train, "validation": validation})
    results.sort(key=_rank_key, reverse=True)
    return results[:max(1, int(top))]


def _oos_view(block: Any) -> dict[str, Any] | None:
    """Fold aggregates report medians/means; normalise them to the metric names the gate compares."""
    if not isinstance(block, dict):
        return None
    view = dict(block)
    for target, names in (("net_return_pct", ("net_return_pct", "net_return_pct_median")),
                          ("profit_factor", ("profit_factor", "profit_factor_mean"))):
        view[target] = next((view[name] for name in names if _numbers(view, name)), None)
    return view if _numbers(view, "net_return_pct", "max_drawdown_pct", "trades") else None


def _oos_metrics(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """Out-of-sample evidence only: the walk-forward aggregate, else the validation slice. Never train or test."""
    if not isinstance(report, dict):
        return None
    block, validation = report.get("walk_forward"), report.get("validation")
    aggregate = block.get("aggregate") if isinstance(block, dict) else None
    window = validation if isinstance(validation, dict) else {}
    return _oos_view(aggregate) or _oos_view(window) or _oos_view(window.get("metrics"))


def _fold_list(report: dict[str, Any]) -> list[dict[str, Any]]:
    block = report.get("walk_forward")
    folds = block.get("folds") if isinstance(block, dict) else None
    return [fold for fold in folds if isinstance(fold, dict)] if isinstance(folds, list) else []


def acceptance(candidate: dict[str, Any], champion: dict[str, Any], *, min_trades: int = 8) -> tuple[bool, list[str]]:
    """Anti-overfitting gate. Failure is normal: the champion stays and the reasons get stored."""
    challenger, incumbent = _oos_metrics(candidate), _oos_metrics(champion)
    if challenger is None:
        return False, ["candidate has no out-of-sample evaluation: validation or walk-forward aggregate is required"]
    if incumbent is None:
        return False, ["champion has no out-of-sample baseline: measure the current build before replacing it"]

    reasons: list[str] = []
    label = candidate.get("params_label")
    if label and label == champion.get("params_label"):
        reasons.append("candidate is the unchanged champion: the grid selected the current parameters, so this "
                       "hypothesis did not alter behaviour on this dataset and nothing new was tested")
    evidence = candidate.get("risk_checks")
    if not isinstance(evidence, dict) or not evidence:
        reasons.append("no risk-limit evidence recorded for the candidate")
    else:
        missing = [key for key in RISK_EVIDENCE_KEYS if key not in evidence]
        if missing:
            reasons.append(f"risk-limit evidence incomplete: {', '.join(missing)}")
        reasons += [f"risk-limit check failed: {key}" for key, value in sorted(evidence.items()) if value is not True]
    if int(challenger.get("trades", 0)) < 0 or float(challenger.get("max_drawdown_pct", 0)) < 0:
        reasons.append("candidate reports negative trade count or drawdown; refusing to accept invalid data")
    if reasons:
        return False, reasons

    if float(challenger["net_return_pct"]) <= float(incumbent["net_return_pct"]):
        reasons.append(f"no out-of-sample net-return improvement: candidate {float(challenger['net_return_pct']):.3f}% "
                       f"vs champion {float(incumbent['net_return_pct']):.3f}%")
    factor, baseline = challenger.get("profit_factor"), incumbent.get("profit_factor")
    if not isinstance(factor, (int, float)) or not isinstance(baseline, (int, float)):
        reasons.append("profit factor unavailable on out-of-sample data; cannot show improvement")
    elif float(factor) <= float(baseline):
        reasons.append(f"no out-of-sample profit-factor improvement: candidate {float(factor):.3f} "
                       f"vs champion {float(baseline):.3f}")
    if float(challenger["max_drawdown_pct"]) > float(incumbent["max_drawdown_pct"]) + DD_TOLERANCE_PP:
        reasons.append(f"max drawdown {float(challenger['max_drawdown_pct']):.3f}% is more than {DD_TOLERANCE_PP}pp "
                       f"worse than champion {float(incumbent['max_drawdown_pct']):.3f}%")
    if int(challenger["trades"]) < min_trades:
        reasons.append(f"only {int(challenger['trades'])} out-of-sample trades, minimum is {min_trades}")
    folds = _fold_list(candidate)
    if folds:
        positive = sum(1 for fold in folds if float((fold.get("test_metrics") or {}).get("net_return_pct") or 0.0) > 0)
        if positive * 2 < len(folds):
            reasons.append(f"profitable in only {positive} of {len(folds)} folds; at least half is required "
                           f"so one lucky window cannot win")
    return (not reasons, reasons)


def _version_record(params: StrategyParams, *, report: dict[str, Any], source: str, parent: str | None,
                    accepted: bool, decision: str, strategy: str | None = None) -> dict[str, Any]:
    return {"digest": params.digest(), "name": params.name, "version": params.version, "params": params.as_dict(),
            "strategy": strategy or "", "source": source, "parent": parent, "accepted": accepted,
            "decision": decision,
            "train": report.get("train", {}), "validation": report.get("validation", {}),
            "test": report.get("test", {}), "walk_forward": report.get("walk_forward", {})}


def run_research(store: Any, config: Config, *, symbol: str, timeframe: str | None, bars: list[Kline],
                 higher_bars: list[Kline], filters: SymbolFilter, grid: dict[str, list], folds: int = 4,
                 champion: dict[str, Any] | None = None, top: int = 3, warmup: int = 80,
                 assumed_spread_pct: float = 0.05, min_trades: int = 8,
                 dataset_id: str | None = None, strategy: str | None = None) -> dict[str, Any]:
    """Full honest loop: split -> grid on train -> re-score -> walk-forward both -> gate -> persist.

    `strategy` selects an entry-trigger family from `strategy_lab`; only the trigger changes,
    so a family comparison never mixes in a different exit, sizing or risk policy.
    """
    started = datetime.now(timezone.utc)
    base = config.strategy_params or default_params()
    _fn, prefix = _family(strategy)
    tf = timeframe or config.timeframe
    try:
        step = seconds_per_bar(tf)
    except Exception as exc:
        raise ValueError(f"unknown timeframe {tf!r}: {exc}") from exc
    spacings = set(bar_spacings(bars))
    if any(gap <= 0 or gap % step for gap in spacings):
        raise ValueError(f"bars are not aligned to timeframe {tf} ({step}s): spacings {sorted(spacings)[:5]}")
    limits_before = config.limits
    try:
        parts = split_bars(bars, min_bars=max(MIN_SEGMENT_BARS, warmup + 1))
    except ValueError as exc:
        store.log("error", "research", f"research refused: {exc}", {"symbol": symbol, "timeframe": tf})
        store.record_run(kind="grid", symbol=symbol, timeframe=tf, params_label=prefix + base.label(),
                         params=base.tuning(),
                         dataset_id=dataset_id, window=None, metrics={}, status="failed", error=str(exc))
        raise

    grid_results = grid_search(config, symbol, bars=bars, higher_bars=higher_bars, filters=filters, grid=grid, top=top,
                               params=base, warmup=warmup, assumed_spread_pct=assumed_spread_pct, timeframe=tf,
                               split=parts, strategy=strategy)
    measure = partial(evaluate, config, symbol, higher_bars=higher_bars, filters=filters, timeframe=tf,
                      assumed_spread_pct=assumed_spread_pct, strategy=strategy)
    fold_run = partial(walk_forward, config, symbol, bars=bars, higher_bars=higher_bars, filters=filters, folds=folds,
                       warmup=warmup, assumed_spread_pct=assumed_spread_pct, timeframe=tf, strategy=strategy)
    best = grid_results[0]
    candidate_params = params_from_report(best["params"], base)
    candidate: dict[str, Any] = {"params_label": best["params_label"], "params": best["params"],
                                 "train": best["train"], "validation": best["validation"]}
    hold_out_warmup = min(warmup, len(parts.validation) - 1, len(parts.test) - 1)
    candidate["test"] = measure(bars=parts.test, params=candidate_params, warmup=hold_out_warmup)
    candidate["walk_forward"] = fold_run(params=candidate_params)

    # The baseline is the build the system actually runs today: when the candidate switches entry-trigger
    # family, the incumbent must be measured with the config's own family, not the challenger's.
    champion_report: dict[str, Any] = dict(champion or {})
    champion_params = params_from_report(champion_report.get("params"), base)
    champion_family = str(champion_report.get("strategy") or "").strip() or None
    _ch_fn, ch_prefix = _family(champion_family)
    champion_report.setdefault("params_label", ch_prefix + champion_params.label())
    champion_report.setdefault("params", champion_params.as_dict())
    champion_measure = partial(evaluate, config, symbol, higher_bars=higher_bars, filters=filters, timeframe=tf,
                               assumed_spread_pct=assumed_spread_pct, strategy=champion_family)
    champion_fold = partial(walk_forward, config, symbol, bars=bars, higher_bars=higher_bars, filters=filters,
                            folds=folds, warmup=warmup, assumed_spread_pct=assumed_spread_pct, timeframe=tf,
                            strategy=champion_family)
    if not isinstance(champion_report.get("validation"), dict):
        champion_report["validation"] = champion_measure(bars=parts.validation, params=champion_params,
                                                         warmup=hold_out_warmup)
    if not isinstance(champion_report.get("walk_forward"), dict):
        champion_report["walk_forward"] = champion_fold(params=champion_params)

    aggregate = candidate["walk_forward"]["aggregate"]
    checks = {"sized_positions_recorded": int(aggregate.get("trades") or 0) > 0,
              "drawdown_within_config_limit": float(aggregate.get("max_drawdown_pct") or 0.0)
              <= config.limits.max_drawdown_pct,
              "risk_limits_untouched": config.limits == limits_before,
              "grid_targets_strategy_fields_only": True}
    candidate["risk_checks"] = checks
    accepted, reasons = acceptance(candidate, champion_report, min_trades=min_trades)
    verdict = "accepted" if accepted else "rejected"
    decision = (f"{verdict}: candidate replaces the champion" if accepted
                else f"{verdict}: champion kept -> {'; '.join(reasons)}")

    split_dict, window = parts.as_dict(), (_iso(parts.train[0].open_time), _iso(parts.test[-1].open_time))
    champion_wf = champion_report["walk_forward"]
    store.record_run(kind="grid", symbol=symbol, timeframe=tf, params_label=candidate["params_label"],
                     params=candidate["params"], dataset_id=dataset_id, window=window,
                     metrics=candidate["train"]["metrics"], split=split_dict,
                     rejections=candidate["train"]["rejected"],
                     duration_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
    store.record_run(kind="walk_forward", symbol=symbol, timeframe=tf, params_label=candidate["params_label"],
                     params=candidate["params"], dataset_id=dataset_id, window=window, metrics=aggregate,
                     split=split_dict, error="" if accepted else "; ".join(reasons))
    store.record_run(kind="walk_forward", symbol=symbol, timeframe=tf,
                     params_label=champion_report["params_label"], params=champion_report["params"],
                     dataset_id=dataset_id, window=window, metrics=champion_wf["aggregate"], split=split_dict)
    store.save_strategy_version(_version_record(candidate_params, report=candidate,
                                                source=f"grid_search:{strategy}" if strategy else "grid_search",
                                                parent=champion_params.digest(), accepted=accepted, decision=decision,
                                                strategy=strategy))
    store.save_strategy_version(_version_record(champion_params, report=champion_report,
                                                source=f"champion_baseline:{champion_family}" if champion_family
                                                else "champion_baseline",
                                                parent=champion_report.get("parent"),
                                                accepted=bool(champion_report.get("accepted")),
                                                decision=str(champion_report.get("decision") or "incumbent champion"),
                                                strategy=champion_family))
    store.log("info" if accepted else "warning", "research",
              f"research {verdict} {candidate['params_label']} against {champion_report['params_label']}",
              {"symbol": symbol, "timeframe": tf, "verdict": verdict, "reasons": reasons,
               "strategy": strategy or "", "champion_strategy": champion_family or "",
               "grid_candidates": len(grid_results), "candidate_out_of_sample": aggregate,
               "champion_out_of_sample": champion_wf["aggregate"]})

    return {"verdict": verdict, "reasons": reasons, "decision": decision,
            "candidate_label": candidate["params_label"], "champion_label": champion_report["params_label"],
            "candidate_params": candidate_params.as_dict(),
            "strategy": strategy or "", "champion_strategy": champion_family or "",
            "symbol": symbol, "timeframe": tf, "mode": config.mode, "dataset_id": dataset_id,
            "generated_at": started.isoformat(timespec="seconds"), "split": split_dict, "grid": [
                {"params_label": entry["params_label"], "train_net_return_pct": entry["metrics"].get("net_return_pct"),
                 "validation_net_return_pct": entry["validation"]["metrics"].get("net_return_pct")}
                for entry in grid_results],
            "train": candidate["train"], "validation": candidate["validation"], "test": candidate["test"],
            "champion_validation": champion_report["validation"],
            "walk_forward": candidate["walk_forward"], "champion_walk_forward": champion_report["walk_forward"],
            "risk_checks": checks, "min_trades": min_trades,
            "test_note": "hold-out segment: measured for reporting only, never used by acceptance()",
            "disclaimer": "اعداد بازده گذشته‌اند (شبیه‌سازی با کارمزد و slippage) و پیش‌بینی یا تضمین سود نیستند."}
