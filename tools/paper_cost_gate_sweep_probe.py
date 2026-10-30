"""D-043 — sensitivity of the accepted family's net-per-day to the cost gate, on a locked grid.

D-041 measured the only `accepted` ledger family (`ema_trend·4h`) in the gate's own unit: entries are not
scarce on the research path (14.18 / 16.58 trades/day against a required 1.1552) and every cell still lost
money — with `min_edge_cost_ratio = 2.0` already in force (read out of `provenance.sim`, not assumed).
So the variable left to measure is the cost structure itself, not the identity of the rule.

The grid, the pass rule and the multiplicity are pre-registered in `DECISIONS.md` (D-043) BEFORE any test
or code, and this tool refuses a ratio outside that grid: a post-hoc addition is mechanically blocked, not
merely discouraged.  Nothing here is an acceptance — `acceptance()` is never called, no gate threshold moves,
`config.yaml` is never written (the ratio changes only on the settings object), the 24h research cap stays in
force (D-034), and a negative result is published as the verdict.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import paper_family_rate_probe as base  # noqa: E402  (single source of truth for the protocol)

OUT = ROOT / "state" / "quant" / "results" / "d043_cost_gate_sweep"
D041_ARTIFACT = base.OUT / "family_rate.json"

# Pre-registered in DECISIONS.md (D-043) before any test or code. 2.0 is the deployed anchor, not a knob.
GRID = (0.0, 1.0, 2.0, 3.0, 5.0)
ANCHOR_RATIO = 2.0
PRE_REGISTRATION = "DECISIONS.md ## D-043 (locked grid, pass rule and multiplicity set before results)"


def cell_key(family: str, window: str, ratio: float) -> str:
    return f"{family}·{window}·{ratio:g}"


def apply_ratio(base_settings: Any, ratio: float, *, grid: Sequence[float] = GRID) -> Any:
    """Change the cost gate on the settings object only — the shipped file is never written."""
    if ratio not in grid:
        raise ValueError(f"ratio {ratio} is not in the pre-registered grid {grid}; refusing an unplanned cell")
    return replace(base_settings, backtest=replace(base_settings.backtest, min_edge_cost_ratio=float(ratio)))


def ratio_verdicts(cells: Mapping[str, Mapping[str, Any]], *, family: str,
                   windows: Sequence[str], grid: Sequence[float]) -> dict[str, Any]:
    """One row per grid value: does net_per_day clear zero in EVERY window (the pre-registered bar)?"""
    rows: list[dict[str, Any]] = []
    for ratio in grid:
        per_window = []
        for window in windows:
            cell = cells.get(cell_key(family, window, ratio))
            if cell is None:
                raise ValueError(f"missing cell {cell_key(family, window, ratio)!r} for {family}")
            net = cell["shared_account"]["net_per_day_pct"]
            per_window.append({"window": window, "net_per_day_pct": net,
                               "positive": (net or 0.0) > 0,
                               "closed_trades": cell["shared_account"]["closed_trades"],
                               "trades_per_day": cell["shared_account"]["trades_per_day"],
                               "excess_over_benchmark_pp": cell["shared_account"]["excess_over_benchmark_pp"]})
        rows.append({"min_edge_cost_ratio": ratio,
                     "windows": per_window,
                     "positive_in_every_window": all(row["positive"] for row in per_window),
                     "windows_positive": [row["window"] for row in per_window if row["positive"]]})
    clearing = [row["min_edge_cost_ratio"] for row in rows if row["positive_in_every_window"]]
    worst_window = [{
        "min_edge_cost_ratio": row["min_edge_cost_ratio"],
        "worst_window_net_per_day_pct": min(w["net_per_day_pct"] or 0.0 for w in row["windows"]),
        "worst_window_labels": [w["window"] for w in row["windows"]
                                if (w["net_per_day_pct"] or 0.0)
                                == min(x["net_per_day_pct"] or 0.0 for x in row["windows"])],
    } for row in rows]
    extremes = _extremes(worst_window)
    return {
        "family": family,
        "grid": list(grid),
        "per_ratio": rows,
        "ratios_positive_in_every_window": clearing,
        "any_ratio_positive_in_every_window": bool(clearing),
        "anchor_in_force": ANCHOR_RATIO,
        "anchor_positive_in_every_window": any(row["min_edge_cost_ratio"] == ANCHOR_RATIO
                                               and row["positive_in_every_window"] for row in rows),
        "worst_window_net_per_day_by_ratio": worst_window,
        **extremes,
        "selection_on_this_grid": ("the best/worst rows above are the extremes of "
                                   f"{len(rows)} unplanned-but-enumerated comparisons; naming one is not "
                                   "evidence it generalises and is not authority to edit config.yaml (D-024)"),
    }


def _extremes(worst_window: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The grid extrema as TIED SETS, never as a single ratio.

    min()/max() on a tied column return whichever row happens to come first, and a first-row answer reads
    exactly like a measured ranking.  When every grid value produces the same worst-window number, the grid
    has ordered nothing: the only honest output is both extremes as sets plus the fact that they coincide.
    """
    values = [row["worst_window_net_per_day_pct"] for row in worst_window]
    ratios = [row["min_edge_cost_ratio"] for row in worst_window]
    hi, lo = max(values), min(values)
    best = [r for r, v in zip(ratios, values) if v == hi]
    worst = [r for r, v in zip(ratios, values) if v == lo]
    distinct = len(set(values))
    return {
        "best_ratios_by_worst_window_net_per_day": best,
        "worst_ratios_by_worst_window_net_per_day": worst,
        "distinct_worst_window_values_on_the_grid": distinct,
        "grid_orders_the_arms": bool(hi != lo),
        "ranking_resolves": bool(hi != lo and len(best) == 1 and len(worst) == 1),
        "ranking_note": (f"{distinct} distinct worst-window values across {len(values)} ratios; "
                         + ("the extremes are unique so the grid ordered the arms"
                            if (hi != lo and len(best) == 1 and len(worst) == 1)
                            else "the grid does not order the arms, so no ratio may be named best or worst")),
    }


