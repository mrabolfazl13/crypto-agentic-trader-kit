"""Walk-forward baseline on default parameters (no fitting, no search).

Runs the same pre-registered strategy/timeframe pairs as ``quant_baseline.py``
through rolling folds that end before the hold-out test slice, and appends one
row per configuration to ``EXPERIMENT_LOG.csv``. Each fold is scored only on its
own unseen 7-day slice, with fees, slippage and a buy-and-hold reference.

Usage:
    python tools/quant_walk_forward_baseline.py
    python tools/quant_walk_forward_baseline.py --configs ema_trend:4h,momentum:4h
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from cat.config import load_config  # noqa: E402
from cat.quant.engine import (Window, chronological_splits, data_window,  # noqa: E402
                              simulate_settings, walk_forward)
from cat.quant.features import QuantParams  # noqa: E402
from cat.quant.loop import code_version  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.storage import current_data_version, universe_symbols  # noqa: E402
from quant_baseline import DEFAULT_CONFIGS, append_log, utc_now  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configs", default="", help="strategy:timeframe pairs, comma separated")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out-dir", default=str(ROOT / "state" / "quant" / "results" / "walk_forward"))
    parser.add_argument("--log", default=str(ROOT / "EXPERIMENT_LOG.csv"))
    args = parser.parse_args()

    settings = load_quant_settings(load_config(root=ROOT))
    symbols = universe_symbols(settings)
    if args.limit:
        symbols = symbols[: max(1, args.limit)]
    configs = DEFAULT_CONFIGS
    if args.configs:
        configs = tuple((part.split(":")[0], part.split(":")[1]) for part in args.configs.split(","))
    window = data_window(settings, symbols)
    splits = chronological_splits(window, train=settings.walk_forward.train_fraction,
                                  validation=settings.walk_forward.validation_fraction)
    fold_window = Window(window.start_ts, splits["test"].start_ts)
    params = QuantParams()
    sim = simulate_settings(settings)
    version = code_version(ROOT)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started_all = time.monotonic()

    print(f"walk-forward baseline: {len(configs)} configs x {len(symbols)} symbols; "
          f"folds {settings.walk_forward.folds}x{settings.walk_forward.test_days}d "
          f"before test start {splits['test'].iso()['start']}", flush=True)
    for strategy, timeframe in configs:
        run_id = f"wf-baseline-{strategy}-{timeframe}"
        started = time.monotonic()
        result = walk_forward(settings, symbols, strategy=strategy, timeframe=timeframe, params=params,
                              sim=sim, window=fold_window)
        aggregate = result["aggregate"]
        seconds = time.monotonic() - started
        row = {
            "date": utc_now(), "run_id": run_id, "code_version": version,
            "strategy": strategy, "timeframe": timeframe, "slice": "walk_forward",
            "params_label": params.label(),
            "data_start": window.iso()["start"], "data_end": window.iso()["end"],
            "window_start": fold_window.iso()["start"], "window_end": fold_window.iso()["end"],
            "trades": aggregate.get("trades"), "net_return_pct": aggregate.get("net_return_pct_sum"),
            "max_drawdown_pct": aggregate.get("max_drawdown_pct"),
            "effective_drawdown_pct": aggregate.get("max_effective_drawdown_pct"),
            "profit_factor": aggregate.get("profit_factor_mean"),
            "win_rate_pct": None, "fees": None, "exposure_pct": None,
            "benchmark_return_pct": aggregate.get("benchmark_return_pct_sum"),
            "excess_return_pct": round(float(aggregate.get("net_return_pct_sum") or 0.0)
                                       - float(aggregate.get("benchmark_return_pct_sum") or 0.0), 4),
            "status": f"completed; positive_folds_pct={aggregate.get('positive_fold_pct')}; "
                      f"folds={aggregate.get('folds')}",
        }
        payload = {"run_id": run_id, "created_at": row["date"], "code_version": version,
                   "data_version": current_data_version(settings), "params": params.as_dict(),
                   "window": fold_window.iso(), "elapsed_seconds": round(seconds, 2),
                   "folds": result["folds"], "aggregate": aggregate}
        (out_dir / f"{run_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                                encoding="utf-8")
        append_log(Path(args.log), row)
        print(f"{run_id}: folds={aggregate.get('folds')} trades={row['trades']} "
              f"sum_net={row['net_return_pct']}% positive={aggregate.get('positive_fold_pct')}% "
              f"PF={row['profit_factor']} DD={row['max_drawdown_pct']}% "
              f"BH_sum={row['benchmark_return_pct']}% elapsed={seconds:.0f}s", flush=True)

    print(f"total {time.monotonic() - started_all:.1f}s; log {args.log}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
