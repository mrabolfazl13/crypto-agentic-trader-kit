"""D-034: does the research verdict depend on the hold cap only the backtest enforces?

The deployment has no time exit (``strategy.py:253`` is guarded by ``max_hold_bars > 0``,
and the shipped params leave it at 0). The backtest caps a hold at
``quant.backtest.max_hold_hours`` — 24 by default, and it is a real wall-clock cap
because trades execute on stored 1m bars. ``tools/quant_nested_walk_forward.py`` now
takes ``--max-hold-hours`` so the same fold protocol can be replayed under a different
cap. This probe judges that replay and nothing else: it reads artifacts, writes only
the report under ``--out``, and never reruns anything.

Exit codes: 0 = every requested cell was measured under a valid single-variable guard,
3 = a guard failed or a cell is indeterminate, so the report must not be cited as a
verdict.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

# The only protocol keys allowed to differ between a control run and its treatment.
CAP_PROTOCOL_KEYS = {"max_hold_hours", "max_hold_hours_overridden", "max_hold_hours_configured"}
IDENTITY_FIELDS = ("code_version", "data_version", "window", "final_test_start", "final_test_read")
# Fold fields that are *inputs* to a fold. A cap contrast is only single-variable if these
# stay identical; if one of them moves, the pair measures something else.
FOLD_IDENTITY_FIELDS = ("fold", "train", "search", "selection", "test", "symbols_evaluated",
                        "search_basket", "scored_basket")
# Fold fields that are *outcomes* of running the fold. The cap is allowed to move these, and
# moving them is part of what D-034 measures: a different cap changes which trials produce
# trades, so it can change the trial count reached, the early stop, the chosen parameters and
# the basket the breadth ranker keeps.
FOLD_OUTCOME_FIELDS = ("chosen_on", "best_params", "params_label", "search_best", "finalists",
                       "trials", "stop_reason", "metrics", "benchmark", "trades", "symbol_subset",
                       "errors", "elapsed_seconds", "search_elapsed_seconds")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _differing_keys(left: dict, right: dict) -> dict[str, tuple[Any, Any]]:
    keys = set(left) | set(right)
    return {k: (left.get(k), right.get(k)) for k in sorted(keys) if left.get(k) != right.get(k)}


def _trial_count(fold: dict) -> Any:
    """Real run artifacts store the trial records; probe fixtures store the count.

    Either way the guard reports a number: dumping every trial of both sides into the report
    would bury the one field the cap is expected to move.
    """
    trials = fold.get("trials")
    return len(trials) if isinstance(trials, list) else trials


def protocol_guard(control: dict, treatment: dict) -> dict:
    """Prove the pair differs in the cap and in nothing else.

    A comparison that also moved the build, the data, the window or the basket is not a
    cap contrast; it would silently credit the cap for whatever else changed.
    """
    problems: list[str] = []
    for field in IDENTITY_FIELDS:
        if control.get(field) != treatment.get(field):
            problems.append(f"{field} differs: control={control.get(field)!r} "
                            f"treatment={treatment.get(field)!r}")
    differing = _differing_keys(control.get("protocol") or {}, treatment.get("protocol") or {})
    unexpected = sorted(set(differing) - CAP_PROTOCOL_KEYS)
    if unexpected:
        problems.append(f"protocol keys other than the cap differ: {unexpected}")
    cap_control = (control.get("protocol") or {}).get("max_hold_hours")
    cap_treatment = (treatment.get("protocol") or {}).get("max_hold_hours")
    if cap_control is None or cap_treatment is None:
        problems.append("a run without protocol.max_hold_hours cannot be attributed to a cap; "
                        "only runs made after the flag exists can be paired")
    elif cap_control == cap_treatment:
        problems.append(f"both runs share cap {cap_control}: this is not a cap contrast")
    folds_c, folds_t = control.get("folds") or [], treatment.get("folds") or []
    outcome_moves: dict[str, list[dict]] = {"trials": [], "chosen_on": [], "kept_basket": []}
    if len(folds_c) != len(folds_t):
        problems.append(f"fold count differs: {len(folds_c)} vs {len(folds_t)}")
    else:
        for index, (fc, ft) in enumerate(zip(folds_c, folds_t)):
            moved = _differing_keys(fc, ft)
            unclassified = sorted(set(moved) - set(FOLD_OUTCOME_FIELDS) - {"max_hold_hours"})
            if unclassified:
                problems.append(f"fold {index}: fields that are neither inputs nor declared "
                                f"outcomes differ: {unclassified}")
            for field in FOLD_IDENTITY_FIELDS:
                if fc.get(field) != ft.get(field):
                    problems.append(f"fold {index}: input field {field} differs: "
                                    f"{fc.get(field)!r} vs {ft.get(field)!r}")
            if fc.get("max_hold_hours") != cap_control or ft.get("max_hold_hours") != cap_treatment:
                problems.append(f"fold {index}: the fold does not record the run-level cap "
                                f"(fold {fc.get('max_hold_hours')}/{ft.get('max_hold_hours')}, "
                                f"run {cap_control}/{cap_treatment})")
            outcome_moves["trials"].append({
                "fold": index, "control": _trial_count(fc), "treatment": _trial_count(ft),
                # The early stop is the channel by which a cap changes the chosen parameters,
                # so the reason each side quit is part of the measurement, not noise.
                "stop_control": fc.get("stop_reason"), "stop_treatment": ft.get("stop_reason")})
            outcome_moves["chosen_on"].append({"fold": index, "control": fc.get("chosen_on"),
                                               "treatment": ft.get("chosen_on")})
            outcome_moves["kept_basket"].append({
                "fold": index,
                "control": (fc.get("symbol_subset") or {}).get("kept"),
                "treatment": (ft.get("symbol_subset") or {}).get("kept")})
    return {"ok": not problems, "problems": problems,
            "protocol_keys_differing": sorted(differing),
            "cap_control": cap_control, "cap_treatment": cap_treatment,
            "outcome_moves": outcome_moves,
            "basket_note": "the fold record stores basket *sizes* only, so equality here proves "
                           "the same number of symbols per fold, not the same symbols"}


def _gates(decision: dict) -> dict[str, bool]:
    return {name: bool(gate.get("pass")) for name, gate in (decision.get("gates") or {}).items()}


def compare_cell(name: str, control: dict, treatment: dict, *, materiality_pct: float) -> dict:
    """H1: did the verdict, or anything material about it, move when only the cap changed?"""
    agg_c, agg_t = control.get("aggregate") or {}, treatment.get("aggregate") or {}
    dec_c, dec_t = control.get("decision") or {}, treatment.get("decision") or {}
    net_c = agg_c.get("net_return_pct_sum")
    net_t = agg_t.get("net_return_pct_sum")
    relative_pct: float | None = None
    material: Any = None
    if net_c is not None and net_t is not None:
        if net_c == 0.0:
            material = None if net_t == 0.0 else True
        else:
            relative_pct = round(abs(net_t - net_c) / abs(net_c) * 100.0, 2)
            material = relative_pct >= materiality_pct or (net_c * net_t) < 0
    verdict_changed = dec_c.get("decision") != dec_t.get("decision")
    gates_c, gates_t = _gates(dec_c), _gates(dec_t)
    gate_flips = sorted(k for k in set(gates_c) | set(gates_t) if gates_c.get(k) != gates_t.get(k))
    metrics = {}
    for key in ("net_return_pct_sum", "profit_factor_mean", "max_effective_drawdown_pct",
                "trades", "positive_fold_pct", "benchmark_return_pct_sum"):
        a, b = agg_c.get(key), agg_t.get(key)
        metrics[key] = {"control": a, "treatment": b,
                        "delta": (round(b - a, 4) if isinstance(a, (int, float))
                                  and isinstance(b, (int, float)) else None)}
    return {
        "cell": name,
        "verdict_control": dec_c.get("decision"), "verdict_treatment": dec_t.get("decision"),
        "verdict_changed": verdict_changed,
        "gate_flips": gate_flips,
        "gate_details": {k: {"control": (dec_c.get("gates") or {}).get(k),
                             "treatment": (dec_t.get("gates") or {}).get(k)} for k in gate_flips},
        "net_return_pct_relative_change": relative_pct,
        "material_by_20pct_rule": material,
        "aggregates": metrics,
        # The mediated channel: the cap is fed to the search as well, so it can change which
        # parameters get chosen and how many trials run before the early stop. Recording it
        # keeps a verdict difference from being read as a pure market effect.
        "search_channel": {
            "folds": [{"fold": index,
                       "params_control": fc.get("params_label"), "params_treatment": ft.get("params_label"),
                       "params_changed": fc.get("params_label") != ft.get("params_label"),
                       "trials_control": _trial_count(fc), "trials_treatment": _trial_count(ft),
                       "stop_control": fc.get("stop_reason"), "stop_treatment": ft.get("stop_reason"),
                       "chosen_control": fc.get("chosen_on"), "chosen_treatment": ft.get("chosen_on")}
                      for index, (fc, ft) in enumerate(zip(control.get("folds") or [],
                                                            treatment.get("folds") or []))],
            "params_changed_folds": sum(1 for fc, ft in zip(control.get("folds") or [],
                                                            treatment.get("folds") or [])
                                        if fc.get("params_label") != ft.get("params_label")),
        },
        "avg_hold_hours_control": [fold.get("metrics", {}).get("avg_hold_hours")
                                   for fold in control.get("folds") or []],
        "avg_hold_hours_treatment": [fold.get("metrics", {}).get("avg_hold_hours")
                                     for fold in treatment.get("folds") or []],
        "load_bearing": bool(verdict_changed or (material is True)),
        "note": "relative change is undefined when the control sums to zero; the cell is then "
                "reported indeterminate, not as no effect",
    }


def _aggregate_signature(cell: dict) -> tuple:
    agg = cell.get("aggregates") or {}
    return tuple((agg.get(key) or {}).get("control") for key in sorted(agg))


def distinct_cells(cells: list[dict]) -> dict:
    """Count distinct computations behind the pairs, not the number of pairs.

    Two protocol labels can collapse onto the same run — the breadth ranker can fall back
    to the pre-registered universe basket, and then both cells are the same 100 symbols,
    the same seeds and the same numbers. Reporting them as two cells would double the
    apparent sample size.
    """
    measured = [c for c in cells if "aggregates" in c]
    signatures: dict[tuple, list[str]] = {}
    for cell in measured:
        signatures.setdefault(_aggregate_signature(cell), []).append(cell["cell"])
    duplicates = {key: names for key, names in signatures.items() if len(names) > 1}
    return {"pairs": len(cells), "measured_pairs": len(measured),
            "distinct_computations": len(signatures),
            "duplicate_groups": [{"cells": names} for names in duplicates.values()],
            "note": "duplicate groups share identical control aggregates, so one verdict "
                    "counts once"}


def hold_census(trades_docs: list[dict]) -> dict:
    """H2: how much of the sampled trade flow is a product of the cap rather than the market?"""
    reasons: Counter[str] = Counter()
    holds: list[float] = []
    at_cap = 0
    for doc in trades_docs:
        declared = doc.get("max_hold_hours")
        for fold in (doc.get("folds") or {}).values():
            for trade in fold:
                reasons[str(trade.get("reason"))] += 1
                span = (int(trade["exit_ts"]) - int(trade["entry_ts"])) / 3600.0
                holds.append(span)
                if declared and abs(span - float(declared)) < 1.0:
                    at_cap += 1
    total = sum(reasons.values())
    if not total:
        return {"trades": 0, "note": "no trade dump supplied: censoring is NOT MEASURED"}
    ordered = sorted(holds)
    declared_caps = sorted({float(d["max_hold_hours"]) for d in trades_docs
                            if d.get("max_hold_hours") is not None})

    def pct(q: float) -> float:
        index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        return round(ordered[index], 3)
    return {
        "trades": total,
        "declared_caps": declared_caps,
        "mixed_caps_in_one_census": len(declared_caps) > 1,
        "exit_reason_counts": dict(reasons),
        "time_exit_share_pct": round(reasons.get("time", 0) / total * 100.0, 3),
        "at_declared_cap_share_pct": round(at_cap / total * 100.0, 3),
        "mean_hold_hours": round(statistics.fmean(holds), 3),
        "median_hold_hours": pct(0.5), "p75_hold_hours": pct(0.75),
        "p90_hold_hours": pct(0.9), "max_hold_hours": round(ordered[-1], 3),
        "share_over_24h_pct": round(sum(1 for h in holds if h > 24.0) / total * 100.0, 3),
    }


def cap_label_census(results_root: Path) -> dict:
    """H3: how many recorded walk-forward verdicts can even be attributed to a cap?"""
    rows: list[dict] = []
    seen: set[Path] = set()
    patterns = ("**/nested-wf-*.json",)
    for pattern in patterns:
        for path in sorted(results_root.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            try:
                doc = read_json(path)
            except (json.JSONDecodeError, OSError) as exc:
                rows.append({"file": str(path.relative_to(results_root)), "status": f"unreadable: {exc}"})
                continue
            if not isinstance(doc, dict) or "run_id" not in doc:
                continue
            name = path.name
            kind = ("trades" if name.endswith(".trades.json")
                    else "partial" if name.endswith(".partial.json") else "run")
            protocol = doc.get("protocol") or {}
            rows.append({
                "file": str(path.relative_to(results_root)), "kind": kind,
                "run_id": doc.get("run_id"),
                "records_cap": "max_hold_hours" in protocol or "max_hold_hours" in doc,
                "cap": protocol.get("max_hold_hours", doc.get("max_hold_hours")),
                "cap_in_filename": "-hold" in str(doc.get("run_id") or ""),
                "decision": (doc.get("decision") or {}).get("decision"),
            })
    runs = [r for r in rows if r["kind"] == "run"]
    return {
        "patterns": patterns,
        "result_files": len(rows), "run_files": len(runs),
        "partial_files": sum(1 for r in rows if r["kind"] == "partial"),
        "trades_files": sum(1 for r in rows if r["kind"] == "trades"),
        "runs_recording_cap": sum(1 for r in runs if r["records_cap"]),
        "runs_without_cap_field": sum(1 for r in runs if not r["records_cap"]),
        "runs_with_cap_in_filename": sum(1 for r in runs if r["cap_in_filename"]),
        "accepted_runs": sum(1 for r in runs if r["decision"] == "accepted"),
        "rows": rows,
        "note": "older artifacts carry no cap field: they ran under the configured value, which "
                "was 24 for their whole lifetime, so they are attributable by inference and not "
                "by a recorded field",
    }


def build_report(cells: list[dict], guards: dict, censuses: dict, deployed: dict | None,
                 historical: list[dict], materiality_pct: float) -> dict:
    guard_ok = all(g["ok"] for g in guards.values()) and bool(guards)
    verdict_moves = [c["cell"] for c in cells if c["verdict_changed"]]
    material_moves = [c["cell"] for c in cells if c["material_by_20pct_rule"] is True]
    indeterminate = [c["cell"] for c in cells if c["material_by_20pct_rule"] is None]
    control_census = censuses.get("control") or {}
    treatment_census = censuses.get("treatment") or {}
    # "reason == time" conflates a cap hit with the last bar of the window, so the binding
    # test is the trade sitting at the declared cap, not the label it carries.
    control_at_cap = control_census.get("at_declared_cap_share_pct")
    treatment_at_cap = treatment_census.get("at_declared_cap_share_pct")
    cap_binds = None if control_at_cap is None else control_at_cap >= 1.0
    distinct = distinct_cells(cells)
    collapse = (f" (measured on {distinct['distinct_computations']} distinct computation(s), "
                f"not {distinct['measured_pairs']} pairs)"
                if distinct["duplicate_groups"] else "")
    conclusion = ("not measured" if not guard_ok else
                  "the cap is load-bearing for the research verdict" + collapse
                  if (verdict_moves or material_moves) else
                  "no recorded verdict moved when only the cap changed" + collapse
                  + ("" if not indeterminate else f"; indeterminate cells: {indeterminate}"))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "question": "do accepted walk-forward verdicts depend on the 24h hold cap that only "
                    "the backtest enforces (D-032: the deployed path has no time exit)?",
        "materiality_pct": materiality_pct,
        "protocol_guards": guards,
        "all_guards_pass": guard_ok,
        "cells": cells,
        "h1": {
            "verdict_changed_cells": verdict_moves,
            "material_cells": material_moves,
            "indeterminate_cells": indeterminate,
            "distinct": distinct,
            "rule": "load-bearing iff a recorded verdict flips, or |Δ net_return_pct_sum| / "
                    f"|control| >= {materiality_pct}% in a guarded pair",
        },
        "h2": {
            "control_census": censuses.get("control") or {"note": "not measured"},
            "treatment_census": censuses.get("treatment") or {"note": "not measured"},
            "censoring_shares_pct": {"control_time_exits": control_census.get("time_exit_share_pct"),
                                     "treatment_time_exits": treatment_census.get("time_exit_share_pct"),
                                     "control_at_declared_cap": control_at_cap,
                                     "treatment_at_declared_cap": treatment_at_cap},
            "cap_binds_in_control": cap_binds,
            "cap_released_in_treatment": None if treatment_at_cap is None
            else treatment_at_cap < max(1.0, (control_at_cap or 0.0) / 10.0),
            "falsification": "if the control census shows ~0% of trades sitting at the declared "
                             "cap then the cap never bound the sampled flow, and D-034 is moot "
                             "regardless of the verdicts",
            "deployed_witnesses": deployed or {"note": "D-033 artifact absent: distance to the "
                                                       "deployed hold distribution is NOT MEASURED"},
        },
        "h3": censuses.get("labels") or {},
        "historical_context_not_comparable": historical,
        "conclusion": conclusion,
        "read_only": True,
    }


def deployed_witnesses(path: Path | None) -> dict | None:
    if path is None or not Path(path).exists():
        return None
    doc = read_json(path)
    cells = doc.get("cells") or {}
    out = {"source": str(path),
           "required_per_day": doc.get("required_per_day"),
           "sim_path_contrast": doc.get("sim_path_contrast")}
    for name in ("w1_4h", "w3_4h"):
        holding = (cells.get(name) or {}).get("holding") or {}
        if holding:
            out[f"{name}_mean_hold_hours"] = holding.get("mean_hours")
            out[f"{name}_share_over_24h_pct"] = holding.get("share_over_24h_pct")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell", action="append", default=[], metavar="NAME|CONTROL|TREATMENT",
                        help="a guarded pair of walk-forward artifacts, pipe separated because "
                             "Windows paths contain colons (repeatable)")
    parser.add_argument("--trades-control", action="append", default=[],
                        help="control .trades.json file(s) pooled for the censoring census (H2)")
    parser.add_argument("--trades-treatment", action="append", default=[],
                        help="treatment .trades.json file(s) pooled for the censoring census (H2)")
    parser.add_argument("--d033", default=str(ROOT / "state" / "quant" / "results"
                                              / "d033_deployed_ceiling" / "deployed_ceiling.json"),
                        help="deployed-path artifact to read hold witnesses from; absent => not measured")
    parser.add_argument("--historical", action="append", default=[],
                        help="accepted verdict artifacts to quote as context only (never compared "
                             "across builds or data versions)")
    parser.add_argument("--results-root", default=str(ROOT / "state" / "quant" / "results"))
    parser.add_argument("--materiality-pct", type=float, default=20.0)
    parser.add_argument("--out", default=str(ROOT / "state" / "quant" / "results"
                                             / "d034_research_cap" / "research_cap.json"))
    args = parser.parse_args()

    cells: list[dict] = []
    guards: dict[str, dict] = {}
    for spec in args.cell:
        parts = spec.split("|")
        if len(parts) != 3:
            raise SystemExit(f"--cell wants NAME|CONTROL|TREATMENT, got {spec!r}")
        name, control_path, treatment_path = parts
        control = read_json(Path(control_path))
        treatment = read_json(Path(treatment_path))
        guard = protocol_guard(control, treatment)
        guards[name] = guard
        if not guard["ok"]:
            cells.append({"cell": name, "guard_failed": True, "verdict_changed": None,
                          "material_by_20pct_rule": None,
                          "note": "guard failed: no cap conclusion is drawn from this pair"})
            continue
        cells.append(compare_cell(name, control, treatment, materiality_pct=args.materiality_pct))

    censuses: dict[str, dict] = {}
    for label, paths in (("control", args.trades_control), ("treatment", args.trades_treatment)):
        if paths:
            census = hold_census([read_json(Path(p)) for p in paths])
            census["sources"] = list(paths)
            censuses[label] = census
    censuses["labels"] = cap_label_census(Path(args.results_root))

    historical = []
    for path in args.historical:
        doc = read_json(Path(path))
        historical.append({
            "file": str(path), "run_id": doc.get("run_id"),
            "code_version": doc.get("code_version"), "data_version": doc.get("data_version"),
            "decision": (doc.get("decision") or {}).get("decision"),
            "aggregate": doc.get("aggregate"),
            "comparable_to_this_pair": False,
            "reason": "context only: this probe compares a guarded pair built on the same commit "
                      "and the same data version, and neither is guaranteed here",
        })

    report = build_report(cells, guards, censuses, deployed_witnesses(Path(args.d033)),
                          historical, args.materiality_pct)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"guards: {'all pass' if report['all_guards_pass'] else 'FAILED'} "
          f"({len(guards)} pair(s)); verdict moves: {report['h1']['verdict_changed_cells'] or 'none'}; "
          f"material: {report['h1']['material_cells'] or 'none'}", flush=True)
    for cell in cells:
        if cell.get("guard_failed"):
            print(f"  {cell['cell']}: guard failed", flush=True)
            continue
        agg = cell["aggregates"]
        relative = cell["net_return_pct_relative_change"]
        print(f"  {cell['cell']}: {cell['verdict_control']} -> {cell['verdict_treatment']} "
              f"net {agg['net_return_pct_sum']['control']} -> {agg['net_return_pct_sum']['treatment']} "
              f"({'undefined: control is zero' if relative is None else f'{relative}%'}) "
              f"PF {agg['profit_factor_mean']['control']} -> "
              f"{agg['profit_factor_mean']['treatment']} DD {agg['max_effective_drawdown_pct']['control']} -> "
              f"{agg['max_effective_drawdown_pct']['treatment']} flips={cell['gate_flips'] or '-'} "
              f"params_changed_folds={cell['search_channel']['params_changed_folds']}",
              flush=True)
    shares = report["h2"]["censoring_shares_pct"]
    print(f"H2 time-exit share control={shares['control_time_exits']}% "
          f"treatment={shares['treatment_time_exits']}%; at declared cap "
          f"control={shares['control_at_declared_cap']}% "
          f"treatment={shares['treatment_at_declared_cap']}% "
          f"(binds={report['h2']['cap_binds_in_control']}, "
          f"released={report['h2']['cap_released_in_treatment']})", flush=True)
    print(f"H3: {censuses['labels']['run_files']} run files, "
          f"{censuses['labels']['runs_recording_cap']} record a cap, "
          f"{censuses['labels']['runs_without_cap_field']} carry no cap field", flush=True)
    print(f"conclusion: {report['conclusion']}", flush=True)
    print(f"wrote {out_path}", flush=True)
    return 0 if report["all_guards_pass"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