def arm_binding(cells: Mapping[str, Mapping[str, Any]], *, family: str,
                windows: Sequence[str], grid: Sequence[float]) -> dict[str, Any]:
    """Which arms differ from the gate-off arm.

    An arm that reproduces ratio 0 exactly measured no binding at all: the floor never rejected an entry
    there. Reporting five identical cells as five independent measurements of a knob would be a lie about
    the experiment, so the identity is named here instead of hidden in a table.
    """
    def fingerprint(cell: Mapping[str, Any]) -> tuple:
        account = cell["shared_account"]
        return (account["closed_trades"], account["net_per_day_pct"],
                cell["aggregate_rate"]["trades_per_day"])

    gate_off = {window: cells.get(cell_key(family, window, 0.0)) for window in windows}
    if any(cell is None for cell in gate_off.values()):
        return {"comparable": False,
                "reason": "the gate-off arm (ratio 0.0) is not in this run, so binding cannot be judged"}
    reference = {window: fingerprint(cell) for window, cell in gate_off.items()}
    arms = []
    for ratio in grid:
        same = all(fingerprint(cells[cell_key(family, window, ratio)]) == reference[window]
                   for window in windows)
        arms.append({"min_edge_cost_ratio": ratio, "identical_to_gate_off_arm": same})
    binding = [arm["min_edge_cost_ratio"] for arm in arms if not arm["identical_to_gate_off_arm"]]
    return {
        "comparable": True,
        "arms": arms,
        "ratios_where_the_gate_binds": binding,
        "first_binding_ratio": binding[0] if binding else None,
        "note": ("an arm identical to ratio 0 rejected nothing: the target already cleared "
                 "ratio x round-trip cost for every entry that survived the other gates"),
    }


