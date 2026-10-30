"""Measure the per-day average return every research verdict actually claims (D-036, read-only).

The owner asked for a script with a positive daily average. AGENTS.md forbids promising profit,
so this answers with a number instead: for each scored walk-forward fold,
``net_per_day = net_return_pct / (test.end - test.start)``, net of the fees and slippage the
engine already charges, and the same denominator for the cost-inclusive buy & hold benchmark.

A run is reported as a positive answer only when all four pre-registered clauses hold
(DECISIONS.md, D-036). Two protocol labels that ran the identical folds are still ONE
computation (F-042), so the sample is counted by fold signature, never by file name.
The same measure is then read on the other two recorded sources with their own ceilings: the
cadence replay on the deployed exit path (in-sample, foldless - context, never the claim) and the
PAPER ledger (n observations, reported as an observation).
Nothing here changes ``acceptance()``: it is a reporting measure, not an acceptance gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cat.config import ConfigError, load_config  # noqa: E402

MIN_TEST_DAYS = 28.0
MIN_DISTINCT_FOLDS = 4
BENCHMARK_MARGIN_PP = 0.0

RUN_PREFIXES = ("nested-wf-", "nested_wf-")
EXCLUDED_SUFFIXES = (".trades.json", ".partial.json")

CLAUSES = {
    "positive_mean": "sum(net_return_pct) / sum(test_days) > 0",
    "sample": f"sum(test_days) >= {MIN_TEST_DAYS} over at least {MIN_DISTINCT_FOLDS} distinct folds",
    "beats_benchmark": f"strategy per-day > benchmark per-day + {BENCHMARK_MARGIN_PP}pp on the same denominator",
    "path_labelled": "the run records the exit cap it executed under (D-034/H3)",
}


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def seconds_between(start: str, end: str) -> float:
    return (parse_ts(end) - parse_ts(start)).total_seconds()


def fold_daily(fold: dict) -> dict | None:
    """One fold's per-day net, or None when the record cannot carry the measure at all."""
    test = fold.get("test") or {}
    metrics = fold.get("metrics") or {}
    if not test.get("start") or not test.get("end") or "net_return_pct" not in metrics:
        return None
    days = seconds_between(test["start"], test["end"]) / 86400.0
    if days <= 0:
        return None
    benchmark = fold.get("benchmark") or {}
    bench_ret = benchmark.get("return_pct")
    net = float(metrics["net_return_pct"])
    return {
        "fold": fold.get("fold"),
        "test_start": test["start"],
        "test_end": test["end"],
        "days": round(days, 4),
        "trades": int(metrics.get("trades") or 0),
        "net_return_pct": net,
        "net_per_day_pct": round(net / days, 6),
        "benchmark_return_pct": bench_ret,
        "benchmark_net_per_day_pct": round(float(bench_ret) / days, 6) if bench_ret is not None else None,
    }


def fold_signature(rows: list[dict]) -> str:
    """Identify the computation behind a run: the scored folds and what they produced."""
    payload = json.dumps([[r["test_start"], r["test_end"], r["net_return_pct"], r["trades"]] for r in rows])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def strategy_family(run_id: str | None) -> str | None:
    """`nested-wf-ema_trend-4h-train_breadth-hold720h` -> `ema_trend-4h`.

    Protocol labels (symbol selection, hold cap) name the same strategy, so cross-window evidence
    must be counted per family or one strategy looks like four.
    """
    if not run_id:
        return None
    name = run_id
    for prefix in RUN_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    for label in ("-train_breadth",):
        name = name.replace(label, "")
    return re.sub(r"-hold\d+(\.\d+)?h$", "", name)


def window_key(run: dict) -> str:
    window = run.get("window") or {}
    return f"{window.get('start')}..{window.get('end')}"


def is_run_file(path: Path) -> bool:
    name = path.name
    if any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
        return False
    return any(name.startswith(prefix) for prefix in RUN_PREFIXES)


def scan_runs(results_root: Path) -> list[Path]:
    found: set[Path] = set()
    for pattern in ("nested-wf-*.json", "nested_wf-*.json"):
        found.update(results_root.rglob(pattern))
    return sorted(path for path in found if is_run_file(path))


