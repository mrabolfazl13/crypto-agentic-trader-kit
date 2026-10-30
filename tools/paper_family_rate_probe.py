"""Count the accepted research family in the gate's own unit: closed trades per day (D-041, read-only).

Every cadence number published so far (D-020/22/23/26/33) came from the *deployed* `trend_pullback`
rule on `cat.strategy`, while the only `accepted` verdict in the ledger belongs to `ema_trend·4h` on
the research engine. This tool asks the one unanswered question — how many trades per day does the
accepted rule actually produce on the same 69-symbol allowlist and the same two independent windows —
and answers with a count, never an acceptance: ``acceptance()`` is not called, no threshold moves, the
24h research cap stays in force, and the deployed path is quoted verbatim from its own artifacts.

DECISIONS.md (D-041) pre-registers the hypotheses (H1 slot-free rate, H2 one-slot ceiling, H3 per-day
mean, H4 path labels plus determinism) and the locks (required read from the gate artifact, settings
read from ``config.yaml``, artifact-only numbers, slot split kept, negatives kept, multiplicity count).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

OUT = ROOT / "state" / "quant" / "results" / "d041_family_rate"
GATE_B = ROOT / "state" / "reports" / "gate_b_evidence_server.json"
DEPLOYED_CEILING = ROOT / "state" / "quant" / "results" / "d033_deployed_ceiling" / "deployed_ceiling.json"
ACCEPTED_RUN = (ROOT / "state" / "quant" / "results" / "nested_walk_forward_breadth_may_v2"
                / "nested-wf-ema_trend-4h-train_breadth.json")
UNIVERSE = ROOT / "state" / "quant" / "universe_allowlist.json"

TIMEFRAME = "4h"
ACCEPTED_FAMILY = "ema_trend"
CONTRAST_FAMILY = "trend_pullback"
# The two independent windows D-026 counted, quoted here so both families share one calendar.
WINDOWS = {"w1": ("2026-05-01", "2026-08-01"), "w3": ("2026-10-01", "2026-10-27")}
SECONDS_PER_DAY = 86400.0


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def epoch(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


def window_days(start_ts: int, end_ts: int) -> float:
    """The counted window is the denominator — never the span of data loaded in front of it."""
    return (float(end_ts) - float(start_ts)) / SECONDS_PER_DAY


def required_trades_per_day(gate_b: Mapping[str, Any], *, source: str) -> float:
    """Read the gate's own unit out of the evidence file; a missing cadence block is an error (F-039)."""
    cadence = gate_b.get("cadence")
    if not isinstance(cadence, Mapping) or cadence.get("required_trades_per_day") is None:
        raise ValueError(f"{source}: no cadence.required_trades_per_day to read; refusing a default")
    return float(cadence["required_trades_per_day"])


def cell_rates(*, candidates: int, closed: int, net_return_pct: float,
               benchmark_return_pct: float | None, days: float) -> dict[str, Any]:
    """The slot split stays split (lock 4): an upper bound and a executed count, never one for the other.

    ``aggregate_rate`` prices every symbol's own account, so it is the research analogue of
    ``pooled_trades_per_day_upper_bound`` — a ceiling, not an achievable cadence for one account.
    """
    if days <= 0:
        raise ValueError(f"window days must be positive, got {days}")
    net = float(net_return_pct)
    bench_per_day = (round(float(benchmark_return_pct) / days, 6)
                     if benchmark_return_pct is not None else None)
    excess = (round(net - float(benchmark_return_pct), 4)
              if benchmark_return_pct is not None else None)
    return {
        "days": round(days, 4),
        "aggregate_rate": {"per_symbol_accounts": int(candidates),
                           "trades_per_day": round(candidates / days, 4)},
        "shared_account": {"closed_trades": int(closed),
                           "trades_per_day": round(closed / days, 4),
                           "net_return_pct": round(net, 4),
                           "net_per_day_pct": round(net / days, 6),
                           "excess_over_benchmark_pp": excess,
                           "beats_benchmark_while_losing": (excess is not None and excess > 0
                                                            and net < 0)},
        "benchmark": {"return_pct": (round(float(benchmark_return_pct), 4)
                                     if benchmark_return_pct is not None else None),
                      "net_per_day_pct": bench_per_day,
                      "same_denominator_as_strategy": bench_per_day is not None},
    }