def _anchor_is_not_the_worst(accepted: Mapping[str, Any]) -> dict[str, Any]:
    """Tri-state for H2, because a tied column has no worst arm to be an anchor's rival.

    `satisfied` only when the grid actually orders its arms and the deployed 2.0 is outside the worst tied
    set; `refuted` when 2.0 is uniquely worst; `indeterminate` otherwise — never a boolean read off
    `max()`/`min()` of an unordered column.
    """
    detail = {"anchor": ANCHOR_RATIO,
              "best_ratios": accepted["best_ratios_by_worst_window_net_per_day"],
              "worst_ratios": accepted["worst_ratios_by_worst_window_net_per_day"],
              "distinct_worst_window_values": accepted["distinct_worst_window_values_on_the_grid"],
              "grid_orders_the_arms": accepted["grid_orders_the_arms"]}
    if not detail["grid_orders_the_arms"]:
        detail["value"] = None
        detail["reason"] = (f"all {len(detail['worst_ratios'])} ratios sit at one identical worst-window "
                            f"value ({detail['distinct_worst_window_values']} distinct value), so the grid "
                            "ordered nothing; whether 2.0 is the worst arm is unanswerable here (F-053)")
    elif ANCHOR_RATIO in detail["worst_ratios"]:
        detail["value"] = False
        detail["reason"] = ("the grid orders its arms and the deployed 2.0 is in the worst set "
                            f"{detail['worst_ratios']}"
                            + ("" if len(detail["worst_ratios"]) == 1 else " (tied for worst)"))
    else:
        detail["value"] = True
        detail["reason"] = (f"the grid orders its arms and 2.0 is strictly above the worst set "
                            f"{detail['worst_ratios']}")
    return detail


def sweep_verdicts(cells: Mapping[str, Mapping[str, Any]], *, families: Sequence[str],
                   windows: Sequence[str], grid: Sequence[float]) -> dict[str, Any]:
    per_family = {family: {**ratio_verdicts(cells, family=family, windows=windows, grid=grid),
                           "arm_binding": arm_binding(cells, family=family, windows=windows, grid=grid)}
                  for family in families}
    accepted, contrast = families[0], families[1] if len(families) > 1 else families[0]
    accepted_clears = per_family[accepted]["any_ratio_positive_in_every_window"]
    contrast_clears = per_family[contrast]["any_ratio_positive_in_every_window"]
    binds = {family: per_family[family]["arm_binding"].get("ratios_where_the_gate_binds", [])
             for family in families}
    grid_binds_anywhere = any(binds.values())
    h2 = _anchor_is_not_the_worst(per_family[accepted])
    return {
        "per_family": per_family,
        "grid_reaches_the_binding_region": grid_binds_anywhere,
        "arms_where_the_gate_binds": binds,
        "binding_note": (None if grid_binds_anywhere else (
            "no arm in the registered grid rejected an entry the gate-off arm allowed, so this grid cannot "
            "answer whether the cost floor helps — it only shows the floor is not binding below "
            f"{max(grid):g}x. Extending the grid is a new pre-registered step, not a revision of this one")),
        "hypothesis_h1_some_ratio_clears_zero_in_every_window": accepted_clears,
        "hypothesis_h2_the_anchor_is_not_the_worst": h2["value"],
        "hypothesis_h2_detail": h2,
        "hypothesis_h3_contrast_family_behaves_the_same": (accepted_clears == contrast_clears),
        "cost_gate_lever_rejected_on_this_grid": not accepted_clears and not contrast_clears,
        "cells_measured": len(cells),
        "tests_run": len(grid) * len(families) * len(windows),
        "multiplicity": (f"{len(grid)} ratios x {len(families)} families x {len(windows)} windows = "
                         f"{len(cells)} cells; every one is published, including the negatives"),
        "reading": ("this is a sensitivity of one policy knob in the gate's unit, not an acceptance: "
                    "a ratio that turns the mean positive on this grid still has to survive the deployed "
                    "path and the 28-day gate-B evidence window (AGENTS.md rule 2)"),
    }