def summarise_run(path: Path) -> dict[str, Any]:
    run = json.loads(path.read_text(encoding="utf-8"))
    rows = [row for row in (fold_daily(fold) for fold in run.get("folds") or []) if row is not None]
    skipped = len(run.get("folds") or []) - len(rows)
    protocol = run.get("protocol") or {}
    decision = run.get("decision") or {}
    verdict = decision.get("decision") if isinstance(decision, dict) else decision

    payload: dict[str, Any] = {
        "file": str(path),
        "run_id": run.get("run_id"),
        "verdict": verdict,
        "window": run.get("window"),
        "final_test_read": run.get("final_test_read"),
        "max_hold_hours": run.get("max_hold_hours", protocol.get("max_hold_hours")),
        "code_version": run.get("code_version"),
        "folds_scored": len(rows),
        "folds_unreadable": skipped,
        "folds": rows,
        "signature": fold_signature(rows) if rows else None,
    }
    if not rows:
        payload["measurable"] = False
        payload["reason"] = "no fold carries both a test window and net_return_pct"
        return payload

    total_days = sum(row["days"] for row in rows)
    total_net = sum(row["net_return_pct"] for row in rows)
    bench_days = [row for row in rows if row["benchmark_return_pct"] is not None]
    total_bench = sum(row["benchmark_return_pct"] for row in bench_days)
    per_day = total_net / total_days
    bench_per_day = (total_bench / sum(row["days"] for row in bench_days)) if bench_days else None
    distinct_days = sorted({(row["test_start"], row["test_end"], row["days"]) for row in rows})
    payload.update({
        "measurable": True,
        "test_days_total": round(total_days, 4),
        "net_return_pct_sum": round(total_net, 6),
        "net_per_day_pct_weighted": round(per_day, 6),
        "net_per_day_pct_mean": round(sum(row["net_per_day_pct"] for row in rows) / len(rows), 6),
        "positive_folds_pct": round(100.0 * sum(1 for row in rows if row["net_return_pct"] > 0) / len(rows), 4),
        "benchmark_return_pct_sum": round(total_bench, 6),
        "benchmark_net_per_day_pct": round(bench_per_day, 6) if bench_per_day is not None else None,
        "distinct_test_windows": len(distinct_days),
        "clauses": {
            "positive_mean": per_day > 0,
            "sample": total_days >= MIN_TEST_DAYS and len(rows) >= MIN_DISTINCT_FOLDS,
            "beats_benchmark": bench_per_day is not None and per_day > bench_per_day + BENCHMARK_MARGIN_PP,
            "path_labelled": payload["max_hold_hours"] is not None,
        },
    })
    payload["positive_daily_average_claim_supported"] = all(payload["clauses"].values())
    payload["failed_clauses"] = [name for name, ok in payload["clauses"].items() if not ok]
    return payload


def paper_ledger(gate_b_path: Path, capital_usdt: float | None) -> dict[str, Any]:
    """The deployed book is one observation, not an average: report it, never average it."""
    if not gate_b_path.exists():
        return {"source": "gate_b", "available": False, "reason": f"{gate_b_path} not found"}
    evidence = json.loads(gate_b_path.read_text(encoding="utf-8"))
    window = evidence.get("window") or {}
    ledger = evidence.get("ledger") or {}
    cadence = evidence.get("cadence") or {}
    days = window.get("span_days")
    net = ledger.get("net_pnl")
    payload: dict[str, Any] = {
        "source": "gate_b",
        "available": True,
        "evaluated_at": evidence.get("evaluated_at"),
        "status": evidence.get("status"),
        "window_days": days,
        "closed_trades": ledger.get("trades"),
        "realized_net_usdt": net,
        "fees_usdt": ledger.get("fees"),
        "observed_trades_per_day": cadence.get("observed_trades_per_day"),
        "required_trades_per_day": cadence.get("required_trades_per_day"),
    }
    if isinstance(days, (int, float)) and days > 0 and isinstance(net, (int, float)) and capital_usdt:
        payload["allocated_capital_usdt"] = capital_usdt
        payload["realized_net_pct_per_day_on_allocated_capital"] = round(100.0 * net / capital_usdt / days, 6)
    payload["average_claimable"] = bool(isinstance(ledger.get("trades"), int)
                                        and ledger["trades"] >= MIN_DISTINCT_FOLDS)
    payload["note"] = ("n=%s closed trade(s): one observation cannot answer a mean; the number is the "
                       "realised P&L of that single trade divided by the elapsed window, on the "
                       "%s USDT allocated capital" % (ledger.get("trades"), capital_usdt))
    return payload


def net_pct_per_day(net_usdt: Any, days: Any, capital_usdt: float | None) -> float | None:
    """Percent-of-allocated-capital per day, or None when any input is missing."""
    if not isinstance(net_usdt, (int, float)) or not isinstance(days, (int, float)) or days <= 0:
        return None
    if not capital_usdt:
        return None
    return round(100.0 * net_usdt / capital_usdt / days, 6)


