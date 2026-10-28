"""Read the D-024 threshold grid and apply the pre-registered acceptance rule.

One knob (`lambda`) scales the three entry-gate thresholds of the shipped classifier. Every
variant was replayed on both independent windows at deployed breadth with production cadence;
this tool only reads those recorded artifacts, re-times them at K=1 (deployed) and K=2 (D-023),
and answers the one question D-024 was pre-registered for: does the absolute threshold level
change expectancy, or only the count?

Read-only. Nothing here adopts a threshold: the rule is checked, reported and the config stays
untouched. `--grid` also folds in the shipped lambda=1 cells from the D-022 records.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from cat.config import load_config  # noqa: E402
from cat.strategies import default_params  # noqa: E402
from paper_frequency_replay import serial_account_schedule  # noqa: E402

LAMBDAS = (0.5, 0.75, 1.0, 1.5, 2.0)
TIMEFRAMES = ("4h", "1h", "15m")
WINDOWS = ("w1", "w3")
REQUIRED_PF = 1.1
THRESHOLD_KEYS = ("vol_threshold", "atr_pct_threshold", "slope_threshold")


def expected_thresholds(lam: float) -> dict[str, float]:
    base = default_params()
    return {key: round(getattr(base, key) * lam, 6) for key in THRESHOLD_KEYS}


def top_rejections(summary: dict[str, Any], limit: int = 4) -> dict[str, int]:
    """The funnel that explains an empty cell, without the allowlist refusals flooding it.

    `vol_threshold`/`atr_pct_threshold` are ceilings (a bar above them is HIGH_VOLATILITY) while
    `slope_threshold` is a floor, so a cell can be empty for opposite reasons; only the recorded
    rejection counts say which.
    """
    pooled = {key: count for key, count in summary["rejections_pooled"].items()
              if not key.endswith("not in approved symbol list")}
    return dict(sorted(pooled.items(), key=lambda item: (-item[1], item[0]))[:limit])


def cell(artifact: Path, *, lam: float, window: str, limits: Any) -> dict[str, Any]:
    doc = json.loads(artifact.read_text(encoding="utf-8"))
    summary, rows = doc["summary"], doc["per_symbol"]
    measured = [row for row in rows if "closed_trades" in row]
    timelines = [row.get("trade_timeline", []) for row in measured]
    days = summary["counted_days_per_symbol"]
    schedules = {k: serial_account_schedule(
        timelines, window_days=days, max_trades_per_day=limits.max_trades_per_day,
        cooldown_minutes_after_loss=limits.cooldown_minutes_after_loss, max_concurrent=k)
        for k in (1, 2)}
    blocked = {key.split(" ")[0] for key in summary["rejections_pooled"]
               if key.endswith("not in approved symbol list")}
    thresholds = {key: summary["classifier"][key] for key in THRESHOLD_KEYS}
    want = expected_thresholds(lam)
    if thresholds != want:
        # The folder name is the only thing claiming this artifact is lambda=X. If the file
        # disagrees, the grid is not the grid and a cell would be counted twice or not at all.
        raise ValueError(f"{artifact}: recorded thresholds {thresholds} do not match lambda={lam} ({want})")
    return {
        "lambda": lam, "window": window, "timeframe": summary["timeframe"],
        "artifact": artifact.relative_to(ROOT).as_posix().replace("\\", "/")
        if artifact.is_absolute() and str(artifact).startswith(str(ROOT)) else artifact.as_posix(),
        "classifier": summary["classifier"]["label"],
        "thresholds": thresholds,
        "symbols_replayed": summary["symbols_replayed"],
        "symbols_blocked_by_allowlist": len(blocked),
        "candidates": sum(len(timeline) for timeline in timelines),
        "decision_ticks": summary["decision_bars_total"],
        "counted_from": summary["counted_from"], "counted_to": summary["counted_to"],
        "funnel_top": top_rejections(summary),
        "counted_days": days,
        "cadence": summary["cadence"], "decide_every_bars": summary["decide_every_bars"],
        "lake_label_covers_every_month_read": (
            sorted(summary.get("months_read") or []) == sorted(summary["data_version"]["months"])),
        "holding_hours_mean_of_means": summary["holding_hours"]["mean_of_means"],
        "schedules": {str(k): {key: v[key] for key in
                               ("taken", "trades_per_day", "profit_factor", "win_rate_pct",
                                "net_pnl_per_1000", "max_drawdown_per_1000", "fees")}
                      for k, v in schedules.items()},
    }


def artifact_path(lam: float, window: str, timeframe: str, *, grid: Path, shipped: Path) -> Path:
    """Where a cell was recorded: lambda=1 reuses the shipped D-022 run, the rest live in the grid."""
    folder = (shipped / f"freqprod_{window}_100_{timeframe}" if lam == 1.0
              else grid / f"lam{lam}_{window}_{timeframe}")
    return folder / f"frequency-{timeframe}.json"


def satisfies_rule(cells: list[dict[str, Any]]) -> dict[str, Any]:
    """Pre-registered rule: same (lambda, timeframe, K) must clear PF >= 1.1 and net > 0 on BOTH windows."""
    verdicts: dict[str, Any] = {}
    for lam in LAMBDAS:
        for tf in TIMEFRAMES:
            hits = []
            for k in ("1", "2"):
                group = [c for c in cells if c["lambda"] == lam and c["timeframe"] == tf]
                if len({c["window"] for c in group}) < len(WINDOWS):
                    continue
                ok = all((c["schedules"][k]["profit_factor"] or 0.0) >= REQUIRED_PF
                         and c["schedules"][k]["net_pnl_per_1000"] > 0 for c in group)
                if ok:
                    hits.append("K=" + k)
            verdicts[f"lam{lam}·{tf}"] = hits
    return verdicts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid", default="state/quant/results/lambda_grid")
    parser.add_argument("--shipped", default="state/quant/results",
                        help="directory holding the D-022 freqprod_w{1,3}_100_* records (lambda = 1)")
    parser.add_argument("--out", default="state/quant/results/lambda_grid")
    args = parser.parse_args()

    limits = load_config(root=ROOT).limits
    grid = Path(args.grid)
    if not grid.is_absolute():
        grid = ROOT / grid
    shipped = Path(args.shipped)
    if not shipped.is_absolute():
        shipped = ROOT / shipped

    cells: list[dict[str, Any]] = []
    for lam in LAMBDAS:
        for window in WINDOWS:
            for tf in TIMEFRAMES:
                artifact = artifact_path(lam, window, tf, grid=grid, shipped=shipped)
                if artifact.exists():
                    cells.append(cell(artifact, lam=lam, window=window, limits=limits))

    missing = [(lam, window, tf) for lam in LAMBDAS for window in WINDOWS for tf in TIMEFRAMES
               if not any(c["lambda"] == lam and c["window"] == window and c["timeframe"] == tf
                          for c in cells)]
    rule = satisfies_rule(cells)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "required_profit_factor": REQUIRED_PF,
        "cells_measured": len(cells),
        "cells_missing": [{"lambda": lam, "window": window, "timeframe": tf}
                          for lam, window, tf in missing],
        "grid_size_tried": len(LAMBDAS) * len(TIMEFRAMES),
        "satisfies_rule": rule,
        "cells": cells,
        "note": ("sensitivity of the deployed classifier to its absolute threshold level, measured on "
                 "two independent windows; no threshold was adopted and config.yaml is untouched"),
    }
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    (out / "threshold_lambda_grid.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"cells measured: {len(cells)} (missing {len(missing)}); grid tried: "
          f"{report['grid_size_tried']} (lambda x timeframe), each on 2 windows\n")
    header = (f"{'lambda':>7} {'win':>4} {'tf':>4} {'cand':>5} {'K1/day':>7} {'K1 PF':>6} "
              f"{'K1 net':>9} {'K2/day':>7} {'K2 PF':>6} {'K2 net':>9} {'mean hold h':>11} {'block':>5}")
    print(header)
    for c in sorted(cells, key=lambda row: (row["timeframe"], row["lambda"], row["window"])):
        s1, s2 = c["schedules"]["1"], c["schedules"]["2"]
        print(f"{c['lambda']:>7} {c['window']:>4} {c['timeframe']:>4} {c['candidates']:>5} "
              f"{s1['trades_per_day']:>7} {s1['profit_factor'] if s1['profit_factor'] is not None else '-':>6} "
              f"{s1['net_pnl_per_1000']:>9} {s2['trades_per_day']:>7} "
              f"{s2['profit_factor'] if s2['profit_factor'] is not None else '-':>6} "
              f"{s2['net_pnl_per_1000']:>9} "
              f"{c['holding_hours_mean_of_means'] if c['holding_hours_mean_of_means'] is not None else '-':>11} "
              f"{c['symbols_blocked_by_allowlist']:>5}")
    passed = {k: v for k, v in rule.items() if v}
    empty = [c for c in cells if c["candidates"] == 0]
    if empty:
        print("\nempty cells and the gate that emptied them (over the recorded decision ticks):")
        for c in sorted(empty, key=lambda row: (row["timeframe"], row["lambda"], row["window"])):
            top = ", ".join(f"{key} {value}" for key, value in list(c["funnel_top"].items())[:2])
            print(f"  lam={c['lambda']} {c['window']} {c['timeframe']:>4}: {c['decision_ticks']} ticks"
                  f" -> {top}")
    print("\npre-registered rule (PF >= 1.1 and net > 0 on BOTH windows, same lambda/timeframe/K):")
    print("  satisfied by: " + (", ".join(f"{k} at {','.join(v)}" for k, v in passed.items())
                                if passed else "هیچ‌کدام — ۰ از " + str(report["grid_size_tried"]) + " خانه"))
    if missing:
        print(f"  incomplete cells: {len(missing)}")
    print(f"\nwritten: {(out / 'threshold_lambda_grid.json').as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