def anchor_reference(d041: Mapping[str, Any], *, grid: Sequence[float]) -> dict[str, Any]:
    """The D-041 cells at the in-force ratio, verbatim — the sweep must reproduce them or say so."""
    if ANCHOR_RATIO not in grid:
        raise ValueError(f"{ANCHOR_RATIO} (the deployed in-force ratio) is not in the grid {grid}")
    rows = {}
    for label, cell in sorted(d041["cells"].items()):
        rows[label] = {
            "aggregate_trades_per_day": cell["aggregate_rate"]["trades_per_day"],
            "shared_trades_per_day": cell["shared_account"]["trades_per_day"],
            "closed_trades": cell["shared_account"]["closed_trades"],
            "net_per_day_pct": cell["shared_account"]["net_per_day_pct"],
            "min_edge_cost_ratio_in_force": cell.get("min_edge_cost_ratio"),
        }
    return {
        "read_verbatim_from": str(D041_ARTIFACT.relative_to(ROOT)).replace("\\", "/"),
        "recomputed": False,
        "cells": rows,
        "note": (f"D-041 ran with min_edge_cost_ratio = {ANCHOR_RATIO} in force (provenance.sim); that row "
                 "of this grid must reproduce these four numbers exactly, and any difference is reported"),
    }


def anchor_mismatches(cells: Mapping[str, Mapping[str, Any]], reference: Mapping[str, Any],
                      *, families: Sequence[str], windows: Sequence[str]) -> list[dict[str, Any]]:
    """Every disagreement between the sweep at the anchor ratio and the archived D-041 count."""
    fields = {"aggregate_trades_per_day": ("aggregate_rate", "trades_per_day"),
              "shared_trades_per_day": ("shared_account", "trades_per_day"),
              "closed_trades": ("shared_account", "closed_trades"),
              "net_per_day_pct": ("shared_account", "net_per_day_pct")}
    out: list[dict[str, Any]] = []
    for label, expected in reference["cells"].items():
        family, window = label.split("·")
        cell = cells.get(cell_key(family, window, ANCHOR_RATIO))
        if cell is None:
            out.append({"cell": label, "field": "missing", "expected": "a cell", "measured": None})
            continue
        for field, path in fields.items():
            measured = cell[path[0]][path[1]]
            if measured != expected[field]:
                out.append({"cell": label, "field": field, "expected": expected[field],
                            "measured": measured})
    return out


def response_shape(verdicts: Mapping[str, Any], *, family: str) -> list[dict[str, Any]]:
    """net_per_day per ratio in each window, so the slope is visible without anyone averaging it away."""
    rows: list[dict[str, Any]] = []
    for row in verdicts["per_family"][family]["per_ratio"]:
        entry = {"min_edge_cost_ratio": row["min_edge_cost_ratio"]}
        for window in row["windows"]:
            entry[window["window"]] = window["net_per_day_pct"]
        entry["trades_per_day"] = {window["window"]: window["trades_per_day"] for window in row["windows"]}
        rows.append(entry)
    return rows


def _digestable(cell: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in cell.items()
            if key not in {"elapsed_seconds", "progress_events", "errors"}}


