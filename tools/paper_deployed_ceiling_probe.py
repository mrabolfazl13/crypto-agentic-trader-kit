"""D-033: what is the one-slot cadence ceiling on the path that actually runs? (read-only)

D-032 proved the backtest (`src/cat/quant/sim.py`) force-closes at 24 hours while the deployed
loop does not (`strategy.py:253` guarded by `max_hold_bars > 0`, which is 0), and left the
*magnitude* unmeasured. It also never noticed that the six cadence cells gate B is argued from
were never produced by `sim.py`: `tools/paper_frequency_replay.py` drives `cat.backtest.run_backtest`
with the deployed `propose`, so their per-trade timelines already are the deployment-faithful
holding distribution — uncensored. This tool reads those timelines and answers the question
D-031/D-032 could only bound.

Nothing here writes a config, relaxes a threshold or places an order. It refuses (exit 3) when a
cell's recorded protocol is not the one this analysis claims to reuse.

Exit codes: 0 = measured, 3 = inputs inconsistent with the pre-registered protocol.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cat.strategies import default_params  # noqa: E402

EXIT = {"MEASURED": 0, "FAIL": 3}
TRUNCATION_REASON = "end_of_test"
DAY_HOURS = 24.0
MATERIAL_CEILING_SHIFT = 0.20  # pre-registered: a ceiling move under this is not "material"
TIMEFRAMES_OF_INTEREST = ("4h", "1h", "15m")


def timeline_of(per_symbol: list[dict]) -> list[dict]:
    """Every trade of every symbol, as one pooled observation set (one trade = one row)."""
    trades: list[dict] = []
    for row in per_symbol:
        for trade in row.get("trade_timeline") or []:
            trades.append({**trade, "symbol": row.get("symbol")})
    return trades


def hold_hours(trades: list[dict]) -> list[float]:
    return [(float(t["exit_time"]) - float(t["entry_time"])) / 3600.0 for t in trades]


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def hold_stats(trades: list[dict]) -> dict:
    """The estimator defined in the D-033 pre-registration: trade-weighted, never symbol-weighted."""
    hours = hold_hours(trades)
    if not hours:
        return {"trades": 0, "mean_hours": None, "median_hours": None, "p75_hours": None,
                "p90_hours": None, "max_hours": None, "share_over_24h_pct": None,
                "ceiling_per_day_at_mean": None, "ceiling_per_day_at_median": None}
    return {
        "trades": len(hours),
        "mean_hours": round(statistics.fmean(hours), 3),
        "median_hours": round(statistics.median(hours), 3),
        "p75_hours": _quantile(hours, 0.75),
        "p90_hours": _quantile(hours, 0.90),
        "max_hours": round(max(hours), 3),
        "share_over_24h_pct": round(100.0 * sum(1 for h in hours if h > DAY_HOURS) / len(hours), 3),
        "ceiling_per_day_at_mean": round(DAY_HOURS / statistics.fmean(hours), 4),
        "ceiling_per_day_at_median": round(DAY_HOURS / statistics.median(hours), 4),
    }


def reason_census(trades: list[dict]) -> dict:
    reasons: dict[str, int] = {}
    for trade in trades:
        key = str(trade.get("reason") or "unrecorded")
        reasons[key] = reasons.get(key, 0) + 1
    total = len(trades) or 1
    return {"counts": dict(sorted(reasons.items(), key=lambda item: -item[1])),
            "truncation_share_pct": round(100.0 * reasons.get(TRUNCATION_REASON, 0) / total, 3)}


def protocol_guard(summary: Mapping[str, Any], *, expect_hold_bars: int,
                   universe_digest: str) -> list[str]:
    """Refuse to reuse a cell whose recorded protocol differs from the pre-registered one."""
    problems: list[str] = []
    classifier = summary.get("classifier") or {}
    expected = replace(default_params(), max_hold_bars=expect_hold_bars).as_dict()
    mismatched = {k: (classifier.get(k), v) for k, v in expected.items() if classifier.get(k) != v}
    if mismatched:
        problems.append(f"classifier differs from the shipped build: {sorted(mismatched)}")
    if summary.get("cadence") != "production":
        problems.append(f"cadence = {summary.get('cadence')!r}, expected 'production'")
    if summary.get("window_bars") != 300:
        problems.append(f"window_bars = {summary.get('window_bars')!r}, expected 300")
    digest = (summary.get("lake_version_read") or {}).get("universe")
    if digest != universe_digest:
        problems.append(f"universe digest = {digest!r}, expected {universe_digest!r}")
    return problems


def verdict(ceiling_per_day: float | None, required: float) -> str:
    if ceiling_per_day is None:
        return "no-observations"
    return "reachable" if ceiling_per_day >= required else "unreachable"


def gap_attribution(stats: Mapping[str, Any], measured_per_day: float | None) -> str:
    """Is the deployed rate short because of holding time or because entries never come?"""
    ceiling = stats.get("ceiling_per_day_at_mean")
    if ceiling is None or measured_per_day is None:
        return "undetermined"
    return ("hold-bound" if measured_per_day >= 0.8 * ceiling else "entry-scarce")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def source_name(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def cell_from(path: Path, *, expect_hold_bars: int, universe_digest: str) -> dict:
    payload = read_json(path)
    summary = payload.get("summary") or {}
    problems = protocol_guard(summary, expect_hold_bars=expect_hold_bars, universe_digest=universe_digest)
    if problems:
        raise SystemExit(f"{path.name}: not the pre-registered protocol — {'; '.join(problems)}")
    trades = timeline_of(payload.get("per_symbol") or [])
    kept = [t for t in trades if str(t.get("reason")) != TRUNCATION_REASON]
    stats = hold_stats(trades)
    schedule = summary.get("single_account_schedule") or {}
    measured = schedule.get("trades_per_day")
    return {
        "source": source_name(path),
        "timeframe": summary.get("timeframe"),
        "counted_from": summary.get("counted_from"),
        "counted_to": summary.get("counted_to"),
        "symbols_replayed": summary.get("symbols_replayed"),
        "classifier_label": summary.get("classifier", {}).get("label"),
        "max_hold_bars": summary.get("classifier", {}).get("max_hold_bars"),
        "holding": stats,
        "holding_without_window_truncation": hold_stats(kept),
        "exit_reasons": reason_census(trades),
        "measured_trades_per_day_serial": measured,
        "serial_schedule": {k: schedule.get(k) for k in
                           ("max_concurrent", "taken", "trades_per_day", "skipped", "profit_factor",
                            "net_pnl_per_1000", "max_drawdown_per_1000", "days_to_30_trades")},
        "pooled_trades_per_day_upper_bound": summary.get("pooled_trades_per_day_upper_bound"),
        "gap_attribution": gap_attribution(stats, measured),
    }


def h1_verdict(cells: Mapping[str, dict], required: float) -> dict:
    """H1: the deployed 4h hold distribution is not the censored sim one (mean > 24h, both windows)."""
    four = {name: cell for name, cell in cells.items() if cell["timeframe"] == "4h"}
    per_cell = {name: {"mean_hours": cell["holding"]["mean_hours"],
                       "over_24h_both_ways": bool(cell["holding"]["mean_hours"]
                                                  and cell["holding"]["mean_hours"] > DAY_HOURS)}
                for name, cell in sorted(four.items())}
    return {"4h_cells": per_cell,
            "confirmed": bool(per_cell) and all(item["over_24h_both_ways"] for item in per_cell.values()),
            "required_per_day": required}


def h2_verdict(cells: Mapping[str, dict], required: float) -> dict:
    per_cell = {name: {"ceiling_per_day": cell["holding"]["ceiling_per_day_at_mean"],
                       "ceiling_per_day_at_median": cell["holding"]["ceiling_per_day_at_median"],
                       "verdict_at_median": verdict(cell["holding"]["ceiling_per_day_at_median"], required),
                       "trades_observed": cell["holding"]["trades"],
                       "ceiling_without_truncation": cell["holding_without_window_truncation"]["ceiling_per_day_at_mean"],
                       "verdict_at_mean": verdict(cell["holding"]["ceiling_per_day_at_mean"], required),
                       "verdict_without_truncation": verdict(
                           cell["holding_without_window_truncation"]["ceiling_per_day_at_mean"], required),
                       "measured_per_day": cell["measured_trades_per_day_serial"],
                       "gap_attribution": cell["gap_attribution"]}
                for name, cell in sorted(cells.items()) if cell["timeframe"] == "4h"}
    unreachable = [name for name, item in per_cell.items() if item["verdict_at_mean"] == "unreachable"]
    return {"required_per_day": required, "per_cell": per_cell,
            "unreachable_in_every_4h_cell": bool(per_cell) and len(unreachable) == len(per_cell),
            "material_against_sim_ceiling_rule": None}


def cap_comparison(shipped: Mapping[str, dict], capped: Mapping[str, dict], required: float) -> dict:
    """H3's pre-registered rule: recommend the cap only if BOTH windows clear all three clauses."""
    rows: dict[str, dict] = {}
    all_pass = True
    for name in sorted(capped):
        base, alt = shipped[name], capped[name]
        base_sched, alt_sched = base["serial_schedule"], alt["serial_schedule"]
        clauses = {
            "cadence_clears_requirement": bool((alt_sched.get("trades_per_day") or 0) >= required),
            "profit_factor_at_least_1": bool((alt_sched.get("profit_factor") or -1) >= 1.0),
            "net_positive": bool((alt_sched.get("net_pnl_per_1000") or -1) > 0),
        }
        all_pass = all_pass and all(clauses.values())
        rows[name] = {
            "clauses": clauses,
            "trades_per_day": {"shipped_no_cap": base_sched.get("trades_per_day"),
                               "cap_24h": alt_sched.get("trades_per_day")},
            "profit_factor": {"shipped_no_cap": base_sched.get("profit_factor"),
                              "cap_24h": alt_sched.get("profit_factor")},
            "net_pnl_per_1000": {"shipped_no_cap": base_sched.get("net_pnl_per_1000"),
                                 "cap_24h": alt_sched.get("net_pnl_per_1000")},
            "max_drawdown_per_1000": {"shipped_no_cap": base_sched.get("max_drawdown_per_1000"),
                                      "cap_24h": alt_sched.get("max_drawdown_per_1000")},
            "trades": {"shipped_no_cap": base["holding"]["trades"], "cap_24h": alt["holding"]["trades"]},
            "mean_hold_hours": {"shipped_no_cap": base["holding"]["mean_hours"],
                                "cap_24h": alt["holding"]["mean_hours"]},
            "cap_actually_fired": (alt["exit_reasons"]["counts"].get("exit_horizon", 0)
                                   + alt["exit_reasons"]["counts"].get("horizon", 0)),
            "exit_reasons_cap": alt["exit_reasons"]["counts"],
        }
    return {"cells": rows, "recommend_cap": all_pass and bool(rows),
            "rule": "cadence >= required AND PF >= 1.0 AND net > 0 in every capped cell"}


