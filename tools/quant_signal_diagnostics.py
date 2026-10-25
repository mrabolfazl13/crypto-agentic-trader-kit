"""Explain every rejected quant hypothesis with per-gate signal counts.

Two evidence sources, both offline:

* ``state/quant/research.sqlite3``: re-run the *stored* parameters of each rule
  experiment through the current signal engine on the validation window and
  count how many bars survive each conjunctive gate. This separates "no signal"
  from "signal but no edge" without retraining or touching the test slice.
* ``state/platform.sqlite3``: export the rejection funnel that the older system
  recorded for each variant, including the zero-trade variants that failed the
  ``sized_positions_recorded`` risk audit.

Writes one JSON document and prints a short table. It never reads the hold-out
test slice and never changes any stored result.

Usage:
    python tools/quant_signal_diagnostics.py
    python tools/quant_signal_diagnostics.py --seq 0,1,4,5,6,7 --limit-symbols 20
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
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from cat.config import load_config  # noqa: E402
from cat.quant.engine import (chronological_splits, data_window, run_backtest,  # noqa: E402
                              simulate_settings, symbol_candidates)
from cat.quant.features import params_from_dict  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.storage import universe_symbols  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def merge_funnel(total: dict[str, Any], part: dict[str, Any]) -> None:
    for key in ("bars", "complete_bars", "indicator_ready_bars", "ready_bars",
                "entry_signals", "exit_signals"):
        total[key] = total.get(key, 0) + int(part.get(key) or 0)
    for step in part.get("funnel") or []:
        total.setdefault("funnel", {}).setdefault(step["gate"], 0)
        total["funnel"][step["gate"]] += int(step["passing"])
    for name, value in (part.get("conditions") or {}).items():
        total.setdefault("conditions", {}).setdefault(name, 0)
        total["conditions"][name] += int(value)


def empty_funnel() -> dict[str, Any]:
    return {"bars": 0, "complete_bars": 0, "indicator_ready_bars": 0, "ready_bars": 0,
            "entry_signals": 0, "exit_signals": 0, "funnel": {}, "conditions": {}}


def quant_experiments(db_path: Path, settings, symbols: list[str], window, seqs: list[int] | None,
                      with_metrics: bool = False) -> list[dict[str, Any]]:
    import sqlite3

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "select seq, strategy, timeframe, params_json, validation_json, status, acceptance, reason, "
        "created_at from experiments "
        "where status='completed' and params_json is not null and params_json != '' order by created_at"
    ).fetchall()
    connection.close()
    splits = chronological_splits(window, train=settings.walk_forward.train_fraction,
                                  validation=settings.walk_forward.validation_fraction)
    validation = splits["validation"]
    sim = simulate_settings(settings)
    output = []
    for row in rows:
        if seqs is not None and int(row["seq"]) not in seqs:
            continue
        strategy, timeframe = row["strategy"], row["timeframe"]
        if strategy not in {"ema_trend", "trend_pullback", "momentum", "mean_reversion", "breakout"}:
            continue
        stored = json.loads(row["params_json"])
        params = params_from_dict(overrides=stored)
        total = empty_funnel()
        per_symbol: dict[str, Any] = {}
        entries_by_symbol: dict[str, int] = {}
        errors: dict[str, str] = {}
        for index, symbol in enumerate(symbols, start=1):
            try:
                data = symbol_candidates(settings, symbol, strategy=strategy, timeframe=timeframe,
                                         params=params, sim=sim, start_ts=validation.start_ts,
                                         end_ts=validation.end_ts)
            except Exception as exc:  # noqa: BLE001 - one symbol must not hide the rest
                errors[symbol] = f"{type(exc).__name__}: {exc}"
                continue
            merge_funnel(total, data["gates"])
            steps = data["gates"]["funnel"]
            per_symbol[symbol] = {step["gate"]: step["passing"] for step in steps}
            entries_by_symbol[symbol] = int(steps[-1]["passing"]) if steps else 0
            if index % 10 == 0:
                print(f"  {strategy}/{timeframe}: {index}/{len(symbols)} symbols", flush=True)
        entry = int(total["entry_signals"])
        # The last cumulative step is the entry rule; this checks the merge did not
        # lose or double-count symbols.
        candidates = int(sum(entries_by_symbol.values()))
        stored_validation = json.loads(row["validation_json"]) if row["validation_json"] else {}
        record = {
            "source": "quant", "seq": int(row["seq"]), "strategy": strategy, "timeframe": timeframe,
            "stored_status": row["status"], "stored_acceptance": row["acceptance"],
            "stored_reason": row["reason"], "params": params.as_dict(),
            "stored_validation": {key: stored_validation.get(key) for key in
                                  ("trades", "net_return_pct", "profit_factor", "max_drawdown_pct",
                                   "effective_drawdown_pct", "benchmark_return_pct",
                                   "excess_return_pct") if key in stored_validation},
            "validation_window": validation.iso(), "symbols_evaluated": len(per_symbol),
            "errors": errors, "diagnostics": total,
            "funnel_sum_check": {"entry_signals": entry, "per_symbol_entry_sum": candidates},
        }
        if with_metrics:
            corrected = run_backtest(settings, symbols, strategy=strategy, timeframe=timeframe,
                                     params=params, sim=sim, start_ts=validation.start_ts,
                                     end_ts=validation.end_ts)
            record["corrected_validation"] = {
                key: corrected["metrics"].get(key) for key in
                ("trades", "net_return_pct", "profit_factor", "max_drawdown_pct",
                 "effective_drawdown_pct", "benchmark_return_pct", "excess_return_pct", "fees")
            }
            record["corrected_benchmark"] = corrected["benchmark"]
        output.append(record)
        print(f"seq {row['seq']} {strategy}/{timeframe}: ready={total['ready_bars']} "
              f"entry={entry} gates={total['funnel']}", flush=True)
    return output


def legacy_zero_trade_evidence(db_path: Path) -> list[dict[str, Any]]:
    import sqlite3

    if not db_path.exists():
        return []
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "select digest, name, version, params_json, train_json, decision, created_at from strategy_versions"
    ).fetchall()
    connection.close()
    output = []
    seen: set[str] = set()
    for row in rows:
        train = json.loads(row["train_json"]) if row["train_json"] else {}
        metrics = train.get("metrics") or {}
        rejected = train.get("rejected") or {}
        trades = metrics.get("trades")
        decision = row["decision"] or ""
        params = json.loads(row["params_json"]) if row["params_json"] else {}
        if (trades not in (0, None) and "risk-limit" not in decision
                and params.get("regime_gate") != "RANGING"):
            continue
        key = f"{row['digest']}|{row['created_at']}"
        if key in seen:
            continue
        seen.add(key)
        output.append({
            "source": "legacy", "digest": row["digest"], "label": f"{row['name']}:{row['version']}@{row['digest']}",
            "created_at": row["created_at"], "decision": decision,
            "train_trades": trades, "train_bars": train.get("bars"),
            "window": {"start": train.get("start"), "end": train.get("end")},
            "rejected": rejected,
            "params": {key: params.get(key) for key in
                       ("ema_fast", "ema_slow", "atr_stop_mult", "atr_target_mult", "regime_gate",
                        "rsi_bullish_low", "rsi_bullish_high", "slope_threshold", "vol_threshold",
                        "atr_pct_threshold")},
        })
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Per-gate signal diagnostics for rejected hypotheses")
    parser.add_argument("--db", default=str(ROOT / "state" / "quant" / "research.sqlite3"))
    parser.add_argument("--legacy-db", default=str(ROOT / "state" / "platform.sqlite3"))
    parser.add_argument("--out", default=str(ROOT / "state" / "quant" / "results" / "signal_diagnostics.json"))
    parser.add_argument("--seq", default="", help="comma-separated experiment seq values; default all")
    parser.add_argument("--limit-symbols", type=int, default=0)
    parser.add_argument("--skip-legacy", action="store_true")
    parser.add_argument("--with-metrics", action="store_true",
                        help="re-run validation with corrected engine and compare to the stored metrics")
    parser.add_argument("--metrics-only", action="store_true",
                        help="add corrected validation metrics to an existing report without re-counting gates")
    parser.add_argument("--legacy-only", action="store_true",
                        help="refresh the legacy section of an existing report without re-running gates")
    args = parser.parse_args()

    settings = load_quant_settings(load_config(root=ROOT))
    symbols = universe_symbols(settings)
    if args.limit_symbols:
        symbols = symbols[: max(1, args.limit_symbols)]
    window = data_window(settings, symbols)
    seqs = [int(part) for part in args.seq.split(",") if part.strip()] or None
    print(f"universe={len(symbols)} window={window.iso()} seqs={seqs or 'all'}", flush=True)

    out = Path(args.out)
    previous = (json.loads(out.read_text(encoding="utf-8"))
                if (args.legacy_only or args.metrics_only) and out.exists() else None)
    if args.metrics_only:
        if previous is None:
            parser.error("--metrics-only needs an existing report at --out")
        splits = chronological_splits(window, train=settings.walk_forward.train_fraction,
                                      validation=settings.walk_forward.validation_fraction)
        validation = splits["validation"]
        sim = simulate_settings(settings)
        for record in previous["experiments"]:
            params = params_from_dict(overrides=record["params"])
            corrected = run_backtest(settings, symbols, strategy=record["strategy"],
                                     timeframe=record["timeframe"], params=params, sim=sim,
                                     start_ts=validation.start_ts, end_ts=validation.end_ts)
            record["corrected_validation"] = {
                key: corrected["metrics"].get(key) for key in
                ("trades", "net_return_pct", "profit_factor", "max_drawdown_pct",
                 "effective_drawdown_pct", "benchmark_return_pct", "excess_return_pct", "fees")
            }
            record["corrected_benchmark"] = corrected["benchmark"]
            print(f"seq {record['seq']} corrected: {record['corrected_validation']}", flush=True)
        experiments = previous["experiments"]
    else:
        experiments = previous["experiments"] if previous else quant_experiments(
            Path(args.db), settings, symbols, window, seqs, with_metrics=args.with_metrics)
    payload = {
        "generated_at": now_iso(),
        "code_note": "gates are counted on the validation window only; the test slice is not read",
        "universe_symbols": len(symbols),
        "window": window.iso(),
        "experiments": experiments,
        "legacy_zero_trade": [] if args.skip_legacy else legacy_zero_trade_evidence(Path(args.legacy_db)),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