def cells_digest(cells: Mapping[str, Mapping[str, Any]]) -> str:
    blob = json.dumps({label: _digestable(cell) for label, cell in sorted(cells.items())},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ratios", default=",".join(f"{g:g}" for g in GRID),
                        help="subset of the pre-registered grid; a value outside it is refused")
    parser.add_argument("--families", default=f"{base.ACCEPTED_FAMILY},{base.CONTRAST_FAMILY}")
    parser.add_argument("--windows", default=",".join(base.WINDOWS))
    parser.add_argument("--timeframe", default=base.TIMEFRAME)
    parser.add_argument("--universe", default=str(base.UNIVERSE))
    parser.add_argument("--universe-digest", default="cf828076fde8f347")
    parser.add_argument("--extra-months", default="2026-10")
    parser.add_argument("--limit", type=int, help="first N symbols only (pilot; never the record run)")
    parser.add_argument("--out", default=str(OUT / "cost_gate_sweep.json"))
    args = parser.parse_args()

    if args.timeframe != base.TIMEFRAME:
        print(f"D-043 is pre-registered on {base.TIMEFRAME} only, got {args.timeframe}")
        return 3
    try:
        ratios = tuple(float(part) for part in args.ratios.split(",") if part.strip() != "")
    except ValueError:
        print(f"--ratios must be numbers, got {args.ratios!r}")
        return 3
    outside = [r for r in ratios if r not in GRID]
    if outside or not ratios:
        print(f"ratios {outside} are outside the pre-registered grid {GRID}; refusing to run")
        return 3
    windows = [part.strip() for part in args.windows.split(",") if part.strip()]
    if any(name not in base.WINDOWS for name in windows):
        print(f"unknown window: {[w for w in windows if w not in base.WINDOWS]}")
        return 3
    families = [part.strip() for part in args.families.split(",") if part.strip()]
    if len(families) < 2:
        print(f"D-043 needs the accepted family and the contrast family, got {families}")
        return 3
    return run_sweep(args, families, windows, ratios)