def hold_ceiling(*, closed_trades: Sequence[Mapping[str, Any]], required: float,
                 window_end_iso: str) -> dict[str, Any]:
    """One-slot ceiling from the D-037 tri-state estimator — reused, never re-implemented."""
    from paper_slot_reachability_probe import cadence_ceiling

    return cadence_ceiling(trades=list(closed_trades), positions=[], snapshot_ts=window_end_iso,
                           required=required)


def _digestable(cell: Mapping[str, Any]) -> dict[str, Any]:
    """Everything that a second pass must reproduce: the numbers, not the clock or the progress feed."""
    return {key: value for key, value in cell.items()
            if key not in {"elapsed_seconds", "progress_events", "errors"}}


def cell_digest(cell: Mapping[str, Any]) -> str:
    blob = json.dumps(_digestable(cell), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def cells_digest(cells: Mapping[str, Mapping[str, Any]]) -> str:
    blob = json.dumps({label: _digestable(cell) for label, cell in sorted(cells.items())},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def verdicts(cells: Mapping[str, Mapping[str, Any]], *, required: float,
             accepted_family: str, contrast_family: str) -> dict[str, Any]:
    """H1/H2/H3 evaluated as pre-registered: a count with an explicit pass/fail per hypothesis."""
    per_family: dict[str, dict[str, Any]] = {}
    for family in (accepted_family, contrast_family):
        rows = [cell for cell in cells.values() if cell["family"] == family]
        h1 = [{"window": cell["window"], "aggregate_trades_per_day":
               cell["aggregate_rate"]["trades_per_day"],
               "clears_required": cell["aggregate_rate"]["trades_per_day"] >= required} for cell in rows]
        h2 = [{"window": cell["window"],
               "ceiling_per_day": cell["one_slot_ceiling"]["ceiling_one_slot_per_day"],
               "clears_required": cell["one_slot_ceiling"]["one_slot_clears_requirement"]} for cell in rows]
        h3 = [{"window": cell["window"], "net_per_day_pct": cell["shared_account"]["net_per_day_pct"],
               "positive": (cell["shared_account"]["net_per_day_pct"] or 0.0) > 0,
               "benchmark_net_per_day_pct": cell["benchmark"]["net_per_day_pct"]} for cell in rows]
        per_family[family] = {
            "h1_windows_clearing_required": [row["window"] for row in h1 if row["clears_required"]],
            "h1_supported": any(row["clears_required"] for row in h1),
            "h2_windows_clearing_required": [row["window"] for row in h2 if row["clears_required"] is True],
            "h2_supported": any(row["clears_required"] is True for row in h2),
            "h2_indeterminate_windows": [row["window"] for row in h2 if row["clears_required"] is None],
            "h3_windows": h3,
            "h3_supported": bool(rows) and all(row["positive"] for row in h3),
            "cells": h1,
        }
    accepted = per_family[accepted_family]
    return {
        "required_trades_per_day": required,
        "unit": "closed trades per counted day of the window",
        "accepted_family": {"family": accepted_family, **accepted},
        "contrast_family": {"family": contrast_family, **per_family[contrast_family]},
        "cells_measured": len(cells),
        "tests_run": len(cells) * 3,
        "h1_rejected_cells": sum(1 for cell in cells.values()
                                if cell["aggregate_rate"]["trades_per_day"] < required),
        "h3_negative_cells": sum(1 for cell in cells.values()
                                if (cell["shared_account"]["net_per_day_pct"] or 0.0) <= 0),
        "reading": ("this is a count in the gate's unit, not an acceptance: acceptance() is never "
                    "called, no threshold moved, and every cell carries path=research_sim"),
    }


def deployed_reference(*, ceiling_file: Path, universe_digest: str) -> dict[str, Any]:
    """Quote the deployed path from its own published artifacts — recomputing it here is the D-025 error."""
    ceiling = json.loads(ceiling_file.read_text(encoding="utf-8"))
    cells = {label: cell for label, cell in (ceiling.get("cells") or {}).items()
             if cell.get("timeframe") == TIMEFRAME}
    for label, cell in cells.items():
        source = cell.get("source")
        if not source:
            raise ValueError(f"deployed cell {label} names no source artifact")
        path = (ROOT / source).resolve()
        summary = json.loads(path.read_text(encoding="utf-8")).get("summary") or {}
        if summary.get("universe_digest") != universe_digest:
            raise ValueError(f"{source}: universe digest {summary.get('universe_digest')!r} "
                             f"is not {universe_digest!r}")
    return {
        "recomputed": False,
        "read_verbatim_from": str(ceiling_file.relative_to(ROOT)).replace("\\", "/"),
        "required_per_day": ceiling.get("required_per_day"),
        "max_hold_days_to_clear_with_one_slot": ceiling.get("max_hold_days_to_clear_with_one_slot"),
        "h1": ceiling.get("h1"),
        "sim_path_contrast": ceiling.get("sim_path_contrast"),
        "cells_4h": cells,
        "note": ("the deployed numbers are D-033's own; they are printed beside this run so the two "
                 "paths can be compared, never so one validates the other (H4)"),
    }


def path_divergence(*, cells: Mapping[str, Mapping[str, Any]], deployed: Mapping[str, Any],
                    rule: str) -> dict[str, Any]:
    """Put the two counts of the same rule side by side, each with its own source.

    D-033 counted the deployed classifier under the production decision clock and one account; this
    run counts the research generator with per-symbol serial positions. The ratio is the size of that
    protocol gap, and it is why a passing H1 here cannot be read as a passing cadence in production.
    """
    rows: list[dict[str, Any]] = []
    for label in sorted(cells):
        cell = cells[label]
        if cell["family"] != rule:
            continue
        reference = (deployed.get("cells_4h") or {}).get(f"{cell['window']}_4h") or {}
        serial = reference.get("measured_trades_per_day_serial")
        rate = cell["shared_account"]["trades_per_day"]
        rows.append({"window": cell["window"],
                     "research_shared_account_per_day": rate,
                     "research_aggregate_per_day": cell["aggregate_rate"]["trades_per_day"],
                     "deployed_serial_per_day": serial,
                     "deployed_pooled_per_day": reference.get("pooled_trades_per_day_upper_bound"),
                     "research_over_deployed_serial": (round(rate / serial, 2) if serial else None)})
    return {"rule_compared": rule, "cells": rows, "deployed_numbers_read_verbatim": True,
            "note": ("a ratio of the two paths is a protocol gap, not evidence that either path is "
                     "wrong (H4); neither number is averaged into the other")}


def accepted_verdict_reference(*, run_file: Path, measured_params_label: str,
                               measured_symbols: int) -> dict[str, Any]:
    """Quote the ledger's only `accepted` verdict from its own run file and name every difference.

    Lock 2 forbids re-tuning here, so this measurement prices the family at the shipped default
    parameters on the full allowlist. The accepted verdict was earned with per-fold train-only
    parameters on a 25-symbol train-breadth subset inside a different window — reading its file
    verbatim keeps that gap visible instead of letting one number impersonate the other.
    """
    run = json.loads(run_file.read_text(encoding="utf-8"))
    folds = run.get("folds") or []
    return {
        "recomputed": False,
        "read_verbatim_from": str(run_file.relative_to(ROOT)).replace("\\", "/"),
        "run_id": run.get("run_id"),
        "created_at": run.get("created_at"),
        "code_version": run.get("code_version"),
        "data_version": run.get("data_version"),
        "window": run.get("window"),
        "protocol": run.get("protocol"),
        "final_test_read": run.get("final_test_read"),
        "aggregate": run.get("aggregate"),
        "decision": (run.get("decision") or {}).get("decision"),
        "per_fold_params": [{"fold": fold.get("fold"), "params_label": fold.get("params_label"),
                             "test": fold.get("test"), "trades": (fold.get("metrics") or {}).get("trades"),
                             "best_params": fold.get("best_params")} for fold in folds],
        "measured_here": {"params_label": measured_params_label, "symbols": measured_symbols,
                          "windows": {name: list(WINDOWS[name]) for name in WINDOWS}},
        "divergence": [
            "parameters: this run prices the shipped default rule; the accepted verdict was earned "
            "with per-fold train-only search",
            "breadth: this run uses the whole 69-symbol allowlist; the accepted verdict scored a "
            "train-breadth subset per fold",
            "window: the accepted verdict never saw w1 or w3",
        ],
    }


def run_cell(*, settings: Any, sim: Any, params: Any, family: str, timeframe: str,
             symbols: Sequence[str], start_ts: int, end_ts: int,
             required: float) -> dict[str, Any]:
    from cat.quant.engine import run_backtest

    started = time.monotonic()
    result = run_backtest(settings, symbols, strategy=family, timeframe=timeframe,
                          params=params, sim=sim, start_ts=start_ts, end_ts=end_ts)
    metrics = result["metrics"]
    rates = cell_rates(candidates=sum(int(row["candidates"]) for row in result["per_symbol"]),
                       closed=int(metrics.get("trades") or 0),
                       net_return_pct=float(metrics.get("net_return_pct") or 0.0),
                       benchmark_return_pct=metrics.get("benchmark_return_pct"),
                       days=window_days(start_ts, end_ts))
    rates["shared_account"].update({"profit_factor": metrics.get("profit_factor"),
                                    "max_drawdown_pct": metrics.get("max_drawdown_pct"),
                                    "win_rate_pct": metrics.get("win_rate_pct")})
    rates["rejections"] = metrics.get("rejections")
    rates["one_slot_ceiling"] = hold_ceiling(closed_trades=result["trades"], required=required,
                                            window_end_iso=_iso(end_ts))
    rates["path"] = "research_sim"
    rates["family"] = family
    rates["timeframe"] = timeframe
    rates["window"] = None
    rates["counted_from"] = _iso(start_ts)[:10]
    rates["counted_to"] = _iso(end_ts)[:10]
    rates["symbols_requested"] = len(symbols)
    rates["symbols_evaluated"] = len(result["per_symbol"])
    rates["params_label"] = result["params_label"]
    rates["max_hold_hours_in_force"] = result["sim"]["max_hold_hours"]
    rates["regime"] = result["regime"]
    rates["elapsed_seconds"] = round(time.monotonic() - started, 2)
    rates["errors"] = dict(result["errors"])
    return rates


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--families", default=f"{ACCEPTED_FAMILY},{CONTRAST_FAMILY}")
    parser.add_argument("--windows", default=",".join(WINDOWS))
    parser.add_argument("--timeframe", default=TIMEFRAME)
    parser.add_argument("--universe", default=str(UNIVERSE))
    parser.add_argument("--gate-b", default=str(GATE_B))
    parser.add_argument("--deployed-ceiling", default=str(DEPLOYED_CEILING))
    parser.add_argument("--accepted-run", default=str(ACCEPTED_RUN))
    parser.add_argument("--universe-digest", default="cf828076fde8f347")
    parser.add_argument("--extra-months", default="")
    parser.add_argument("--limit", type=int, help="first N symbols only (pilot; never the record run)")
    parser.add_argument("--out", default=str(OUT / "family_rate.json"))
    args = parser.parse_args()

    if args.timeframe != TIMEFRAME:
        print(f"D-041 is pre-registered on {TIMEFRAME} only, got {args.timeframe}")
        return 3
    windows = [part.strip() for part in args.windows.split(",") if part.strip()]
    if any(name not in WINDOWS for name in windows):
        print(f"unknown window: {[w for w in windows if w not in WINDOWS]}")
        return 3
    families = [part.strip() for part in args.families.split(",") if part.strip()]
    if not families:
        print("no families")
        return 3

    return run_probe(args, families, windows)


def run_probe(args: argparse.Namespace, families: Sequence[str], windows: Sequence[str]) -> int:
    from cat.config import load_config
    from cat.quant.engine import simulate_settings
    from cat.quant.features import params_from_dict
    from cat.quant.settings import load_quant_settings
    from cat.quant.storage import current_data_version, daily_data_version, universe_digest
    from dataclasses import replace
    from paper_signal_census import months_with_extra

    root = ROOT
    gate_b = Path(args.gate_b)
    required = required_trades_per_day(json.loads(gate_b.read_text(encoding="utf-8")),
                                      source=str(gate_b))

    config = load_config(root=root)
    settings = load_quant_settings(config)
    universe_path = Path(args.universe)
    settings = replace(settings, universe_path=(universe_path if universe_path.is_absolute()
                                               else root / universe_path))
    months_read = months_with_extra(settings, args.extra_months)
    settings = replace(settings, months=tuple(months_read))
    digest = universe_digest(settings)
    if digest != args.universe_digest:
        # The count only means something on the allowlist D-026/D-033 counted (lock 3).
        print(f"universe digest = {digest}, expected {args.universe_digest}; refusing to count")
        return 3
    symbols = list(json.loads(settings.universe_path.read_text(encoding="utf-8"))["symbols"])
    if args.limit:
        symbols = symbols[:args.limit]

    sim = simulate_settings(settings)
    params = params_from_dict()
    print(f"D-041: {len(symbols)} symbols x {args.timeframe}, required "
          f"{required} trades/day (read from {gate_b.name}), cap {sim.max_hold_hours}h (kept)")

    cells: dict[str, dict[str, Any]] = {}
    started = time.monotonic()
    for family in families:
        for name in windows:
            start_day, end_day = WINDOWS[name]
            start_ts, end_ts = epoch(start_day), epoch(end_day)
            cell = run_cell(settings=settings, sim=sim, params=params, family=family,
                            timeframe=args.timeframe, symbols=symbols,
                            start_ts=start_ts, end_ts=end_ts, required=required)
            cell["window"] = name
            cell["window_start"] = start_day
            cell["window_end"] = end_day
            cell["aggregate_rate"]["clears_required_trades_per_day"] = (
                cell["aggregate_rate"]["trades_per_day"] >= required)
            cells[f"{family}·{name}"] = cell
            print(f"  {family} {name}: aggregate {cell['aggregate_rate']['trades_per_day']} trades/day, "
                  f"shared {cell['shared_account']['trades_per_day']} trades/day, "
                  f"net {cell['shared_account']['net_return_pct']}% "
                  f"({cell['shared_account']['net_per_day_pct']}%/day), "
                  f"one-slot ceiling {cell['one_slot_ceiling']['ceiling_one_slot_per_day']}"
                  f" (clears: {cell['one_slot_ceiling']['one_slot_clears_requirement']})")

    deployed = deployed_reference(ceiling_file=Path(args.deployed_ceiling),
                                 universe_digest=args.universe_digest)
    payload = {
        "decision": "D-041",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timeframe": args.timeframe,
        "accepted_family": ACCEPTED_FAMILY,
        "contrast_family": CONTRAST_FAMILY,
        "path_label": "research_sim",
        "required_trades_per_day": required,
        "provenance": {
            "gate_b": {"file": str(gate_b.relative_to(root)).replace("\\", "/"),
                       "required_trades_per_day": required},
            "universe_file": str(settings.universe_path.relative_to(root)).replace("\\", "/"),
            "universe_digest": digest,
            "symbols": len(symbols),
            "windows": {name: list(WINDOWS[name]) for name in windows},
            "months_read": months_read,
            "settings_file": "config.yaml (read; never written)",
            "overrides": {"strategy": families, "note": ("simulator settings come from config.yaml "
                                                         "verbatim; the 24h research cap is NOT removed "
                                                         "(D-034); acceptance() is never called")},
            "lake_version": (daily_data_version(settings) if months_read != list(settings.months)
                             else current_data_version(settings)),
            "sim": {"initial_capital": sim.initial_capital, "fee_pct": sim.fee_pct,
                    "slippage_pct": sim.slippage_pct, "position_pct": sim.position_pct,
                    "max_open_positions": sim.max_open_positions,
                    "max_hold_hours": sim.max_hold_hours,
                    "min_edge_cost_ratio": sim.min_edge_cost_ratio,
                    "risk_per_trade_pct": sim.risk_per_trade_pct,
                    "regime_symbol": sim.regime_symbol, "regime_ema": sim.regime_ema},
            "read_only": True,
        },
        "cells": cells,
        "verdicts": verdicts(cells, required=required, accepted_family=ACCEPTED_FAMILY,
                            contrast_family=CONTRAST_FAMILY),
        "deployed_reference": deployed,
        "path_divergence": path_divergence(cells=cells, deployed=deployed, rule=CONTRAST_FAMILY),
        "accepted_verdict_reference": accepted_verdict_reference(
            run_file=Path(args.accepted_run), measured_params_label=params.label(),
            measured_symbols=len(symbols)),
        "cells_digest": cells_digest(cells),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "not_a_forecast": ("PAPER research counts on stored 1m bars, net of fees and slippage; a count "
                           "in the gate's unit is not a promise of profit (AGENTS.md rule 2)"),
    }
    out_path = Path(args.out)
    out_path = out_path if out_path.is_absolute() else root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    v = payload["verdicts"]
    acc = v["accepted_family"]
    print(f"\nverdict ({args.timeframe}, required {required}/day): H1 {acc['h1_supported']} "
          f"{acc['h1_windows_clearing_required']} | H2 {acc['h2_supported']} "
          f"{acc['h2_windows_clearing_required']} indeterminate {acc['h2_indeterminate_windows']} | "
          f"H3 {acc['h3_supported']}")
    print(f"cells measured {v['cells_measured']} | H1 rejected in {v['h1_rejected_cells']} cells | "
          f"H3 negative in {v['h3_negative_cells']} cells | digest {payload['cells_digest']}")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
