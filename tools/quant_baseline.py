"""Pre-registered baseline: default parameters, no search, chronological slices.

Every strategy/timeframe pair from the research plan is measured once with
``QuantParams()`` defaults on train, validation and test, with fees, slippage
and an equal-weight buy-and-hold reference. Nothing is tuned, so the numbers are
a floor that any optimized candidate must beat honestly.

Writes one JSON per run plus a row per run in ``EXPERIMENT_LOG.csv``.

Usage:
    python tools/quant_baseline.py
    python tools/quant_baseline.py --limit 20 --configs ema_trend:1h,breakout:4h
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

import psutil  # noqa: E402

from cat.config import load_config  # noqa: E402
from cat.quant.engine import (chronological_splits, data_window, run_backtest,  # noqa: E402
                              simulate_settings)
from cat.quant.features import QuantParams  # noqa: E402
from cat.quant.loop import code_version  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.storage import current_data_version, universe_symbols  # noqa: E402

DEFAULT_CONFIGS = (
    ("ema_trend", "1h"), ("ema_trend", "4h"),
    ("trend_pullback", "1h"), ("trend_pullback", "15m"),
    ("momentum", "4h"), ("mean_reversion", "1h"),
    ("breakout", "4h"), ("breakout", "1h"),
)
LOG_COLUMNS = (
    "date", "run_id", "code_version", "strategy", "timeframe", "slice", "params_label",
    "data_start", "data_end", "window_start", "window_end", "trades", "net_return_pct",
    "max_drawdown_pct", "effective_drawdown_pct", "profit_factor", "win_rate_pct", "fees",
    "exposure_pct", "benchmark_return_pct", "excess_return_pct", "status",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_log(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key) for key in LOG_COLUMNS})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configs", default="", help="strategy:timeframe pairs, comma separated")
    parser.add_argument("--limit", type=int, default=0, help="limit symbols (0 = whole universe)")
    parser.add_argument("--slices", default="train,validation,test")
    parser.add_argument("--out-dir", default=str(ROOT / "state" / "quant" / "results" / "baseline"))
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
    slices = [name.strip() for name in args.slices.split(",") if name.strip()]
    params = QuantParams()
    sim = simulate_settings(settings)
    version = code_version(ROOT)
    data_version = current_data_version(settings)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    process = psutil.Process(os.getpid())
    started_all = time.monotonic()
    rows: list[dict] = []

    print(f"baseline: {len(configs)} configs x {len(slices)} slices x {len(symbols)} symbols; "
          f"window {window.iso()}", flush=True)
    for strategy, timeframe in configs:
        for name in slices:
            if name not in splits:
                continue
            part = splits[name]
            run_id = f"baseline-{strategy}-{timeframe}-{name}"
            started = time.monotonic()
            memory_before = process.memory_info().rss / (1024 * 1024)
            result = run_backtest(settings, symbols, strategy=strategy, timeframe=timeframe,
                                  params=params, sim=sim, start_ts=part.start_ts, end_ts=part.end_ts)
            seconds = time.monotonic() - started
            metrics = result["metrics"]
            row = {
                "date": utc_now(), "run_id": run_id, "code_version": version,
                "strategy": strategy, "timeframe": timeframe, "slice": name,
                "params_label": params.label(),
                "data_start": window.iso()["start"], "data_end": window.iso()["end"],
                "window_start": part.iso()["start"], "window_end": part.iso()["end"],
                "trades": metrics.get("trades"), "net_return_pct": metrics.get("net_return_pct"),
                "max_drawdown_pct": metrics.get("max_drawdown_pct"),
                "effective_drawdown_pct": metrics.get("effective_drawdown_pct"),
                "profit_factor": metrics.get("profit_factor"),
                "win_rate_pct": metrics.get("win_rate_pct"), "fees": metrics.get("fees"),
                "exposure_pct": metrics.get("exposure_pct"),
                "benchmark_return_pct": metrics.get("benchmark_return_pct"),
                "excess_return_pct": metrics.get("excess_return_pct"), "status": "completed",
            }
            payload = {
                "run_id": run_id, "created_at": row["date"], "code_version": version,
                "data_version": data_version, "params": params.as_dict(), "slice": name,
                "window": part.iso(), "elapsed_seconds": round(seconds, 2),
                "memory_mb": {"before": round(memory_before, 1),
                              "after": round(process.memory_info().rss / (1024 * 1024), 1)},
                "metrics": metrics, "benchmark": result["benchmark"],
                "symbols_evaluated": result["symbols_evaluated"], "errors": result["errors"],
            }
            (out_dir / f"{run_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                                    encoding="utf-8")
            append_log(Path(args.log), row)
            rows.append(row)
            print(f"{run_id}: trades={row['trades']} net={row['net_return_pct']}% "
                  f"PF={row['profit_factor']} DD={row['max_drawdown_pct']}% "
                  f"BH={row['benchmark_return_pct']}% elapsed={seconds:.0f}s", flush=True)

    summary = {"generated_at": utc_now(), "code_version": version, "data_version": data_version,
               "universe_symbols": len(symbols), "window": window.iso(),
               "params": params.as_dict(), "rows": rows,
               "elapsed_seconds": round(time.monotonic() - started_all, 1)}
    (out_dir / "baseline_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                                   encoding="utf-8")
    print(f"wrote {out_dir / 'baseline_summary.json'} and {args.log}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