def run_sweep(args: argparse.Namespace, families: Sequence[str], windows: Sequence[str],
              ratios: Sequence[float]) -> int:
    from cat.config import load_config
    from cat.quant.engine import simulate_settings
    from cat.quant.features import params_from_dict
    from cat.quant.settings import load_quant_settings
    from cat.quant.storage import current_data_version, daily_data_version, universe_digest
    from paper_signal_census import months_with_extra

    gate_b = base.GATE_B
    required = base.required_trades_per_day(json.loads(gate_b.read_text(encoding="utf-8")),
                                           source=str(gate_b))
    config = load_config(root=ROOT)
    settings = load_quant_settings(config)
    universe_path = Path(args.universe)
    settings = replace(settings, universe_path=(universe_path if universe_path.is_absolute()
                                               else ROOT / universe_path))
    months_read = months_with_extra(settings, args.extra_months)
    settings = replace(settings, months=tuple(months_read))
    digest = universe_digest(settings)
    if digest != args.universe_digest:
        print(f"universe digest = {digest}, expected {args.universe_digest}; refusing to count")
        return 3
    symbols = list(json.loads(settings.universe_path.read_text(encoding="utf-8"))["symbols"])
    if args.limit:
        symbols = symbols[:args.limit]
    params = params_from_dict()

    in_force = settings.backtest.min_edge_cost_ratio
    print(f"D-043: grid {list(ratios)} (config.yaml in force = {in_force}, anchor = {ANCHOR_RATIO}), "
          f"{len(symbols)} symbols x {args.timeframe}, cap {settings.backtest.max_hold_hours}h kept, "
          f"required {required} trades/day read from {gate_b.name}")

    cells: dict[str, dict[str, Any]] = {}
    started = time.monotonic()
    for ratio in ratios:
        settings_r = apply_ratio(settings, ratio)
        sim = simulate_settings(settings_r)
        if sim.min_edge_cost_ratio != ratio:
            print(f"simulator did not take the ratio (asked {ratio}, got {sim.min_edge_cost_ratio}); stopping")
            return 3
        for family in families:
            for name in windows:
                start_day, end_day = base.WINDOWS[name]
                start_ts, end_ts = base.epoch(start_day), base.epoch(end_day)
                cell = base.run_cell(settings=settings_r, sim=sim, params=params, family=family,
                                     timeframe=args.timeframe, symbols=symbols,
                                     start_ts=start_ts, end_ts=end_ts, required=required)
                cell.update({"window": name, "window_start": start_day, "window_end": end_day,
                             "min_edge_cost_ratio": sim.min_edge_cost_ratio,
                             "fee_pct": sim.fee_pct, "slippage_pct": sim.slippage_pct,
                             "round_trip_cost_pct": round(2 * (sim.fee_pct + sim.slippage_pct), 4)})
                cells[cell_key(family, name, ratio)] = cell
                print(f"  ratio {ratio:g} {family} {name}: net/day "
                      f"{cell['shared_account']['net_per_day_pct']}% at "
                      f"{cell['shared_account']['trades_per_day']} trades/day")

    verdict = sweep_verdicts(cells, families=families, windows=windows, grid=ratios)
    d041 = json.loads(D041_ARTIFACT.read_text(encoding="utf-8"))
    reference = anchor_reference(d041, grid=ratios)
    if args.limit or 0.0 not in ratios:
        differences: list[dict[str, Any]] = []
        comparison = ("skipped: --limit ran a pilot basket, which cannot be compared to the archived "
                      "69-symbol count" if args.limit else
                      "skipped: the gate-off arm (ratio 0.0) is not in this run")
        anchor_reproduced = None
    else:
        differences = anchor_mismatches(cells, reference, families=families, windows=windows)
        comparison = "the sweep at the in-force ratio must equal the archived D-041 count exactly"
        anchor_reproduced = not differences

    payload: dict[str, Any] = {
        "decision": "d043-cost-gate-sweep",
        "record_run": not bool(args.limit),
        "pre_registration": PRE_REGISTRATION,
        "grid_locked_before_results": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timeframe": args.timeframe,
        "required_trades_per_day": required,
        "path_label": "research_sim",
        "provenance": {
            "gate_b": str(gate_b.relative_to(ROOT)).replace("\\", "/"),
            "universe_file": str(settings.universe_path.relative_to(ROOT)).replace("\\", "/"),
            "universe_digest": digest,
            "symbols": len(symbols),
            "pilot_limit": args.limit or None,
            "windows": {name: list(base.WINDOWS[name]) for name in windows},
            "families": list(families),
            "ratios_run": list(ratios),
            "anchor_ratio": ANCHOR_RATIO,
            "config_min_edge_cost_ratio_in_force": in_force,
            "settings_file": "config.yaml (read; never written — the ratio changes on the object only)",
            "months_read": months_read,
            "lake_version": (daily_data_version(settings) if months_read != list(settings.months)
                             else current_data_version(settings)),
            "read_only": True,
        },
        "cells": cells,
        "verdicts": verdict,
        "response_shape": {family: response_shape(verdict, family=family) for family in families},
        "anchor_reference": {**reference, "comparison": comparison,
                             "differences_from_this_sweep": differences,
                             "anchor_reproduced": anchor_reproduced},
        "deployed_reference": base.deployed_reference(ceiling_file=base.DEPLOYED_CEILING,
                                                      universe_digest=args.universe_digest),
        "accepted_verdict_reference": base.accepted_verdict_reference(
            run_file=base.ACCEPTED_RUN, measured_params_label=params.label(),
            measured_symbols=len(symbols)),
        "cells_digest": cells_digest(cells),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "not_a_forecast": ("PAPER research counts on stored 1m bars, net of fees and slippage, with the 24h "
                           "research cap in force; a sensitivity of one policy knob is not a promise of "
                           "profit and changes no gate (AGENTS.md rule 2)"),
    }
    out_path = Path(args.out)
    out_path = out_path if out_path.is_absolute() else ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path.relative_to(ROOT).as_posix()} "
          f"({verdict['cells_measured']} cells, digest {payload['cells_digest']})")
    print("  accepted family clears zero in every window at: "
          f"{verdict['per_family'][families[0]]['ratios_positive_in_every_window']}")
    print(f"  cost-gate lever rejected on this grid: {verdict['cost_gate_lever_rejected_on_this_grid']}")
    print(f"  anchor (ratio {ANCHOR_RATIO}) reproduces D-041: {payload['anchor_reference']['anchor_reproduced']}"
          f" ({len(differences)} differences)")
    print(f"  arms where the gate binds: {verdict['arms_where_the_gate_binds']}"
          f" (grid reaches the binding region: {verdict['grid_reaches_the_binding_region']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