def cell_signature(row: dict) -> str:
    """Identify a replay outcome by what it recorded, not by the file that holds it.

    Two universes that produce byte-identical trades are ONE observation (F-042 again); counting the
    label would make one positive cell look like two.
    """
    payload = json.dumps([row.get("timeframe"), row.get("counted_from"), row.get("counted_to"),
                          row.get("counted_days_per_symbol"), row.get("serial_trades"),
                          row.get("serial_trades_per_day"), row.get("profit_factor"),
                          row.get("net_pnl_per_1000")])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def deployed_path_replay(compare_path: Path, capital_usdt: float | None) -> dict[str, Any]:
    """Source (2) of the pre-registered measure: the cadence replay on the path that runs.

    These are the only recorded crypto numbers produced by the *deployed* exit structure
    (`max_hold_bars = 0`, so no 24h time exit; D-032/D-034). They are in-sample replays of one
    fixed window with no train/test split, so they can never satisfy the fold clause of the
    pre-registered rule; they are reported as an observation with its own ceiling.
    """
    if not compare_path.exists():
        return {"source": str(compare_path), "available": False, "reason": "replay artifact not found"}
    compare = json.loads(compare_path.read_text(encoding="utf-8"))
    rows = compare.get("rows") or {}

    cells: list[dict[str, Any]] = []
    for cell_id, row in sorted(rows.items()):
        try:
            classifier = json.loads(row.get("classifier_key") or "{}")
        except json.JSONDecodeError:
            classifier = {}
        max_hold_bars = classifier.get("max_hold_bars")
        days = row.get("counted_days_per_symbol")
        net = row.get("net_pnl_per_1000")
        cells.append({
            "cell": cell_id,
            "universe_file": row.get("universe_file"),
            "lake_symbols": row.get("lake_symbols"),
            "timeframe": row.get("timeframe"),
            "cadence": row.get("cadence"),
            "window": f"{row.get('counted_from')}..{row.get('counted_to')}",
            "window_days": days,
            "serial_trades": row.get("serial_trades"),
            "serial_trades_per_day": row.get("serial_trades_per_day"),
            "profit_factor": row.get("profit_factor"),
            "net_usdt_on_account": net,
            "net_pct_of_capital": (round(100.0 * net / capital_usdt, 6)
                                   if isinstance(net, (int, float)) and capital_usdt else None),
            "net_per_day_pct": net_pct_per_day(net, days, capital_usdt),
            "max_hold_bars": max_hold_bars,
            "time_exit_in_deployment": bool(max_hold_bars),
            "signature": cell_signature(row),
        })

    per_day = [cell["net_per_day_pct"] for cell in cells if cell["net_per_day_pct"] is not None]
    positive_signatures = {cell["signature"] for cell in cells
                           if (cell["net_per_day_pct"] or 0.0) > 0}
    payload: dict[str, Any] = {
        "source": str(compare_path),
        "available": bool(cells),
        "protocol": ("in-sample replay of the shipped build over one fixed window, one account, "
                     "serial schedule at the deployed cycle clock - no train/test split, no folds"),
        "path": "deployed (exit cap read from classifier_key.max_hold_bars)",
        "capital_basis_usdt": capital_usdt,
        "cells": cells,
        "cells_measured": len(per_day),
        "cells_positive": sum(1 for value in per_day if value > 0),
        "distinct_positive_outcomes": len(positive_signatures),
        "best_per_day_pct": max(per_day) if per_day else None,
        "worst_per_day_pct": min(per_day) if per_day else None,
        "can_support_the_d036_claim": False,
        "reason": ("the pre-registered 'sample' clause is defined over scored test folds; a foldless "
                   "in-sample replay has none, so it is context and an upper bound, never the claim"),
    }
    if capital_usdt and abs(capital_usdt - 1000.0) > 1e-9:
        payload["capital_warning"] = (f"the replay field is named per_1000 and was produced on the "
                                      f"configured allocated capital; this read uses {capital_usdt} USDT")
    if payload["cells_positive"] != payload["distinct_positive_outcomes"]:
        payload["positive_collapse_note"] = (
            f"{payload['cells_positive']} positive cell(s) collapse to "
            f"{payload['distinct_positive_outcomes']} distinct recorded outcome(s)")
    return payload


