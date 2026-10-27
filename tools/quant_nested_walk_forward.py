"""Nested walk-forward optimization on a rolling-origin window before the holdout.

Each fold is split chronologically into an inner search slice and an inner
selection slice. Trials are ranked on the search slice, only the finalists are
scored on the selection slice, and the chosen parameters are then measured on
that fold's unseen test week. Nothing in this tool reads the final hold-out
slice: the rolling window ends where the test slice starts.

This is a fresh protocol, not fresh market data. It exists because the earlier
search ranked trials on the validation slice it was later judged on; here every
scoring window is strictly earlier than the slice that scores it.

Writes one JSON per configuration (plus a partial JSON after every fold) and
appends one row per configuration to ``EXPERIMENT_LOG.csv``.

Usage:
    python tools/quant_nested_walk_forward.py
    python tools/quant_nested_walk_forward.py --configs ema_trend:4h --trials 6 --resume
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from cat.config import load_config  # noqa: E402
from cat.quant.engine import (Window, aggregate_folds, chronological_splits,  # noqa: E402
                              data_window, run_backtest, simulate_settings,
                              walk_forward_windows)
from cat.quant.features import params_from_dict  # noqa: E402
from cat.quant.loop import acceptance, code_version  # noqa: E402
from cat.quant.optimizer import optimize_windows  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.storage import current_data_version, universe_symbols  # noqa: E402
from quant_baseline import DEFAULT_CONFIGS, append_log, utc_now  # noqa: E402


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fold_protocol(train: Window, inner_selection_fraction: float) -> tuple[Window, Window]:
    span = train.end_ts - train.start_ts
    selection_start = train.end_ts - int(span * inner_selection_fraction)
    if selection_start <= train.start_ts:
        raise ValueError("fold train window is too short for the inner selection split")
    return Window(train.start_ts, selection_start), Window(selection_start, train.end_ts)


def evaluate_fold(settings, symbols_search: list[str], symbols: list[str], *, strategy: str,
                  timeframe: str, plan: dict[str, Window], trials: int, finalists: int,
                  inner_selection_fraction: float, seed: int,
                  include_trades: bool = False, sim=None) -> dict:
    search, selection = fold_protocol(plan["train"], inner_selection_fraction)
    test = plan["test"]
    if search.end_ts > plan["train"].end_ts or selection.end_ts > plan["train"].end_ts \
            or test.start_ts < plan["train"].end_ts:
        raise AssertionError("nested fold windows are not chronological inside the training slice")
    started = time.monotonic()
    search_result = optimize_windows(
        settings, symbols_search, strategy=strategy, timeframe=timeframe,
        search_window=search, selection_window=selection, allowed_window=plan["train"],
        trials=trials, seed=seed, finalists=finalists)
    params = params_from_dict(overrides=search_result["best"]["params"])
    scored = run_backtest(settings, symbols, strategy=strategy, timeframe=timeframe, params=params,
                          sim=sim or simulate_settings(settings),
                          start_ts=test.start_ts, end_ts=test.end_ts)
    payload = {
        "fold": plan["fold"],
        "train": plan["train"].iso(), "search": search.iso(), "selection": selection.iso(),
        "test": test.iso(),
        "chosen_on": search_result["chosen_on"],
        "best_params": params.as_dict(), "params_label": params.label(),
        "search_best": search_result["best"],
        "finalists": search_result["finalists"],
        "trials": search_result["trials"],
        "stop_reason": search_result["stop_reason"],
        "search_elapsed_seconds": search_result["elapsed_seconds"],
        "metrics": scored["metrics"], "benchmark": scored["benchmark"],
        "trades": len(scored["trades"]),
        "symbols_evaluated": scored["symbols_evaluated"],
        "errors": scored["errors"],
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    if include_trades:
        payload["trades_detail"] = scored["trades"]
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configs", default="", help="strategy:timeframe pairs, comma separated")
    parser.add_argument("--limit", type=int, default=0, help="limit scored symbols (0 = whole universe)")
    parser.add_argument("--search-symbols", type=int, default=30,
                        help="symbols used inside the per-fold search")
    parser.add_argument("--trials", type=int, default=6, help="trials per fold")
    parser.add_argument("--finalists", type=int, default=3, help="trials re-scored on the inner selection")
    parser.add_argument("--inner-selection-fraction", type=float, default=0.20)
    parser.add_argument("--dump-trades", action="store_true",
                        help="also write <run_id>.trades.json with the per-fold test trades, "
                             "for failed-trade analysis (the final holdout is still never read)")
    parser.add_argument("--regime-symbol", default="",
                        help="enable the pre-registered regime gate (D-012): long entries only "
                             "while this symbol is above its own EMA on the same timeframe")
    parser.add_argument("--regime-ema", type=int, default=200,
                        help="EMA period for --regime-symbol (default 200)")
    parser.add_argument("--no-regime", action="store_true",
                        help="clear the regime gate even if config enables it, so results recorded "
                             "before the gate became the default stay reproducible")
    parser.add_argument("--end", default="",
                        help="cap the data window end (ISO date, e.g. 2026-06-01) so a run can "
                             "replicate on an earlier regime slice; the cap is never extended "
                             "and the final holdout still starts where the test slice starts")
    parser.add_argument("--out-dir",
                        default=str(ROOT / "state" / "quant" / "results" / "nested_walk_forward"))
    parser.add_argument("--log", default=str(ROOT / "EXPERIMENT_LOG.csv"))
    parser.add_argument("--resume", action="store_true",
                        help="skip configurations that already have a completed JSON at this code version")
    args = parser.parse_args()

    settings = load_quant_settings(load_config(root=ROOT))
    sim = simulate_settings(settings)
    if args.no_regime and args.regime_symbol:
        raise SystemExit("--no-regime and --regime-symbol contradict each other")
    if args.no_regime:
        sim = simulate_settings(settings, regime_symbol="")
    elif args.regime_symbol:
        sim = simulate_settings(settings, regime_symbol=args.regime_symbol,
                                regime_ema=int(args.regime_ema))
    symbols = universe_symbols(settings)
    if args.limit:
        symbols = symbols[: max(1, args.limit)]
    symbols_search = symbols[: max(3, min(args.search_symbols, len(symbols)))]
    configs = DEFAULT_CONFIGS
    if args.configs:
        configs = tuple((part.split(":")[0], part.split(":")[1]) for part in args.configs.split(","))
    window = data_window(settings, symbols)
    if args.end:
        cap = int(datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc).timestamp())
        if cap <= window.start_ts:
            raise SystemExit(f"--end {args.end} precedes the data window start {window.iso()['start']}")
        if cap >= window.end_ts:
            raise SystemExit(f"--end {args.end} is beyond the data window end {window.iso()['end']}")
        window = Window(window.start_ts, cap)
    splits = chronological_splits(window, train=settings.walk_forward.train_fraction,
                                  validation=settings.walk_forward.validation_fraction)
    fold_window = Window(window.start_ts, splits["test"].start_ts)
    plans = walk_forward_windows(fold_window, folds=settings.walk_forward.folds,
                                 test_days=settings.walk_forward.test_days)
    version = code_version(ROOT)
    data_version = current_data_version(settings)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started_all = time.monotonic()
    run_stamp = utc_stamp()
    print(f"nested walk-forward: {len(configs)} configs x {len(plans)} folds x {args.trials} trials; "
          f"{len(symbols_search)} search symbols, {len(symbols)} scored symbols; "
          f"regime={sim.regime_symbol or 'off'}"
          + (f"/ema{sim.regime_ema}" if sim.regime_symbol else "")
          + f"; rolling window ends {fold_window.iso()['end']} (final test starts "
          f"{splits['test'].iso()['start']}, never read)", flush=True)

    for strategy, timeframe in configs:
        run_id = f"nested-wf-{strategy}-{timeframe}"
        target = out_dir / f"{run_id}.json"
        if args.resume and target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            if existing.get("code_version") == version and existing.get("status") == "completed":
                print(f"{run_id}: skipping (completed at {version})", flush=True)
                continue
        partial = out_dir / f"{run_id}.partial.json"
        records: list[dict] = []
        trade_dump: dict[str, list] = {}
        config_started = time.monotonic()
        for plan in plans:
            fold = evaluate_fold(settings, symbols_search, symbols, strategy=strategy,
                                 timeframe=timeframe, plan=plan, trials=args.trials,
                                 finalists=args.finalists,
                                 inner_selection_fraction=args.inner_selection_fraction,
                                 seed=42 + plan["fold"], include_trades=args.dump_trades,
                                 sim=sim)
            if args.dump_trades:
                trade_dump[str(fold["fold"])] = fold.pop("trades_detail", [])
            records.append(fold)
            if args.dump_trades:
                (out_dir / f"{run_id}.trades.json").write_text(json.dumps(
                    {"run_id": run_id, "code_version": version, "window": fold_window.iso(),
                     "final_test_read": False,
                     "note": "trades are test-fold output used for diagnosis; "
                             "the final holdout starting "
                             f"{splits['test'].iso()['start']} was never read",
                     "folds": trade_dump}, ensure_ascii=False, indent=1), encoding="utf-8")
            partial.write_text(json.dumps(
                {"run_id": run_id, "code_version": version, "data_version": data_version,
                 "window": fold_window.iso(), "final_test_start": splits["test"].iso()["start"],
                 "final_test_read": False, "folds": records, "updated_at": utc_now()},
                ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{run_id} fold {fold['fold']}: search {fold['search']['start']}..{fold['search']['end']} "
                  f"selection -> test {fold['test']['start']}..{fold['test']['end']} | "
                  f"trades={fold['metrics'].get('trades')} net={fold['metrics'].get('net_return_pct')}% "
                  f"BH={fold['benchmark'].get('return_pct')}% chosen={fold['chosen_on']} "
                  f"elapsed={fold['elapsed_seconds']}s", flush=True)

        aggregate = aggregate_folds(records)
        validation = {
            "net_return_pct": aggregate.get("net_return_pct_sum"),
            "effective_drawdown_pct": aggregate.get("max_effective_drawdown_pct"),
            "benchmark_return_pct": aggregate.get("benchmark_return_pct_sum"),
        }
        decision = acceptance(validation, aggregate, None, settings)
        seconds = time.monotonic() - config_started
        row = {
            "date": utc_now(), "run_id": run_id, "code_version": version,
            "strategy": strategy, "timeframe": timeframe, "slice": "nested_walk_forward",
            "params_label": "nested-train-only",
            "data_start": window.iso()["start"], "data_end": window.iso()["end"],
            "window_start": fold_window.iso()["start"], "window_end": fold_window.iso()["end"],
            "trades": aggregate.get("trades"), "net_return_pct": aggregate.get("net_return_pct_sum"),
            "max_drawdown_pct": aggregate.get("max_drawdown_pct"),
            "effective_drawdown_pct": aggregate.get("max_effective_drawdown_pct"),
            "profit_factor": aggregate.get("profit_factor_mean"),
            "win_rate_pct": None, "fees": None, "exposure_pct": None,
            "benchmark_return_pct": aggregate.get("benchmark_return_pct_sum"),
            "excess_return_pct": round(float(aggregate.get("net_return_pct_sum") or 0.0)
                                       - float(aggregate.get("benchmark_return_pct_sum") or 0.0), 4),
            "status": f"completed; decision={decision['decision']}; "
                      f"positive_folds_pct={aggregate.get('positive_fold_pct')}; "
                      f"folds={aggregate.get('folds')}; final_test_read=false; "
                      f"regime={sim.regime_symbol or 'off'}"
                      + (f"/ema{sim.regime_ema}" if sim.regime_symbol else ""),
        }
        payload = {
            "run_id": run_id, "created_at": row["date"], "code_version": version,
            "data_version": data_version, "status": "completed",
            "protocol": {"trials_per_fold": args.trials, "finalists": args.finalists,
                         "inner_selection_fraction": args.inner_selection_fraction,
                         "search_symbols": len(symbols_search), "scored_symbols": len(symbols),
                         "folds": settings.walk_forward.folds,
                         "test_days": settings.walk_forward.test_days,
                         "regime_symbol": sim.regime_symbol or None,
                         "regime_ema": sim.regime_ema if sim.regime_symbol else None},
            "window": fold_window.iso(), "final_test_start": splits["test"].iso()["start"],
            "final_test_read": False,
            "folds": records, "aggregate": aggregate, "decision": decision,
            "elapsed_seconds": round(seconds, 2), "generated_at": run_stamp,
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        partial.unlink(missing_ok=True)
        append_log(Path(args.log), row)
        print(f"{run_id}: folds={aggregate.get('folds')} trades={row['trades']} "
              f"sum_net={row['net_return_pct']}% BH_sum={row['benchmark_return_pct']}% "
              f"excess={row['excess_return_pct']}pp positive={aggregate.get('positive_fold_pct')}% "
              f"PF={row['profit_factor']} DD={row['max_drawdown_pct']}% "
              f"decision={decision['decision']} elapsed={seconds:.0f}s", flush=True)

    summary = {"generated_at": utc_now(), "code_version": version, "data_version": data_version,
               "window": fold_window.iso(), "final_test_start": splits["test"].iso()["start"],
               "final_test_read": False,
               "elapsed_seconds": round(time.monotonic() - started_all, 1)}
    (out_dir / "nested_walk_forward_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"total {time.monotonic() - started_all:.1f}s; log {args.log}; out {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