def material_shift(sim_ceiling: float | None, deployed_ceiling: float | None) -> bool | None:
    if not sim_ceiling or deployed_ceiling is None:
        return None
    return abs(deployed_ceiling - sim_ceiling) / sim_ceiling >= MATERIAL_CEILING_SHIFT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--breadth-dir", default=str(ROOT / "state/quant/results/allowlist_breadth"))
    parser.add_argument("--capped-dir", default=None,
                        help="directory of the max_hold_bars=6 replays (H3); absent => H3 not measured")
    parser.add_argument("--gate-b", default=str(ROOT / "state/reports/gate_b_evidence_server.json"))
    parser.add_argument("--d030", default=str(ROOT / "state/quant/results/d030_slots/slot_reachability.json"))
    parser.add_argument("--d031", default=str(ROOT / "state/quant/results/d031_hold/hold_distribution.json"))
    parser.add_argument("--universe-digest", default="cf828076fde8f347")
    parser.add_argument("--out", default=str(ROOT / "state/quant/results/d033_deployed_ceiling/deployed_ceiling.json"))
    args = parser.parse_args(argv)

    breadth = Path(args.breadth_dir)
    gate_b = read_json(Path(args.gate_b))
    required = float(gate_b["cadence"]["required_trades_per_day"])
    d030 = read_json(Path(args.d030))
    d031 = read_json(Path(args.d031))

    cells: dict[str, dict] = {}
    for path in sorted(breadth.glob("alw_*/frequency-*.json")):
        label = path.parent.name.removeprefix("alw_")
        cells[label] = cell_from(path, expect_hold_bars=0, universe_digest=args.universe_digest)

    deployed_4h = {name: cell["holding"]["ceiling_per_day_at_mean"]
                   for name, cell in cells.items() if cell["timeframe"] == "4h"}
    sim_ceiling = (d031.get("one_slot_ceiling") or {}).get("at_pooled_mean_per_day")
    h2 = h2_verdict(cells, required)
    h2["material_against_sim_ceiling_rule"] = {
        "sim_path_ceiling_per_day": sim_ceiling,
        "deployed_4h_ceilings": deployed_4h,
        "materiality_rule": f"relative shift >= {MATERIAL_CEILING_SHIFT}",
    }
    for name, value in deployed_4h.items():
        h2["material_against_sim_ceiling_rule"][f"material_in_{name}"] = material_shift(sim_ceiling, value)

    out: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "required_per_day": required,
        "max_hold_days_to_clear_with_one_slot": (d030.get("cadence_ceiling") or {}).get(
            "max_hold_days_to_clear_with_one_slot"),
        "provenance": {
            "gate_b": {"file": str(Path(args.gate_b)), "evaluated_at": gate_b.get("evaluated_at"),
                       "closed_trades": (gate_b.get("cadence") or {}).get("closed_trades"),
                       "status": gate_b.get("status")},
            "d030_slots": str(Path(args.d030)), "d031_hold": str(Path(args.d031)),
            "cells_dir": str(breadth), "universe_digest": args.universe_digest,
            "read_only": True,
        },
        "cells": cells,
        "h1": h1_verdict(cells, required),
        "h2": h2,
        "sim_path_contrast": {
            "n_trades": (d031.get("pooled") or {}).get("n"),
            "mean_days": (d031.get("pooled") or {}).get("mean_days"),
            "mean_hours": round((d031["pooled"]["mean_days"]) * 24.0, 3) if d031.get("pooled") else None,
            "censored_share_pct": round(100.0 * d031["censoring"]["n_censored"]
                                        / (d031["censoring"]["n_censored"] + d031["censoring"]["n_uncensored"]), 3)
            if d031.get("censoring") else None,
            "ceiling_per_day": sim_ceiling,
            "note": "this is the *backtest* path (sim.py 24h cap); D-033 measures the deployed path directly",
        },
    }

    if args.capped_dir:
        capped_dir = Path(args.capped_dir)
        capped: dict[str, dict] = {}
        for path in sorted(capped_dir.glob("alw_*/frequency-*.json")):
            label = path.parent.name.removeprefix("alw_").removesuffix("_hold6")
            capped[label] = cell_from(path, expect_hold_bars=6, universe_digest=args.universe_digest)
        missing = sorted(set(capped) - set(cells))
        if missing:
            raise SystemExit(f"capped cells have no shipped twin: {missing}")
        out["h3_cap_experiment"] = cap_comparison({k: cells[k] for k in capped}, capped, required)
        out["h3_cap_cells"] = capped

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"required_per_day": required,
                      "h1_confirmed": out["h1"]["confirmed"],
                      "h2_unreachable_everywhere": out["h2"]["unreachable_in_every_4h_cell"],
                      "ceilings": deployed_4h,
                      "sim_ceiling": sim_ceiling,
                      "h3": out.get("h3_cap_experiment", {}).get("recommend_cap")},
                     ensure_ascii=False))
    return EXIT["MEASURED"]


if __name__ == "__main__":
    sys.exit(main())