def build_report(results_root: Path, gate_b_path: Path, capital_usdt: float | None = None,
                 replay_path: Path | None = None) -> dict[str, Any]:
    runs = [summarise_run(path) for path in scan_runs(results_root)]
    measurable = [run for run in runs if run.get("measurable")]
    signatures: dict[str, list[str]] = {}
    for run in measurable:
        signatures.setdefault(run["signature"], []).append(run["run_id"] or run["file"])
    supported = [run for run in measurable if run.get("positive_daily_average_claim_supported")]
    positive_mean = [run for run in measurable if run["clauses"]["positive_mean"]]
    accepted = [run for run in measurable if run.get("verdict") == "accepted"]

    windows: dict[str, dict[str, Any]] = {}
    for run in measurable:
        key = window_key(run)
        bucket = windows.setdefault(key, {"window": run.get("window"), "runs": 0, "accepted_runs": 0,
                                         "claim_supported_runs": 0, "per_day_values": [],
                                         "benchmark_per_day": set()})
        bucket["runs"] += 1
        bucket["accepted_runs"] += int(run.get("verdict") == "accepted")
        bucket["claim_supported_runs"] += int(run.get("positive_daily_average_claim_supported"))
        bucket["per_day_values"].append(run["net_per_day_pct_weighted"])
        if run["benchmark_net_per_day_pct"] is not None:
            bucket["benchmark_per_day"].add(run["benchmark_net_per_day_pct"])
    for bucket in windows.values():
        values = bucket["per_day_values"]
        bucket["net_per_day_pct_best"] = max(values)
        bucket["net_per_day_pct_worst"] = min(values)
        bucket["benchmark_per_day"] = sorted(bucket["benchmark_per_day"])
        del bucket["per_day_values"]

    supported_signatures = {run["signature"] for run in supported}
    supported_window_keys = {window_key(run) for run in supported}
    families: dict[str, dict[str, Any]] = {}
    for run in measurable:
        family = strategy_family(run["run_id"])
        entry = families.setdefault(family, {"windows_scored": set(), "windows_claim_supported": set(),
                                             "signatures": set()})
        entry["windows_scored"].add(window_key(run))
        entry["signatures"].add(run["signature"])
        if run.get("positive_daily_average_claim_supported"):
            entry["windows_claim_supported"].add(window_key(run))
    family_summary = {
        family: {"windows_scored": len(entry["windows_scored"]),
                 "windows_claim_supported": sorted(entry["windows_claim_supported"]),
                 "distinct_computations": len(entry["signatures"]),
                 "positive_daily_average_on_every_scored_window":
                     bool(entry["windows_claim_supported"])
                     and set(entry["windows_scored"]) == set(entry["windows_claim_supported"])}
        for family, entry in sorted(families.items(), key=lambda kv: str(kv[0]))}
    cross_window_families = [family for family, row in family_summary.items()
                             if len(row["windows_claim_supported"]) >= 2]

    ledger = paper_ledger(gate_b_path, capital_usdt)
    replay = deployed_path_replay(replay_path or (results_root / "allowlist_breadth" / "compare.json"),
                                  capital_usdt)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "question": "does any recorded crypto verdict show a positive per-day average, measured as "
                    "net_return_pct over the scored test days, net of fees and slippage?",
        "measure": CLAUSES,
        "run_files_scanned": len(runs),
        "runs_measurable": len(measurable),
        "runs_unmeasurable": len(runs) - len(measurable),
        "distinct_computations": len(signatures),
        "duplicate_groups": [{"runs": names} for names in signatures.values() if len(names) > 1],
        "accepted_runs": len(accepted),
        "runs_with_positive_mean": len(positive_mean),
        "runs_supporting_the_claim": [
            {"run_id": run["run_id"], "file": run["file"], "family": strategy_family(run["run_id"]),
             "window": run["window"],
             "verdict": run.get("verdict"), "net_per_day_pct_weighted": run["net_per_day_pct_weighted"],
             "benchmark_net_per_day_pct": run["benchmark_net_per_day_pct"],
             "test_days_total": run["test_days_total"], "positive_folds_pct": run["positive_folds_pct"],
             "signature": run["signature"]} for run in supported],
        "supported_distinct_computations": len(supported_signatures),
        "supported_distinct_windows": len(supported_window_keys),
        "per_window": dict(sorted(windows.items())),
        "distinct_windows_scored": len(windows),
        "per_family": family_summary,
        "families_positive_on_two_or_more_windows": cross_window_families,
        "accepted_summary": [{
            "run_id": run["run_id"], "verdict": run.get("verdict"),
            "test_days_total": run["test_days_total"], "folds_scored": run["folds_scored"],
            "net_per_day_pct_weighted": run["net_per_day_pct_weighted"],
            "benchmark_net_per_day_pct": run["benchmark_net_per_day_pct"],
            "positive_folds_pct": run["positive_folds_pct"], "clauses": run["clauses"],
            "failed_clauses": run["failed_clauses"],
            "positive_daily_average_claim_supported": run["positive_daily_average_claim_supported"],
        } for run in accepted],
        "best_negative_context": sorted(
            [{"run_id": run["run_id"], "net_per_day_pct_weighted": run["net_per_day_pct_weighted"],
              "test_days_total": run["test_days_total"], "verdict": run.get("verdict")}
             for run in measurable],
            key=lambda row: row["net_per_day_pct_weighted"] or 0.0, reverse=True)[:10],
        "paper_ledger": ledger,
        "deployed_path_replay": replay,
        "answer": {
            "positive_daily_average_proven": bool(supported),
            "accepted_and_positive": any(run.get("verdict") == "accepted" for run in supported),
            "evidence_breadth": {"claim_supported_runs": len(supported),
                                 "distinct_computations": len(supported_signatures),
                                 "distinct_windows": len(supported_window_keys),
                                 "windows_scored_in_total": len(windows),
                                 "families_positive_on_two_or_more_windows": cross_window_families},
            "per_day_by_path": {
                "research_path_best_pct": max((run["net_per_day_pct_weighted"] for run in measurable),
                                              default=None),
                "deployed_path_best_pct": replay.get("best_per_day_pct"),
                "deployed_path_cells_positive": replay.get("cells_positive"),
                "deployed_path_distinct_positive_outcomes": replay.get("distinct_positive_outcomes"),
                "deployed_path_cells_measured": replay.get("cells_measured"),
                "paper_book_pct": ledger.get("realized_net_pct_per_day_on_allocated_capital"),
            },
            "wording_limit": "measured on closed folds of recorded runs; not a forecast and not a promise "
                             "(AGENTS.md rule 2). The backtest path carries a 24h exit the deployment "
                             "never had, so every WF number is a research-path bound (D-034/H3); "
                             "deployed_path_replay gives the same measure on the path that actually "
                             "runs and is in-sample, so it is a second bound, not a claim.",
            "single_window_warning": (len(supported_window_keys) == 1 and len(windows) > 1) if supported else None,
        },
        "read_only": True,
        "runs": runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", default="state/quant/results")
    parser.add_argument("--gate-b", default="state/reports/gate_b_evidence_server.json")
    parser.add_argument("--replay", default=None,
                        help="cadence-replay compare.json for the deployed-path source "
                             "(default: <results-root>/allowlist_breadth/compare.json)")
    parser.add_argument("--out", default="state/quant/results/d036_daily_average/daily_average.json")
    args = parser.parse_args()

    # Never hardcode the deployed account size: read it from the shipped config (F-039).
    try:
        capital = load_config(root=ROOT).allocated_capital
    except ConfigError as exc:
        capital = None
        print(f"warning: allocated capital unreadable ({exc}); reporting the paper ledger in USDT only")

    replay_path = Path(args.replay) if args.replay else None
    report = build_report(Path(args.results_root), Path(args.gate_b), capital_usdt=capital,
                          replay_path=replay_path)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"runs scanned={report['run_files_scanned']} measurable={report['runs_measurable']} "
          f"distinct={report['distinct_computations']} accepted={report['accepted_runs']} "
          f"positive_mean={report['runs_with_positive_mean']} "
          f"claim_supported={len(report['runs_supporting_the_claim'])}")
    for row in report["accepted_summary"]:
        print(f"  {row['run_id']}: verdict={row['verdict']} days={row['test_days_total']} "
              f"net/day={row['net_per_day_pct_weighted']} bh/day={row['benchmark_net_per_day_pct']} "
              f"failed={row['failed_clauses']}")
    print(f"  paper ledger: {json.dumps(report['paper_ledger'], ensure_ascii=False)[:200]}")
    replay = report["deployed_path_replay"]
    print(f"  deployed path: available={replay.get('available')} cells={replay.get('cells_measured')} "
          f"positive={replay.get('cells_positive')} best/day={replay.get('best_per_day_pct')} "
          f"worst/day={replay.get('worst_per_day_pct')}")
    print(f"answer={json.dumps(report['answer'], ensure_ascii=False)[:300]}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
