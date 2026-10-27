"""Concurrency curve for one account: does letting K positions overlap buy the cadence Gate B needs?

Reads the trade timelines already recorded by `tools/paper_frequency_replay.py` (no re-simulation,
so every column differs only by the open-position budget) and re-runs the greedy single-account
schedule for each K. K = 1 must reproduce the recorded `single_account_schedule` exactly; if it
does not, the run is marked and the numbers are not usable.

Read-only: nothing is traded, no threshold, timeframe, allocation or cap in config.yaml changes.
The deployed engine still holds one serial position at 5% allocation.
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

from paper_frequency_replay import serial_account_schedule  # noqa: E402
from cat.config import load_config  # noqa: E402

CONCURRENCY_SWEEP = (1, 2, 3, 5)
GATE_B_TRADES_PER_DAY = 1.1322


def curve(artifact: Path, *, limits: Any, sweep: tuple[int, ...]) -> dict[str, Any]:
    doc = json.loads(artifact.read_text(encoding="utf-8"))
    summary, rows = doc["summary"], doc["per_symbol"]
    measured = [row for row in rows if "closed_trades" in row]
    timelines = [row.get("trade_timeline", []) for row in measured]
    days = summary["counted_days_per_symbol"]
    points = [serial_account_schedule(
        timelines, window_days=days, max_trades_per_day=limits.max_trades_per_day,
        cooldown_minutes_after_loss=limits.cooldown_minutes_after_loss, max_concurrent=k)
        for k in sweep]
    recorded = summary["single_account_schedule"]
    k1 = points[0]
    matches = all(k1[key] == value for key, value in recorded.items() if key != "note")
    try:
        shown = str(artifact.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        shown = artifact.as_posix()
    curve_points = []
    for point in points:
        row = {key: point[key] for key in
               ("max_concurrent", "taken", "peak_concurrent_positions", "trades_per_day",
                "win_rate_pct", "profit_factor", "net_pnl_per_1000", "max_drawdown_per_1000",
                "fees", "days_to_30_trades", "skipped")}
        # Every replayed symbol sizes its entry off 5% of its own 1000-USDT account, and the
        # merged schedule sums those pnls, so K open positions mean 5% x K of one account is
        # committed at once — more than the deployed total-allocation cap allows.
        row["allocation_cap_needed_pct"] = round(limits.max_account_allocation_pct
                                                 * point["max_concurrent"], 2)
        row["deployed_allocation_cap_pct"] = limits.max_account_allocation_pct
        curve_points.append(row)
    return {
        "artifact": shown,
        "timeframe": summary["timeframe"],
        "window": [summary["counted_from"], summary["counted_to"]],
        "window_days": days,
        "cadence": summary["cadence"],
        "decide_every_bars": summary["decide_every_bars"],
        "symbols_replayed": summary["symbols_replayed"],
        "months_read": summary.get("months_read"),
        "lake_label": summary["data_version"],
        "lake_label_covers_every_month_read": (
            sorted(summary.get("months_read") or []) == sorted(summary["data_version"]["months"])),
        "candidate_trades": sum(len(timeline) for timeline in timelines),
        "data_version": summary["data_version"],
        "reproduces_recorded_k1": matches,
        "recorded_k1": recorded,
        "curve": curve_points,
    }


def both_windows_pass_at(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Smallest K that clears the gate on *every* window replayed for that timeframe.

    One window passing is not a result: D-022 showed the 15m cadence depends on the slice.
    """
    verdicts: dict[str, Any] = {}
    for timeframe in sorted({row["timeframe"] for row in results}):
        rows = [row for row in results if row["timeframe"] == timeframe]
        if len(rows) < 2:
            verdicts[timeframe] = {"windows": len(rows), "min_k_passing_everywhere": None,
                                   "note": "needs at least two independent windows"}
            continue
        smallest = None
        for k in CONCURRENCY_SWEEP:
            points = [next(point for point in row["curve"] if point["max_concurrent"] == k) for row in rows]
            if all(point["trades_per_day"] >= GATE_B_TRADES_PER_DAY for point in points):
                smallest = k
                break
        if smallest is None:
            verdicts[timeframe] = {"windows": len(rows), "min_k_passing_everywhere": None,
                                   "profit_factor_at_that_k": None,
                                   "expectancy_positive_at_that_k": None}
            continue
        points = [next(point for point in row["curve"] if point["max_concurrent"] == smallest)
                  for row in rows]
        verdicts[timeframe] = {
            "windows": len(rows), "min_k_passing_everywhere": smallest,
            "trades_per_day_at_that_k": [point["trades_per_day"] for point in points],
            "profit_factor_at_that_k": [point["profit_factor"] for point in points],
            "net_per_1000_at_that_k": [point["net_pnl_per_1000"] for point in points],
            "allocation_cap_needed_pct_at_that_k": [point["allocation_cap_needed_pct"] for point in points],
            "expectancy_positive_at_that_k": any(point["profit_factor"] is None
                                                 or point["profit_factor"] >= 1.0 for point in points)}
    return verdicts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True,
                        help="comma separated paths to frequency-<timeframe>.json files recorded by the replay")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    limits = load_config(root=ROOT).limits
    results = []
    for part in args.artifacts.split(","):
        artifact = Path(part.strip())
        if not artifact.is_absolute():
            artifact = ROOT / artifact
        results.append(curve(artifact, limits=limits, sweep=CONCURRENCY_SWEEP))

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gate_b_required_trades_per_day": GATE_B_TRADES_PER_DAY,
        "sweep": list(CONCURRENCY_SWEEP),
        "deployed_caps": {"max_trades_per_day": limits.max_trades_per_day,
                          "cooldown_minutes_after_loss": limits.cooldown_minutes_after_loss,
                          "max_account_allocation_pct": limits.max_account_allocation_pct},
        "results": results,
        "both_windows_pass_at": both_windows_pass_at(results),
        "note": ("research simulation of a concurrency cap that does not exist in the deployed engine; "
                 "config.yaml untouched and no order placed. A point with K>1 also needs "
                 "`max_account_allocation_pct` raised to `allocation_cap_needed_pct`, so this lever is "
                 "two risk knobs, not one, and every per-symbol pnl here is sized off 5% of 1000"),
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "concurrency_curve.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    for row in results:
        flag = "" if row["reproduces_recorded_k1"] else "  [K=1 DOES NOT REPRODUCE]"
        print(f"\n{Path(row['artifact']).parent.name} · {row['timeframe']} · "
              f"{row['window'][0]}..{row['window'][1]} · {row['window_days']} days · "
              f"{row['symbols_replayed']} symbols · {row['candidate_trades']} candidate trades{flag}")
        print(f"  {'K':>2} {'taken':>6} {'peak':>5} {'/day':>8} {'vs 1.1322':>10} {'win%':>7} "
              f"{'PF':>6} {'net/1000':>10} {'DD/1000':>9} {'days→30':>8} {'alloc%':>7}  "
              f"skipped (overlap/cap/cooldown)")
        for point in row["curve"]:
            verdict = "PASS" if point["trades_per_day"] >= GATE_B_TRADES_PER_DAY else "short"
            print(f"  {point['max_concurrent']:>2} {point['taken']:>6} "
                  f"{point['peak_concurrent_positions']:>5} {point['trades_per_day']:>8} {verdict:>10} "
                  f"{point['win_rate_pct'] if point['win_rate_pct'] is not None else '-':>7} "
                  f"{point['profit_factor'] if point['profit_factor'] is not None else '-':>6} "
                  f"{point['net_pnl_per_1000']:>10} {point['max_drawdown_per_1000']:>9} "
                  f"{point['days_to_30_trades'] if point['days_to_30_trades'] is not None else '-':>8} "
                  f"{point['allocation_cap_needed_pct']:>7}  "
                  f"{point['skipped']['overlapping_position']}/{point['skipped']['max_trades_per_day']}/"
                  f"{point['skipped']['cooldown_after_loss']}")
    print("\nverdict — smallest K that clears 1.1322/day on EVERY window replayed for that timeframe:")
    print(json.dumps(report["both_windows_pass_at"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
