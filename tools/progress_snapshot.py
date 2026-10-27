"""Regenerate progress.json from real repository state (never from prose).

Everything written here is read back from disk or from git at run time, so a
restart can trust the file without trusting the last report. The test-suite
section is only updated by an explicit --with-tests run (or by passing
--tests-passed/--tests-seconds from a fresh run); it never invents a number.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRESS = ROOT / "progress.json"
TEST_RUNS = ROOT / "state" / "reports" / "test_runs.json"


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          check=False).stdout.strip()


def code_version() -> dict:
    raw = subprocess.run(["git", "status", "--porcelain", "-z"], cwd=ROOT,
                         capture_output=True, text=True, check=False).stdout
    dirty = [entry[3:] for entry in raw.split("\0") if len(entry) > 3]
    return {
        "head": git("rev-parse", "--short", "HEAD"),
        "committed_at": git("log", "-1", "--format=%cI"),
        "subject": git("log", "-1", "--format=%s"),
        "dirty": bool(dirty),
        "dirty_files": dirty,
    }


def experiment_log() -> dict:
    path = ROOT / "EXPERIMENT_LOG.csv"
    if not path.exists():
        return {"csv_rows": 0}
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    last = rows[-1] if rows else {}
    keep = ("run_id", "strategy", "timeframe", "slice", "net_return_pct",
            "excess_return_pct", "status")
    return {
        "csv_rows": len(rows),
        "last_row": {key: last.get(key) for key in keep} if last else None,
        "runs_with_window": sum(1 for row in rows if row.get("window_start")),
    }


def nested_walk_forwards() -> list[dict]:
    out = []
    for path in sorted((ROOT / "state" / "quant" / "results").glob("*/nested-wf-*.json")):
        if path.name.endswith(".partial.json") or path.name.endswith(".trades.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data.get("folds"), list) or "aggregate" not in data:
            continue
        agg = data.get("aggregate") or {}
        net, bench = agg.get("net_return_pct_sum"), agg.get("benchmark_return_pct_sum")
        excess = (round(float(net) - float(bench), 4)
                  if isinstance(net, (int, float)) and isinstance(bench, (int, float)) else None)
        decision = data.get("decision") or {}
        rejections: dict[str, int] = {}
        for fold in data.get("folds") or []:
            for reason, count in ((fold.get("metrics") or {}).get("rejections") or {}).items():
                rejections[reason] = rejections.get(reason, 0) + int(count)
        out.append({
            "id": data.get("run_id"),
            "dir": path.parent.name,
            "code_version": data.get("code_version"),
            "window": (data.get("window") or {}).get("start"),
            "window_end": (data.get("window") or {}).get("end"),
            "final_test_read": data.get("final_test_read"),
            "decision": decision.get("decision"),
            "reason": (decision.get("reason") or "")[:220],
            "trades": agg.get("trades"),
            "net_sum_pct": net,
            "benchmark_sum_pct": bench,
            "excess_pp": excess,
            "positive_fold_pct": agg.get("positive_fold_pct"),
            "profit_factor": agg.get("profit_factor_mean"),
            "max_drawdown_pct": agg.get("max_drawdown_pct"),
            "rejections": dict(sorted(rejections.items())),
        })
    return out


def acceptance_tally(runs: list[dict]) -> dict:
    """Count verdicts over *distinct results*, not over JSON files.

    A replay (determinism check, the same window re-run under a new flag) writes
    another directory with the same aggregate numbers, so counting files would
    turn one accepted verdict into three and move the acceptance rate.  Two runs
    are the same verdict only when window, end, trade count, net and profit
    factor all match; every run id and directory behind a group is listed, so
    collapsing never hides a file.  An accepted verdict is still not a registered
    candidate — that decision lives in DECISIONS.md, and this file does not claim it.
    """
    decided = [row for row in runs if row["decision"] in ("accepted", "rejected")]

    def key(row: dict) -> tuple:
        return (row["window"], row["window_end"], row["trades"], row["net_sum_pct"],
                row["profit_factor"])

    groups: dict[tuple, list[dict]] = {}
    for row in decided:
        groups.setdefault(key(row), []).append(row)
    accepted = [row for row in decided if row["decision"] == "accepted"]
    accepted_groups = [rows for rows in groups.values() if rows[0]["decision"] == "accepted"]
    return {
        "runs_with_decision": len(decided),
        "distinct_verdicts": len(groups),
        "accepted_files": len(accepted),
        "accepted_verdicts": len(accepted_groups),
        "rejected_files": len(decided) - len(accepted),
        "accepted_detail": [
            {
                "run_ids": sorted({row["id"] for row in rows}),
                "window": rows[0]["window"],
                "window_end": rows[0]["window_end"],
                "trades": rows[0]["trades"],
                "net_sum_pct": rows[0]["net_sum_pct"],
                "excess_pp": rows[0]["excess_pp"],
                "profit_factor": rows[0]["profit_factor"],
                "positive_fold_pct": rows[0]["positive_fold_pct"],
                "dirs": sorted(row["dir"] for row in rows),
            }
            for rows in accepted_groups
        ],
        "note": "an accepted verdict is not a registered candidate; see DECISIONS.md/FAILURES.md",
    }


def data_lake() -> dict:
    root = ROOT / "state" / "quant"
    files = list(root.rglob("*.parquet"))
    total = sum(f.stat().st_size for f in files)
    audit_path = root / "results" / "data_audit.json"
    audit = None
    if audit_path.exists():
        try:
            raw = json.loads(audit_path.read_text(encoding="utf-8"))
            audit = {key: raw.get(key) for key in ("status", "rows", "symbols", "months",
                                                  "digest", "generated_at") if key in raw}
        except (OSError, json.JSONDecodeError):
            pass
    return {"parquet_files": len(files), "bytes": total,
            "gb": round(total / 1e9, 3), "audit": audit}


def gate_b() -> dict:
    out = {}
    for name, rel in (("server", "state/reports/gate_b_evidence_server.json"),
                      ("local", "state/reports/gate_b_evidence.json")):
        path = ROOT / rel
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        win = raw.get("window") or {}
        cadence = raw.get("cadence") or {}
        funnel = raw.get("decision_funnel") or {}
        top_reason = next(iter(funnel.get("by_reason_pct") or {}), None)
        out[name] = {
            "status": raw.get("status"),
            "span_days": win.get("span_days"),
            "min_days": raw.get("min_days"),
            "on_time_fraction": win.get("on_time_fraction"),
            "cycles_observed": win.get("cycles_observed"),
            "downtime_gaps": win.get("downtime_gaps"),
            "days_remaining": win.get("days_remaining"),
            "milestone_at": win.get("milestone_at"),
            "closed_trades": (raw.get("ledger") or {}).get("trades"),
            "trade_cadence_state": cadence.get("state"),
            "required_trades_per_day": cadence.get("required_trades_per_day"),
            "decision_symbol_cycles": funnel.get("decisions"),
            "top_decision_reason": top_reason,
            "top_decision_reason_pct": (funnel.get("by_reason_pct") or {}).get(top_reason),
            "executed_decisions": sum((funnel.get("executed") or {}).values()),
            "evaluated_at": raw.get("evaluated_at"),
            "database": raw.get("database"),
            "source_file": rel,
        }
    return out


def server_snapshot() -> dict:
    path = ROOT / "state" / "reports" / "server_platform_snapshot.sqlite3"
    info = {"file": str(path.relative_to(ROOT)), "exists": path.exists()}
    if not path.exists():
        return info
    info["bytes"] = path.stat().st_size
    try:
        con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table' order by name")]
        info["tables"] = {name: con.execute(f"select count(*) from {name}").fetchone()[0]
                          for name in ("datasets", "decisions", "portfolio_snapshots", "logs")}
        con.close()
    except sqlite3.Error as exc:
        info["error"] = str(exc)
    return info


def run_tests() -> dict:
    started = time.time()
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT,
                          capture_output=True, text=True)
    elapsed = round(time.time() - started, 2)
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    match = re.search(r"(\d+) passed", proc.stdout or "")
    record = {
        "at": now_iso(),
        "command": "python -m pytest -q",
        "passed": int(match.group(1)) if match else None,
        "exit_code": proc.returncode,
        "seconds": elapsed,
        "tail": tail,
    }
    history = []
    if TEST_RUNS.exists():
        try:
            history = json.loads(TEST_RUNS.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            history = []
    history.append(record)
    TEST_RUNS.parent.mkdir(parents=True, exist_ok=True)
    TEST_RUNS.write_text(json.dumps(history[-20:], indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    return record


def last_tests() -> dict | None:
    if not TEST_RUNS.exists():
        return None
    try:
        history = json.loads(TEST_RUNS.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return history[-1] if history else None


def build(tests: dict | None) -> dict:
    wf = nested_walk_forwards()
    return {
        "generated_at": now_iso(),
        "code_version": code_version(),
        "tests": {"last_run": tests or last_tests()},
        "experiments": {
            "log": experiment_log(),
            "nested_walk_forwards": wf,
            "acceptance": acceptance_tally(wf),
        },
        "data_lake": data_lake(),
        "gates": {"B_paper": gate_b()},
        "server_snapshot": server_snapshot(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(PROGRESS))
    parser.add_argument("--with-tests", action="store_true",
                        help="run the full pytest suite and record it in progress.json")
    args = parser.parse_args()

    tests = run_tests() if args.with_tests else None
    payload = build(tests)
    Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    tally = payload["experiments"]["acceptance"]
    print(f"progress.json written: {args.out}")
    print(f"  head={payload['code_version']['head']} dirty={payload['code_version']['dirty']}")
    print(f"  tests={payload['tests']['last_run'] and payload['tests']['last_run'].get('passed')}")
    print(f"  acceptance={tally['accepted_verdicts']} accepted verdict(s) of "
          f"{tally['distinct_verdicts']} distinct ({tally['runs_with_decision']} result files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
